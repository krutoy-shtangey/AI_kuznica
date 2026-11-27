"""Integration checks on a temporary synthetic corpus. Run via unittest discovery."""
from concurrent.futures import ThreadPoolExecutor
from importlib.util import find_spec
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from retrieval.agent_adapter import retrieval_factory
from retrieval.embeddings import Encoder
from retrieval.index import update_index
from shared.contracts import RetrievalError, SearchResult
from test_search import build, CORPUS, Fake


@unittest.skipUnless(find_spec("backend"), "requires backend from main")
class AgentRetrievalTests(unittest.TestCase):
    def setUp(self):
        from backend.main import create_app
        from fastapi.testclient import TestClient
        from test_agent_api import FakeModel

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.index = self.root / "index"
        self.encoder = Fake()
        build(self.root, self.encoder)
        self.enterContext(patch.dict(os.environ, RETRIEVAL_INDEX=str(self.index)))
        self.encoder_factory = self.enterContext(patch("retrieval.search.Encoder", return_value=self.encoder))

        def start():
            from test_quality_api import SECRET
            app = create_app(retrieval_factory=retrieval_factory, db_path=self.root / "test.sqlite3",
                             quality_secret=SECRET,
                             model=FakeModel(), workers=False)
            client = self.enterContext(TestClient(app))
            return client, app.state.quality.retrieval

        self.start = start
        self.client, self.adapter = start()

    def message(self, question):
        dialogue = self.client.post("/dialogues").json()
        return self.client.post(f"/dialogues/{dialogue['dialogue_id']}/messages",
                                headers={"Authorization": "Bearer " + dialogue["access_token"]},
                                json={"message": question})

    def test_http_citation_and_alternatives(self):
        from test_agent_api import FakeModel, draft

        hits, details = self.adapter.search_with_context("доступ к стенду", stage="implementation")
        self.assertTrue({"tech", "ib"}.issubset({hit.department for hit in hits}))
        self.assertTrue(details["relationships"])
        hit, = self.adapter.search_documents("пункт 1.1", 1, ("tech",), "implementation")
        self.assertIsInstance(hit, SearchResult)
        self.assertEqual(hit.source_id, f"regulation:{hit.document_id}:1.1")
        self.assertEqual(hit.text, CORPUS["access"][0])
        self.client.app.state.agent.model = FakeModel(draft("ANSWER", hit))
        response = self.message("Что предусмотрено в пункте 1.1?")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"]["source_ids"], [hit.source_id])

    def test_ambiguous_http_applies_followup_filter(self):
        from test_agent_api import FakeModel, draft
        from test_quality_api import QualityTests

        question = "Кто согласует доступ к стенду?"
        hits, details = self.adapter.search_with_context(question, top_k=1, stage="implementation")
        self.assertEqual(len(hits), 1)
        self.assertTrue(details["relationships"])
        ib = self.adapter.search_documents(question, 1, "ib", "implementation")[0]
        tech = self.adapter.search_documents(question, 1, "tech", "implementation")[0]
        model = FakeModel(draft("CLARIFY", ib, tech, field="department", question="Какое подразделение?"),
                          draft("ANSWER", tech))
        self.client.app.state.agent.model = model
        api = QualityTests()
        dialogue = api.dialogue(self.client)
        self.assertEqual(api.send(self.client, dialogue, question)["result"]["result_type"], "CLARIFY")
        result = api.send(self.client, dialogue, "Техническая дирекция")
        self.assertEqual(result["result"]["source_ids"], [tech.source_id])
        self.assertNotIn(ib.source_id, [source["source_id"] for source in model.calls[-1][1]["sources"]])

    def test_numbered_section_with_multiple_chunks_and_unverified_preface(self):
        from docx import Document
        document = Document()
        document.add_paragraph("Вводная часть о спектрометре.")
        document.add_paragraph("9.9. Поверка спектрометра.")
        table = document.add_table(rows=2, cols=1)
        table.cell(0, 0).text = "Параметр спектрометра"
        table.cell(1, 0).text = "Частота спектрометра"
        document.save(self.root / "sources" / "measure.docx")
        update_index(self.root / "sources", self.index, self.encoder)
        hits, context = self.adapter.search_with_context("спектрометра")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].source_id, f"regulation:{hits[0].document_id}:9.9")
        self.assertGreater(len(context["candidates"]), 1)
        self.assertTrue(all(c["section"] == "9.9" for c in context["candidates"]))

    def test_empty_and_failure_differ(self):
        self.assertEqual(self.message("Что предусмотрено в пункте 99.99?").json()["result"]["result_type"], "NO_ANSWER")
        (self.index / "CURRENT").unlink()
        response = self.message("Что предусмотрено в пункте 1.1?")
        self.assertEqual((response.status_code, response.json()["code"]), (503, "retrieval_unavailable"))

    def test_parallel_refresh_and_restart(self):
        def ask(_):
            return self.adapter.search_documents("пункт 1.1")
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(ask, range(8)))
        self.assertTrue(all(hits == results[0] and hits for hits in results))
        (self.root / "sources" / "access.docx").unlink()
        update_index(self.root / "sources", self.index, self.encoder)
        self.assertEqual(ask(None), [])
        self.encoder_factory.assert_called_once()
        marker = (self.index / "CURRENT").read_bytes()
        _, restarted = self.start()
        self.assertEqual(restarted.search_documents("пункт 1.1"), [])
        self.assertTrue(restarted.search_documents("пункт 2.4"))
        self.assertEqual(marker, (self.index / "CURRENT").read_bytes())

    def test_missing_appeal_cannot_publish(self):
        from datetime import datetime, timezone
        from shared.contracts import ApprovedExpertAnswer, ApplicabilityScope, ExpertAnswerMetadata
        key = "expert_answer:synthetic"
        answer = ApprovedExpertAnswer(key, "Пример", "Утверждённый текст",
                                      ExpertAnswerMetadata("synthetic", datetime.now(timezone.utc),
                                                           ApplicabilityScope(), 1))
        self.assertIs(self.adapter.publications, self.client.app.state.quality)
        self.assertEqual(self.adapter.publications.published_revisions((key,)), {})
        with self.assertRaises(RetrievalError):
            self.adapter.upsert_expert_answer(answer)
        self.adapter.delete_expert_answer(key, 2)
        self.adapter.upsert_expert_answer(answer)  # Late delivery after a persisted withdrawal.


    def test_expert_revision_retry_and_restart(self):
        from dataclasses import replace
        from datetime import datetime, timezone
        from shared.contracts import ApprovedExpertAnswer, ApplicabilityScope, ExpertAnswerMetadata

        source_id = "expert_answer:synthetic"
        question = "Каким способом опломбировать резервуар?"
        quality = self.adapter.publications
        answer = ApprovedExpertAnswer(source_id, "Разъяснение", "Используйте красную пломбу.",
                                      ExpertAnswerMetadata("synthetic", datetime.now(timezone.utc),
                                                           ApplicabilityScope("tech", "presale", "тип=учебный"), 1))
        with patch.object(quality, "index_questions", return_value={source_id: question}), \
             patch.object(quality, "published_revisions", return_value={source_id: 1}) as lookup:
            with patch.object(self.encoder, "encode", side_effect=RuntimeError):
                with self.assertRaises(RetrievalError):
                    self.adapter.upsert_expert_answer(answer)
            self.adapter.upsert_expert_answer(answer)
            self.adapter.upsert_expert_answer(answer)
            hit = self.adapter.search_documents(question, 1, "tech", "presale")[0]
            self.assertEqual((hit.text, hit.expert_metadata), (answer.text, answer.metadata))
            self.assertEqual(self.adapter.search_documents(question, department="ib"), [])
            self.assertEqual(self.adapter.search_documents(question, stage="implementation"), [])
            with self.assertRaises(RetrievalError):
                self.adapter.upsert_expert_answer(replace(answer, text="Другой текст"))
            lookup.side_effect = OSError
            with self.assertRaises(RetrievalError):
                self.adapter.search_documents(question)
            lookup.side_effect = None
            lookup.return_value = {}
            self.assertEqual(self.adapter.search_documents(question), [])
            newer = replace(answer, text="Используйте синюю пломбу.", metadata=replace(answer.metadata, revision=2))
            self.adapter.upsert_expert_answer(newer)
            self.adapter.upsert_expert_answer(answer)
            self.adapter.delete_expert_answer(source_id, 1)
            lookup.return_value = {source_id: 2}
            self.assertEqual(self.adapter.search_documents(question)[0].text, newer.text)
            _, restarted = self.start()
            with patch.object(restarted.publications, "published_revisions", return_value={source_id: 2}):
                self.assertEqual(restarted.search_documents(question)[0].text, newer.text)
                restarted.delete_expert_answer(source_id, 3)
                restarted.delete_expert_answer(source_id, 3)
                self.adapter.upsert_expert_answer(newer)
                self.assertEqual(restarted.search_documents(question), [])
                self.assertEqual(self.adapter.search_documents(question), [])
                with self.assertRaises(RetrievalError):
                    self.adapter.upsert_expert_answer(replace(newer, metadata=replace(newer.metadata, revision=3)))


    def test_concurrent_writers_and_metadata_only_revision(self):
        from dataclasses import replace
        from datetime import datetime, timezone
        from shared.contracts import ApprovedExpertAnswer, ApplicabilityScope, ExpertAnswerMetadata

        key = "expert_answer:parallel"
        question = "Кто пломбирует учебный контейнер?"
        _, other = self.start()
        with patch.object(self.adapter.publications, "index_questions", return_value={key: question}), \
             patch.object(other.publications, "index_questions", return_value={key: question}):
            base = ApprovedExpertAnswer(key, "Пример", "Контейнер пломбирует контролёр.",
                                        ExpertAnswerMetadata("parallel", datetime.now(timezone.utc),
                                                             ApplicabilityScope(), 1))
            revisions = [replace(base, metadata=replace(base.metadata, revision=n)) for n in (2, 4, 3, 1)]
            with patch.object(self.encoder, "encode", wraps=self.encoder.encode) as encode:
                with ThreadPoolExecutor(max_workers=4) as pool:
                    jobs = [(self.adapter if n % 2 else other, answer) for n, answer in enumerate(revisions)]
                    list(pool.map(lambda job: job[0].upsert_expert_answer(job[1]), jobs))
                self.assertEqual(encode.call_count, 1)
            with patch.object(other.publications, "published_revisions", return_value={key: 4}):
                hit, = other.search_documents(question, 1)
                self.assertEqual(hit.expert_metadata.revision, 4)
            other.delete_expert_answer(key, 5)
            self.adapter.upsert_expert_answer(revisions[1])
            with patch.object(self.adapter.publications, "published_revisions", return_value={key: 4}):
                self.assertEqual(self.adapter.search_documents(question), [])

    def test_mixed_relevant_sources_stay_separate(self):
        from datetime import datetime, timezone
        from shared.contracts import ApprovedExpertAnswer, ApplicabilityScope, ExpertAnswerMetadata, SourceType

        key = "expert_answer:mixed"
        question = "Кто оформляет доступ к стенду?"
        answer = ApprovedExpertAnswer(key, "Разъяснение", "Доступ к стенду оформляет куратор.",
                                      ExpertAnswerMetadata("mixed", datetime.now(timezone.utc),
                                                           ApplicabilityScope("tech", "implementation"), 1))
        with patch.object(self.adapter.publications, "index_questions", return_value={key: question}), \
             patch.object(self.adapter.publications, "published_revisions", return_value={key: 1}) as lookup:
            self.adapter.upsert_expert_answer(answer)
            hits = self.adapter.search_documents(question, 2, "tech", "implementation")
            self.assertEqual({hit.source_type for hit in hits}, {SourceType.REGULATION, SourceType.EXPERT_ANSWER})
            self.assertEqual({hit.text for hit in hits}, {CORPUS["access"][0], answer.text})
            lookup.return_value = {}
            hit, = self.adapter.search_documents(question, 1, "tech", "implementation")
            self.assertEqual(hit.source_type, SourceType.REGULATION)

    def test_queue_retries_after_failed_sqlite_write(self):
        import sqlite3
        from contextlib import closing
        from test_quality_api import QualityTests, QUESTION

        quality = self.adapter.publications
        api = QualityTests()
        card = api.create_card(self.client)
        key = "expert_answer:" + card["id"]
        api.set_draft(self.client, card)
        with closing(sqlite3.connect(self.adapter.experts.path)) as db, db:
            db.execute("""CREATE TRIGGER reject_write BEFORE INSERT ON entries
                        BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END""")
        try:
            failed = api.action(self.client, card, "approve", expect=202)
            self.assertFalse(failed["publication"]["published"])
            self.assertEqual(self.adapter.search_documents(QUESTION), [])
            with closing(sqlite3.connect(self.adapter.experts.path)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM entries").fetchone()[0], 0)
        finally:
            with closing(sqlite3.connect(self.adapter.experts.path)) as db, db:
                db.execute("DROP TRIGGER reject_write")
        with quality.store.write() as db:
            db.execute("UPDATE quality_revisions SET due=0 WHERE appeal_id=?", (card["id"],))
        quality.process_index(card["id"])
        self.assertEqual(quality.published_revisions((key,)), {key: 1})
        self.assertEqual(self.adapter.search_documents(QUESTION)[0].source_id, key)


@unittest.skipUnless(find_spec("backend") and os.environ.get("TEST_LOCAL_E5") == "1",
                     "requires backend and local E5")
class LocalModelIntegration(unittest.TestCase):
    def test_local_model_factory_and_persisted_index(self):
        from test_quality_api import QualityTests, QUESTION, SECRET
        from time import perf_counter
        from backend.main import create_app
        from fastapi.testclient import TestClient
        from test_agent_api import FakeModel
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build(root, Encoder())
            with patch.dict(os.environ, RETRIEVAL_INDEX=str(root / "index")):
                app = create_app(retrieval_factory=retrieval_factory, model=FakeModel(),
                                 workers=False, quality_secret=SECRET, db_path=root / "state.db")
                with TestClient(app) as client:
                    self.assertEqual(client.get("/health").json()["retrieval"], "configured")
                    search = app.state.quality.retrieval
                    self.assertEqual(search.search_documents("пункт 1.1")[0].text, CORPUS["access"][0])
                    api = QualityTests()
                    card = api.create_card(client)
                    key = "expert_answer:" + card["id"]
                    times = {}
                    for revision, text in enumerate(("Карточку выдаёт смотритель.", "Карточку выдаёт диспетчер."), 1):
                        api.set_draft(client, card, text)
                        if revision == 1:
                            self.assertNotIn(key, [hit.source_id for hit in search.search_documents(QUESTION)])
                        start = perf_counter()
                        api.action(client, card, "approve")
                        times["add_ms" if revision == 1 else "update_ms"] = (perf_counter() - start) * 1000
                        start = perf_counter()
                        hit = next(hit for hit in search.search_documents(QUESTION) if hit.source_id == key)
                        times[f"search_revision_{revision}_ms"] = (perf_counter() - start) * 1000
                        self.assertEqual((hit.text, hit.expert_metadata.revision), (text, revision))
                        api.put_expert_into_model(app.state.agent.model, card, text)
                        reply = api.send(client, api.dialogue(client))
                        self.assertEqual(reply["result"]["source_ids"], [key])
                    self.assertIn(key, [hit.source_id for hit in search.search_documents(
                        "Кто отвечает за выдачу жетонов для учебного телескопа?")])
                    start = perf_counter()
                    api.action(client, card, "revoke")
                    times["revoke_ms"] = (perf_counter() - start) * 1000
                    print("EXPERT_E5_HTTP_MS", {k: round(v, 2) for k, v in times.items()})
                    self.assertNotIn(key, [hit.source_id for hit in search.search_documents(QUESTION)])
                restarted = create_app(retrieval_factory=retrieval_factory, model=FakeModel(),
                                       workers=False, db_path=root / "state.db")
                with TestClient(restarted):
                    search = restarted.state.quality.retrieval
                    self.assertNotIn(key, [hit.source_id for hit in search.search_documents(QUESTION)])
