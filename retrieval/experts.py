"""Durable retrieval copy of approved answers. Backend owns publication decisions."""
from contextlib import closing
from dataclasses import asdict
from datetime import datetime
from hashlib import sha256
import json
import sqlite3
from uuid import uuid4

import numpy as np

from shared.contracts import ApplicabilityScope, ExpertAnswerMetadata, RetrievalError, SearchResult, SourceStatus, SourceType


def embedding(encoder, text):
    # Split on character boundaries, checking the actual tokenizer including prefix.
    passages = []
    while text:
        end = len(text)
        while encoder.count("passage: " + text[:end]) > encoder.limit:
            end //= 2
            if not end:
                raise RetrievalError("Expert passage exceeds encoder capacity")
        passages.append("passage: " + text[:end])
        text = text[end:]
    vector = encoder.encode(passages).mean(axis=0)
    norm = np.linalg.norm(vector)
    if vector.shape != (encoder.dimension,) or not np.isfinite(norm) or norm <= 0:
        raise RetrievalError("Invalid expert embedding")
    return (vector / norm).astype("<f4").tobytes()


def content_checksum(payload, question):
    data = json.dumps([payload, question], ensure_ascii=False)
    return sha256(data.encode("utf-8")).hexdigest()


def model_key(encoder):
    return json.dumps([encoder.model, encoder.revision, encoder.dimension])


class ExpertIndex:
    def __init__(self, root):
        self.path = root / "experts.sqlite3"
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS entries (
                id TEXT PRIMARY KEY, revision INTEGER NOT NULL, answer TEXT,
                question TEXT, model TEXT, vector BLOB, checksum TEXT)""")
            columns = {column[1] for column in db.execute("PRAGMA table_info(entries)")}
            if "checksum" not in columns:
                db.execute("ALTER TABLE entries ADD COLUMN checksum TEXT")
                db.executemany("UPDATE entries SET checksum=? WHERE id=?", [
                    (content_checksum(answer, question), source_id)
                    for source_id, answer, question in db.execute(
                        "SELECT id,answer,question FROM entries WHERE answer IS NOT NULL")
                ])
            db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, generation TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO state VALUES (1, ?)", (uuid4().hex,))

    def apply(self, source_id, revision, answer, publications, encoder):
        if (not isinstance(source_id, str) or not source_id.startswith("expert_answer:")
                or not source_id.removeprefix("expert_answer:") or type(revision) is not int or revision < 1):
            raise RetrievalError("Invalid expert identity or revision")
        with closing(sqlite3.connect(self.path, timeout=30)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT revision,answer,question,model,vector FROM entries WHERE id=?",
                             (source_id,)).fetchone()
            if old and old[0] > revision:
                return
            payload = question = key = vector = checksum = None
            if answer is not None:
                if source_id != "expert_answer:" + answer.metadata.appeal_id or not answer.text.strip():
                    raise RetrievalError("Invalid approved answer")
                record = asdict(answer)
                record["metadata"]["approved_at"] = answer.metadata.approved_at.isoformat()
                payload = json.dumps(record, ensure_ascii=False, sort_keys=True)
            if old and old[0] == revision:
                if old[1] != payload:
                    raise RetrievalError("Conflicting operations for one expert revision")
                return
            if answer is not None:
                # Quality supplies the immutable original appeal question, including before publication.
                question_lookup = getattr(publications, "index_questions", None)
                if question_lookup is None:
                    raise RetrievalError("Original expert question lookup unavailable")
                question = question_lookup((source_id,)).get(source_id)
                if not isinstance(question, str) or not question.strip():
                    raise RetrievalError("Original expert question unavailable")
                checksum = content_checksum(payload, question)
                key = model_key(encoder)
                if (old and old[1] is not None and old[2:4] == (question, key)
                        and json.loads(old[1])["text"] == answer.text):
                    vector = old[4]
                else:
                    vector = embedding(encoder, question + "\n" + answer.text)
            db.execute("INSERT OR REPLACE INTO entries (id,revision,answer,question,model,vector,checksum) VALUES (?,?,?,?,?,?,?)",
                       (source_id, revision, payload, question, key, vector, checksum))
            db.execute("UPDATE state SET generation=? WHERE id=1", (uuid4().hex,))

    def snapshot(self, previous, encoder):
        with closing(sqlite3.connect(self.path, timeout=30)) as db, db:
            current_key = model_key(encoder)
            db.execute("BEGIN")
            # Acquire a write transaction only when persisted vectors require a model migration.
            if db.execute("SELECT 1 FROM entries WHERE answer IS NOT NULL AND model != ? LIMIT 1",
                          (current_key,)).fetchone():
                db.rollback()
                db.execute("BEGIN IMMEDIATE")
            generation, = db.execute("SELECT generation FROM state WHERE id=1").fetchone()
            if generation == previous:
                return None
            results, chunks, vectors = [], [], []
            for source_id, revision, payload, question, key, raw, checksum in db.execute(
                "SELECT id,revision,answer,question,model,vector,checksum FROM entries WHERE answer IS NOT NULL ORDER BY id"
            ):
                if content_checksum(payload, question) != checksum:
                    raise RetrievalError("Invalid stored expert checksum")
                item = json.loads(payload)
                meta = item["metadata"]
                if item["source_id"] != source_id or meta["revision"] != revision:
                    raise RetrievalError("Stored expert identity does not match its revision")
                scope = ApplicabilityScope(**meta["scope"])
                metadata = ExpertAnswerMetadata(meta["appeal_id"], datetime.fromisoformat(meta["approved_at"]),
                                                scope, meta["revision"])
                text = question + "\n" + item["text"]
                if key != current_key:
                    raw = embedding(encoder, text)
                    db.execute("UPDATE entries SET model=?,vector=? WHERE id=?",
                               (current_key, raw, item["source_id"]))
                vector = np.frombuffer(raw, dtype="<f4")
                if vector.shape != (encoder.dimension,) or not np.isclose(np.linalg.norm(vector), 1, atol=1e-4):
                    raise RetrievalError("Invalid stored expert vector")
                vectors.append(vector)
                chunks.append(dict(document=item["document"], department=scope.department, stage=scope.stage,
                                   section=item["section"], spans=[], index_text="passage: " + text))
                results.append(SearchResult(item["source_id"], SourceType.EXPERT_ANSWER, item["document"],
                                            item["text"], SourceStatus.APPROVED, 0., section=item["section"],
                                            department=scope.department, stage=scope.stage,
                                            source_url=item["source_url"], expert_metadata=metadata))
            return generation, results, chunks, vectors
