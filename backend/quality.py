"""Durable appeals, editorial workflow, and synchronization with retrieval."""

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from time import time
from uuid import uuid4

from backend.storage import AccessError, Store, dump
from shared.contracts import (
    ApplicabilityScope, ApprovedExpertAnswer, ExpertAnswerMetadata, RetrievalError,
)

INDEX_ATTEMPTS = 3


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class Quality:
    def __init__(self, store: Store, retrieval, mailer, *, mail_status="disabled"):
        self.store, self.retrieval, self.mailer = store, retrieval, mailer
        self.mail_status = "pending" if mailer else mail_status

    def initialize(self) -> None:
        with closing(self.store.connection()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS quality_appeals (
                    id TEXT PRIMARY KEY, turn_id INTEGER NOT NULL UNIQUE REFERENCES turns(id),
                    dialogue_id TEXT NOT NULL REFERENCES dialogues(id),
                    context TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
                    created_epoch REAL NOT NULL, updated_at TEXT NOT NULL, status TEXT NOT NULL,
                    draft TEXT, revision INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS quality_by_dialogue ON quality_appeals(dialogue_id, created_epoch);
                CREATE TABLE IF NOT EXISTS quality_revisions (
                    appeal_id TEXT NOT NULL REFERENCES quality_appeals(id), revision INTEGER NOT NULL,
                    action TEXT NOT NULL, payload TEXT, approved_by TEXT NOT NULL, approved_at TEXT NOT NULL,
                    state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL,
                    lease TEXT, PRIMARY KEY(appeal_id, revision)
                );
                CREATE TABLE IF NOT EXISTS quality_mail (
                    appeal_id TEXT PRIMARY KEY REFERENCES quality_appeals(id), status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL, lease TEXT
                );
            """)

    def create(self, dialogue_id, token, turn_id):
        now = time()
        with self.store.write() as db:
            self.store._get(db, dialogue_id, token)
            existing = db.execute("SELECT id FROM quality_appeals WHERE dialogue_id=? AND turn_id=?",
                                  (dialogue_id, turn_id)).fetchone()
            if existing:
                return self._detail(db, existing["id"]), False
            turn = db.execute("""
                SELECT t.result, c.snapshot FROM turns AS t JOIN turn_appeal_context AS c ON t.id=c.turn_id
                WHERE t.dialogue_id=? AND t.id=? AND t.error IS NULL
            """, (dialogue_id, turn_id)).fetchone()
            if turn is None:
                raise AccessError("appeal_ineligible")
            kind = json.loads(turn["result"])["result_type"]
            if kind not in {"NO_ANSWER", "CONFLICT"}:
                raise AccessError("appeal_ineligible")
            global_count, dialogue_count = db.execute("""
                SELECT count(*), count(CASE WHEN dialogue_id=? THEN 1 END)
                FROM quality_appeals WHERE created_epoch>=?
            """, (dialogue_id, now - 86400)).fetchone()
            if global_count >= 10 or dialogue_count >= 3:
                raise AccessError("appeal_rate_limit")
            key, stamp = str(uuid4()), now_iso()
            db.execute("""
                INSERT INTO quality_appeals(id, turn_id, dialogue_id, context, reason, created_at,
                                            created_epoch, updated_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new')
            """, (key, turn_id, dialogue_id, turn["snapshot"], kind, stamp, now, stamp))
            db.execute("INSERT INTO quality_mail(appeal_id,status,due) VALUES (?,?,?)",
                       (key, self.mail_status, now))
            return self._detail(db, key), True

    def _detail(self, db, key):
        row = db.execute("SELECT * FROM quality_appeals WHERE id=?", (key,)).fetchone()
        if row is None:
            raise AccessError("appeal_not_found")
        revision = db.execute("""
            SELECT revision,action,payload,approved_by,approved_at,state,attempts
            FROM quality_revisions WHERE appeal_id=? AND revision=?
        """, (key, row["revision"])).fetchone()
        mail = db.execute("SELECT status,attempts FROM quality_mail WHERE appeal_id=?", (key,)).fetchone()
        latest = dict(revision) if revision else None
        if latest:
            latest["payload"] = json.loads(latest["payload"])
            latest["published"] = latest["state"] == "indexed" and latest["action"] == "upsert"
        return {
            "id": key, "turn_id": row["turn_id"], "dialogue_id": row["dialogue_id"],
            **json.loads(row["context"]), "reason": row["reason"], "status": row["status"],
            "created_at": row["created_at"], "updated_at": row["updated_at"], "version": row["version"],
            "draft": json.loads(row["draft"]) if row["draft"] else None, "publication": latest,
            "notification": dict(mail),
        }

    def user_detail(self, dialogue_id, token, key):
        with closing(self.store.connection()) as db:
            self.store._get(db, dialogue_id, token)
            row = db.execute("SELECT 1 FROM quality_appeals WHERE id=? AND dialogue_id=?", (key, dialogue_id)).fetchone()
            if row is None:
                raise AccessError("appeal_not_found")
            return self.public(self._detail(db, key))

    @staticmethod
    def public(detail):
        return {field: detail[field] for field in ("id", "turn_id", "status", "created_at", "notification")}

    def detail(self, key):
        with closing(self.store.connection()) as db:
            db.execute("BEGIN")
            return self._detail(db, key)

    def listing(self, status, offset, limit, explanations=False):
        with closing(self.store.connection()) as db:
            db.execute("BEGIN")
            if explanations:
                rows = db.execute("""
                    SELECT a.id FROM quality_appeals AS a
                    JOIN quality_revisions AS r ON r.appeal_id=a.id AND r.revision=a.revision
                    WHERE r.action='upsert' AND r.state='indexed'
                    ORDER BY a.created_epoch DESC,a.id LIMIT ? OFFSET ?
                """, (limit, offset))
            else:
                rows = db.execute("""
                    SELECT id FROM quality_appeals WHERE (? IS NULL OR status=?)
                    ORDER BY created_epoch DESC,id LIMIT ? OFFSET ?
                """, (status, status, limit, offset))
            return [self._detail(db, row["id"]) for row in rows]

    @staticmethod
    def _version(detail, expected):
        if detail["version"] != expected:
            raise AccessError("appeal_changed")

    def status(self, key, version, status):
        with self.store.write() as db:
            item = self._detail(db, key)
            self._version(item, version)
            if status == "in_work" and item["status"] not in {"new", "in_work", "closed"}:
                raise AccessError("appeal_transition")
            db.execute("UPDATE quality_appeals SET status=?, version=version+1, updated_at=? WHERE id=?",
                       (status, now_iso(), key))
            return self._detail(db, key)

    def draft(self, key, version, answer):
        with self.store.write() as db:
            item = self._detail(db, key)
            self._version(item, version)
            if item["status"] == "closed":
                raise AccessError("appeal_transition")
            publication = item["publication"]
            previous = item["draft"] or (publication["payload"] if publication else None) or {}
            payload = {
                "department": item["department"], "stage": item["stage"], "conditions": None,
                **previous, **answer,
            }
            db.execute("""
                UPDATE quality_appeals SET draft=?,status='draft', version=version+1, updated_at=? WHERE id=?
            """, (dump(payload), now_iso(), key))
            return self._detail(db, key)

    def change_publication(self, key, version, actor, action):
        with self.store.write() as db:
            item = self._detail(db, key)
            self._version(item, version)
            current = item["publication"]
            if action == "upsert" and (item["status"] == "closed" or item["draft"] is None):
                raise AccessError("appeal_transition")
            if action == "delete" and (current is None or current["action"] != "upsert"):
                raise AccessError("appeal_transition")
            revision, stamp = (current["revision"] if current else 0) + 1, now_iso()
            payload = item["draft"] if action == "upsert" else None
            db.execute("""
                INSERT INTO quality_revisions(appeal_id,revision,action,payload,approved_by,approved_at,state,due)
                VALUES (?,?,?,?,?,?,'pending',?)
            """, (key, revision, action, dump(payload), actor, stamp, time()))
            db.execute("""
                UPDATE quality_appeals SET status=?, revision=?, draft=NULL, updated_at=?,version=version+1 WHERE id=?
            """, ("approved" if action == "upsert" else "in_work", revision, stamp, key))
        self.process_index(key)
        return self.detail(key)

    def retry_index(self, key, version):
        with self.store.write() as db:
            item = self._detail(db, key)
            self._version(item, version)
            current = item["publication"]
            if not current or current["state"] not in {"failed", "indexed"}:
                raise AccessError("appeal_transition")
            db.execute("""
                UPDATE quality_revisions SET state='pending', attempts=0, due=?
                WHERE appeal_id=? AND revision=?
            """, (time(), key, current["revision"]))
            db.execute("UPDATE quality_appeals SET version=version+1,updated_at=? WHERE id=?", (now_iso(), key))
        self.process_index(key)
        return self.detail(key)

    def process_index(self, key=None):
        if self.retrieval is None:
            return
        now, lease = time(), str(uuid4())
        with self.store.write() as db:
            db.execute("""
                UPDATE quality_revisions SET state='failed', lease=NULL
                WHERE state='running' AND due<=? AND attempts>=?
            """, (now, INDEX_ATTEMPTS))
            row = db.execute("""
                SELECT r.*,a.context FROM quality_revisions AS r
                JOIN quality_appeals AS a ON a.id=r.appeal_id AND a.revision=r.revision
                WHERE r.state IN ('pending','running') AND r.due<=? AND (? IS NULL OR a.id=?)
                ORDER BY r.due LIMIT 1
            """, (now, key, key)).fetchone()
            if row is None:
                return
            identity = (row["appeal_id"], row["revision"])
            db.execute("""
                UPDATE quality_revisions SET state='running',attempts=attempts+1,lease=?,due=?
                WHERE appeal_id=? AND revision=?
            """, (lease, now + 120, *identity))

        source_id = f"expert_answer:{identity[0]}"
        try:
            if row["action"] == "delete":
                self.retrieval.delete_expert_answer(source_id, identity[1])
            else:
                body = json.loads(row["payload"])
                self.retrieval.upsert_expert_answer(ApprovedExpertAnswer(
                    source_id=source_id, document=f"Разъяснение {identity[0]}",
                    text=body["text"], metadata=ExpertAnswerMetadata(
                        identity[0], datetime.fromisoformat(row["approved_at"]),
                        ApplicabilityScope(body["department"], body["stage"], body["conditions"]), identity[1],
                    ),
                ))
        except Exception:
            attempt = row["attempts"] + 1
            state = "failed" if attempt >= INDEX_ATTEMPTS else "pending"
            due = time() + 2 ** attempt
        else:
            state, due = "indexed", time()
        with self.store.write() as db:
            db.execute("""
                UPDATE quality_revisions SET state=?,lease=NULL,due=?
                WHERE appeal_id=? AND revision=? AND lease=?
            """, (state, due, *identity, lease))

    def published_revisions(self, source_ids: tuple[str, ...]) -> dict[str, int]:
        prefix = "expert_answer:"
        keys = tuple(key[len(prefix):] for key in source_ids if key.startswith(prefix))
        if not keys:
            return {}
        placeholders = ",".join("?" for _ in keys)
        try:
            with closing(self.store.connection()) as db:
                rows = db.execute(f"""
                    SELECT r.appeal_id,r.revision FROM quality_revisions AS r
                    JOIN quality_appeals AS a ON a.id=r.appeal_id AND a.revision=r.revision
                    WHERE r.action='upsert' AND r.state='indexed' AND r.appeal_id IN ({placeholders})
                """, keys)
                return {prefix + row["appeal_id"]: row["revision"] for row in rows}
        except sqlite3.Error:
            raise RetrievalError("publication lookup unavailable") from None

    def index_questions(self, source_ids: tuple[str, ...]) -> dict[str, str]:
        """Supply original questions to the synthetic adapter."""
        with closing(self.store.connection()) as db:
            result = {}
            for source_id in source_ids:
                row = db.execute("SELECT context FROM quality_appeals WHERE id=?",
                                 (source_id.removeprefix("expert_answer:"),)).fetchone()
                if row:
                    result[source_id] = json.loads(row["context"])["question"]
            return result
