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
            app = create_app(retrieval_factory=retrieval_factory, db_path=self.root / "test.sqlite3",
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

    def test_expert_outbox_operations_fail_explicitly(self):
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
        with self.assertRaises(RetrievalError):
            self.adapter.delete_expert_answer(key, 2)


@unittest.skipUnless(find_spec("backend") and os.environ.get("TEST_LOCAL_E5") == "1",
                     "requires backend and local E5")
class LocalModelIntegration(unittest.TestCase):
    def test_local_model_factory_and_persisted_index(self):
        from backend.main import create_app
        from fastapi.testclient import TestClient
        from test_agent_api import FakeModel
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build(root, Encoder())
            with patch.dict(os.environ, RETRIEVAL_INDEX=str(root / "index")):
                app = create_app(retrieval_factory=retrieval_factory, model=FakeModel(),
                                 workers=False, db_path=root / "state.db")
                with TestClient(app) as client:
                    self.assertEqual(client.get("/health").json()["retrieval"], "configured")
                    search = app.state.quality.retrieval
                    self.assertEqual(search.search_documents("пункт 1.1")[0].text, CORPUS["access"][0])
