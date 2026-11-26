"""Opt-in synthetic search fixture for a local demonstration."""

import re
from contextlib import closing
from dataclasses import asdict
from datetime import datetime
from json import loads

from backend.storage import dump

from shared.contracts import (
    ApplicabilityScope, ExpertAnswerMetadata, RetrievalError, SearchResult, SourceStatus, SourceType,
)

_FIXTURES = (
    ("ib", "presale", "1.1", "Опытный макет на пресейле принимает дежурный наблюдатель."),
    ("tech", "presale", "1.2", "Опытный макет на пресейле принимает координатор демонстрации."),
    ("tech", "implementation", "2.1", "Опытный макет на этапе реализации принимает руководитель стенда."),
    ("tech", "implementation", "2.2", "На этапе реализации руководитель стенда фиксирует приёмку опытного макета в журнале «Сапфир»."),
    ("dbrct", "implementation", "3.1", "Калибровку макета согласует инженер учебной мастерской."),
    ("tech", "implementation", "4.1", "Акт испытаний опытного макета подписывает руководитель стенда."),
    ("tech", "implementation", "4.2", "Акт испытаний опытного макета подписывает координатор испытаний."),
    ("tech", "implementation", "5.1", "Для синего макета нужен бумажный ярлык. Для красного макета нужен магнитный ярлык."),
)
_IGNORED = {"какой", "какая", "кто", "куда", "когда", "где", "проект", "это", "этап", "реали",
            "пресе", "дирек", "техни", "опытн", "учебн", "доста", "поряд", "отвеч", "сведе"}


def _words(text):
    return {word[:5] for word in re.findall(r"[а-яё0-9]{4,}", text.casefold())} - _IGNORED


class DemoRetrieval:
    def __init__(self, store, publications):
        self.store, self.publications = store, publications

    def initialize(self):
        with closing(self.store.connection()) as db:
            db.execute('''
                CREATE TABLE IF NOT EXISTS demo_expert_index (
                    source_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, payload TEXT
                )
            ''')

    def search_documents(self, question, top_k=8, department=None, stage=None):
        current, *context = question.splitlines()
        words, previous = _words(current), _words(" ".join(context))
        found = []
        for scope, phase, section, text in _FIXTURES:
            if department and scope not in ((department,) if isinstance(department, str) else department):
                continue
            if stage and phase not in ((stage,) if isinstance(stage, str) else stage):
                continue
            hits = _words(text)
            direct_hits = words & hits
            if direct_hits:
                score = 4 * len(direct_hits) + len(previous & hits)
                found.append(SearchResult(
                    source_id=f"regulation:demo-{scope}:{section}", source_type=SourceType.REGULATION,
                    document=f"Синтетический демонстрационный регламент ({scope})",
                    text=text, section=section, score=float(score), status=SourceStatus.UNKNOWN,
                    department=scope, stage=phase, document_id=f"demo-{scope}",
                ))
        with closing(self.store.connection()) as db:
            rows = db.execute("SELECT * FROM demo_expert_index WHERE payload IS NOT NULL").fetchall()
        keys = tuple(row["source_id"] for row in rows)
        active = self.publications.published_revisions(keys)
        questions = self.publications.index_questions(tuple(active))
        for row in rows:
            key = row["source_id"]
            if active.get(key) != row["revision"]:
                continue
            body = loads(row["payload"])
            meta = body["metadata"]
            scope = ApplicabilityScope(**meta["scope"])
            if department and scope.department and scope.department not in (
                (department,) if isinstance(department, str) else department
            ):
                continue
            if stage and scope.stage and scope.stage not in ((stage,) if isinstance(stage, str) else stage):
                continue
            original = questions.get(key, "")
            direct_hits = words & (_words(body["text"]) | _words(original))
            if not direct_hits and current.strip().casefold() != original.strip().casefold():
                continue
            found.append(SearchResult(
                key, SourceType.EXPERT_ANSWER, body["document"], body["text"], SourceStatus.APPROVED,
                float(4 * len(direct_hits)), department=scope.department, stage=scope.stage,
                expert_metadata=ExpertAnswerMetadata(
                    meta["appeal_id"], datetime.fromisoformat(meta["approved_at"]), scope, meta["revision"],
                ),
            ))
        return sorted(found, key=lambda source: -source.score)[:top_k]

    def upsert_expert_answer(self, answer):
        self._update(answer.source_id, answer.metadata.revision, dump(asdict(answer)))

    def delete_expert_answer(self, source_id, revision):
        self._update(source_id, revision, None)

    def _update(self, source_id, revision, payload):
        with self.store.write() as db:
            existing = db.execute("SELECT revision,payload FROM demo_expert_index WHERE source_id=?",
                                  (source_id,)).fetchone()
            if existing and existing["revision"] == revision and existing["payload"] != payload:
                raise RetrievalError("Conflicting payload for the same revision")
            db.execute("""
                INSERT INTO demo_expert_index(source_id,revision,payload) VALUES (?,?,?)
                ON CONFLICT(source_id) DO UPDATE SET revision=excluded.revision,payload=excluded.payload
                WHERE excluded.revision>demo_expert_index.revision
            """, (source_id, revision, payload))
