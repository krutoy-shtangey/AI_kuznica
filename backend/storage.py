"""SQLite state, dialogue capabilities, and request accounting."""

import hashlib
import json
import secrets
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from time import time
from uuid import uuid4

from backend.agent import DialogueState


class AccessError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def dump(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def summarize(calls: list[dict | None]) -> dict:
    attempts = [item for call in calls if call for item in call["attempts"]]
    totals = {"attempts": len(attempts), "seconds": sum(call["seconds"] for call in calls if call)}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        values = [item[key] for item in attempts]
        totals[key] = None if any(value is None for value in values) else sum(values)

    for key, currency_field in (("provider_cost", "provider_currency"), ("alibaba_estimate", "alibaba_currency")):
        subtotals = {}
        unknown = 0
        for attempt in attempts:
            if attempt[key] is None:
                unknown += 1
            else:
                currency = attempt[currency_field]
                subtotals[currency] = subtotals.get(currency, Decimal(0)) + Decimal(attempt[key])
        totals[key] = {
            "known_by_currency": {key: str(value) for key, value in subtotals.items()},
            "unknown_attempts": unknown,
        }
    return totals


class Store:
    def __init__(self, path: Path, *, per_minute: int = 12, daily_attempts: int = 80,
                 dialogue_turns: int = 100, lease_seconds: int = 300):
        self.path = path
        self.per_minute = per_minute
        self.daily_attempts = daily_attempts
        self.dialogue_turns = dialogue_turns
        self.lease_seconds = lease_seconds

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connection()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS dialogues (
                    id TEXT PRIMARY KEY, token_hash TEXT NOT NULL,
                    state TEXT NOT NULL, lease_id TEXT, lease_until REAL
                );
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY, dialogue_id TEXT NOT NULL REFERENCES dialogues(id),
                    message TEXT NOT NULL, result TEXT, sources TEXT NOT NULL,
                    metrics TEXT, error TEXT
                );
                CREATE INDEX IF NOT EXISTS turns_dialogue ON turns(dialogue_id, id);
                CREATE TABLE IF NOT EXISTS activity (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, instant REAL NOT NULL, attempts INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS activity_time ON activity(kind, instant);
            """)
            db.execute("SELECT 1")

    def connection(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, isolation_level=None, timeout=3)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def write(self):
        with closing(self.connection()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                yield db
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()

    def _rate(self, db, kind, now):
        db.execute("DELETE FROM activity WHERE instant < ?", (now - 86400,))
        recent, = db.execute("SELECT count(*) FROM activity WHERE kind=? AND instant>=?", (kind, now - 60)).fetchone()
        if recent >= self.per_minute:
            raise AccessError("rate_limit")

    def create(self) -> tuple[str, str]:
        dialogue_id, token = str(uuid4()), secrets.token_urlsafe(32)
        now = time()
        with self.write() as db:
            self._rate(db, "create", now)
            db.execute("INSERT INTO activity VALUES (?, 'create', ?, 0)", (dialogue_id, now))
            db.execute("INSERT INTO dialogues(id, token_hash, state) VALUES (?, ?, ?)",
                       (dialogue_id, digest(token), dump(asdict(DialogueState()))))
        return dialogue_id, token

    @staticmethod
    def _get(db, dialogue_id, token):
        row = db.execute("SELECT * FROM dialogues WHERE id=? AND token_hash=?",
                         (dialogue_id, digest(token))).fetchone()
        if row is None:
            raise AccessError("dialogue_not_found")
        return row

    def begin(self, dialogue_id: str, token: str, max_attempts: int) -> tuple[str, DialogueState]:
        lease, now = str(uuid4()), time()
        with self.write() as db:
            row = self._get(db, dialogue_id, token)
            if row["lease_id"] and row["lease_until"] > now:
                raise AccessError("dialogue_busy")
            turns, = db.execute("SELECT count(*) FROM turns WHERE dialogue_id=?", (dialogue_id,)).fetchone()
            if turns >= self.dialogue_turns:
                raise AccessError("dialogue_limit")
            self._rate(db, "send", now)
            reserved, = db.execute("SELECT coalesce(sum(attempts), 0) FROM activity WHERE kind='send'").fetchone()
            if reserved + max_attempts > self.daily_attempts:
                raise AccessError("daily_budget")
            db.execute("INSERT INTO activity VALUES (?, 'send', ?, ?)", (lease, now, max_attempts))
            db.execute("UPDATE dialogues SET lease_id=?, lease_until=? WHERE id=?",
                       (lease, now + self.lease_seconds, dialogue_id))
            return lease, DialogueState.load(json.loads(row["state"]))

    def finish(self, dialogue_id, lease, message, *, outcome=None, error=None) -> dict:
        result = asdict(outcome.result) if outcome is not None else None
        sources = outcome.sources if outcome is not None else ()
        metrics_object = outcome.metrics if outcome is not None else error.metrics
        metrics = asdict(metrics_object) if metrics_object is not None else None
        with self.write() as db:
            row = db.execute("SELECT state FROM dialogues WHERE id=? AND lease_id=?", (dialogue_id, lease)).fetchone()
            if row is None:
                raise AccessError("dialogue_busy")
            state = dump(asdict(outcome.state)) if outcome is not None else row["state"]
            db.execute("INSERT INTO turns(dialogue_id, message, result, sources, metrics, error) VALUES (?, ?, ?, ?, ?, ?)",
                       (dialogue_id, message, dump(result), dump(sources), dump(metrics), error.code if error else None))
            db.execute("UPDATE dialogues SET state=?, lease_id=NULL, lease_until=NULL WHERE id=?", (state, dialogue_id))
            # Unexpected failures may have called a provider without returning metrics. Keep their reservation.
            if error is None or metrics is not None or error.code != "internal_error":
                db.execute("UPDATE activity SET attempts=? WHERE id=?",
                           (metrics_object.attempt_count if metrics_object else 0, lease))
            return self._usage(db, dialogue_id)

    @staticmethod
    def _usage(db, dialogue_id):
        return summarize([json.loads(row[0]) for row in db.execute(
            "SELECT metrics FROM turns WHERE dialogue_id=?", (dialogue_id,)
        )])

    def history(self, dialogue_id, token, before: int | None, limit: int) -> tuple[list[dict], int | None, dict]:
        with closing(self.connection()) as db:
            db.execute("BEGIN")
            self._get(db, dialogue_id, token)
            rows = db.execute("""
                SELECT * FROM turns WHERE dialogue_id=? AND (? IS NULL OR id<?)
                ORDER BY id DESC LIMIT ?
            """, (dialogue_id, before, before, limit + 1)).fetchall()
            selected = rows[:limit]
            turns = [
                {"id": row["id"], "message": row["message"], "result": json.loads(row["result"]),
                 "sources": json.loads(row["sources"]), "metrics": json.loads(row["metrics"]), "error": row["error"]}
                for row in reversed(selected)
            ]
            return turns, selected[-1]["id"] if len(rows) > limit else None, self._usage(db, dialogue_id)
