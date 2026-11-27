import json
import shutil
import sys
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch
import tempfile
import unittest
from datetime import date
from pathlib import Path

from docx import Document

from retrieval.index import load_index, publish, update_index
from retrieval.loader import load_directory
from retrieval.metadata import extract, lifecycle, main, policy, profile, read_registry, statistics
from retrieval.search import RegulationSearch, Settings
from shared.contracts import SearchResult, SourceStatus
from test_index import TestEncoder


class MetadataTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.docs = self.root / "docs"
        self.docs.mkdir()
        self.index = self.root / "index"
        self.registry = self.root / "registry.json"
        self.rows = {}
        self.encoder = TestEncoder()

    def doc(self, name, *lines):
        doc = Document()
        for line in lines:
            doc.add_paragraph(line)
        doc.save(self.docs / name)
        return next(d for d in load_directory(self.docs).documents if d.metadata.filename == name)

    def review(self, doc, fields, sections=None):
        meta = doc.metadata
        self.rows[meta.filename] = dict(id=meta.filename.split(".")[0], sha256=meta.checksum,
                                        reviewer="Учебный эксперт", basis="Протокол DEMO",
                                        fields=fields, sections=sections or {})

    def build(self):
        self.registry.write_text(json.dumps(dict(format=1, documents=self.rows)), encoding="utf-8")
        return update_index(self.docs, self.index, self.encoder, registry=self.registry)

    def search(self, history=False):
        return RegulationSearch(self.index, Settings(mode="bm25", include_history=history))

    def test_extraction_evidence_and_unknown(self):
        d = self.doc("a.docx", "Название: Учебный порядок", "Регистрационный номер: ТЕСТ-1",
                     "Редакция 2.3", "Утверждён 07.01.2025", "Вступает в силу с 01.02.2025",
                     "Действует по 20.02.2026 включительно", "Статус: действует",
                     "Подразделение: Дирекция ИБ", "Этап проекта: пресейловая деятельность",
                     "Направление бизнеса: Стенды", "Область применения: Приёмка",
                     "Другой регламент редакции 8 обсуждался в Технической дирекции.")
        records = extract(d)
        fields = {item["field"]: item["value"] for item in records}
        self.assertEqual([fields[k] for k in ("approved_on", "valid_from", "valid_until", "version")],
                         ["2025-01-07", "2025-02-01", "2026-02-20", "2.3"])
        self.assertEqual((fields["department"], fields["stage"]), ("ib", "presale"))
        self.assertEqual(len(records), 11)
        for row in records:
            self.assertEqual(row["quote"], d.blocks[row["element"]].text)
            self.assertEqual(row["document_id"], d.metadata.document_id)
        rule = policy(profile(d.metadata, records, {}), None)
        self.assertEqual(lifecycle(rule, date(2025, 5, 1)), "unknown")
        self.assertEqual(lifecycle(rule, date(2027, 5, 1)), "expired")
        plain = self.doc("b.docx", "См. регламент 42. Сотрудник ДБРЦТ передаёт макет.")
        self.assertEqual(extract(plain), [])
        self.assertEqual(statistics([profile(plain.metadata, [], {})])["status_unknown"], 1)

    def test_document_and_partial_replacement_scope_and_dates(self):
        d = self.doc("a.docx", "1. Макет испытывает инженер.", "2.1. Макет проверяет аналитик.",
                     "3. Макет осматривает мастер.", "4. Макет принимает эксперт.")
        self.review(d, dict(state="approved", department="tech", stage="implementation", version="2"),
                    {"2": dict(department="ib", stage="presale"),
                     "3": dict(state="replaced", replaced_by="demo-v3"),
                     "4": dict(valid_from="2999-01-01")})
        self.build()
        search = self.search()
        found = search.search_documents("макет", department=("tech", "ib"))
        self.assertEqual({r.section for r in found}, {"1", "2.1"})
        item, = search.search_documents("макет", department="ib", stage="presale")
        self.assertIsInstance(item, SearchResult)
        self.assertEqual((item.status, item.source_id), (SourceStatus.APPROVED, "regulation:a:2.1"))
        self.assertEqual(item.text, d.blocks[1].text)
        _, detail = self.search(True).search_with_context("макет")
        self.assertEqual({c["lifecycle"] for c in detail["candidates"]}, {"approved", "replaced", "future"})

    def test_old_new_conflict_and_timed_replacement(self):
        for name, fields, extra in [
                ("old", dict(state="revoked"), []),
                ("expired", dict(state="approved", valid_until="2001-12-31"), []),
                ("pending", dict(state="approved", replaced_by="next", change_on="2999-01-01"), []),
                ("contradiction", dict(state="approved"), ["Статус: отменён"]),
                ("newer", dict(version="999"), [])]:
            d = self.doc(name + ".docx", *extra, f"1. Макет согласует комиссия ({name}).")
            self.review(d, fields)
        self.build()
        found = self.search().search_documents("макет")
        self.assertEqual({r.document_id: r.status.value for r in found},
                         {"pending": "approved", "newer": "unknown"})
        rule = next(c for c in load_index(self.index).chunks if c["document_id"] == "pending")["regulation"]
        self.assertEqual(lifecycle(rule, date(2999, 1, 1)), "replaced")
        _, context = self.search(True).search_with_context("макет")
        self.assertEqual({c["lifecycle"] for c in context["candidates"]},
                         {"approved", "expired", "revoked", "needs_review", "unknown"})

    def test_alternatives_remain_with_small_limit(self):
        for name, dept, job in (("a", "tech", "мастер"), ("b", "tech", "инженер"), ("c", "ib", "аналитик")):
            d = self.doc(name + ".docx", "1. Макет согласует " + job)
            self.review(d, dict(process="Приёмка", department=dept, state="approved"))
        self.build()
        hits, info = self.search().search_with_context("макет", top_k=1)
        self.assertEqual(len(hits), 1)
        self.assertEqual(len(info["candidates"]), 3)
        self.assertEqual({r["reason"] for r in info["relationships"]},
                         {"review_same_process", "different_applicability"})
        with self.assertRaises(ValueError):
            self.search().search_documents("макет", department="unregistered")

    def test_checksum_binding_registry_update_and_atomic_failure(self):
        d = self.doc("a.docx", "1. Осмотр макета")
        self.review(d, dict(state="approved", department="tech"))
        self.build()
        calls = self.encoder.calls
        search = self.search()
        self.rows["a.docx"]["fields"]["department"] = "ib"
        stats = self.build()
        self.assertEqual((stats["reused_embeddings"], self.encoder.calls), (1, calls))
        self.assertEqual(search.search_documents("осмотр", department="tech"), [])
        self.assertEqual(search.search_documents("осмотр")[0].department, "ib")
        pointer = (self.index / "CURRENT").read_bytes()
        self.rows["a.docx"]["fields"]["valid_from"] = "not-a-date"
        with self.assertRaises(ValueError):
            self.build()
        self.assertEqual(pointer, (self.index / "CURRENT").read_bytes())
        del self.rows["a.docx"]["fields"]["valid_from"]
        self.doc("a.docx", "1. Осмотр другого макета")
        self.build()
        self.assertEqual(search.search_documents("осмотр"), [])
        hist, info = self.search(True).search_with_context("осмотр")
        self.assertIsNone(hist[0].department)
        self.assertEqual(hist[0].status, SourceStatus.UNKNOWN)
        self.assertEqual(info["candidates"][0]["lifecycle"], "needs_review")

    def test_unsigned_registry_and_unknown_abbreviation(self):
        d = self.doc("a.docx", "Подразделение: ЯЯЯ")
        self.assertIsNone(extract(d)[0]["value"])
        self.assertEqual(extract(d, {"department": {"яяя": "tech"}})[0]["value"], "tech")
        self.review(d, dict(state="approved"))
        self.rows["a.docx"]["basis"] = ""
        with self.assertRaises(ValueError):
            self.build()
        self.assertFalse((self.index / "CURRENT").exists())
        sample = Path(__file__).resolve().parents[1] / "docs" / "registry.example.json"
        self.assertEqual(len(read_registry(sample)), 1)

    def test_copy_rename_and_local_draft(self):
        d = self.doc("old.docx", "Версия: 1", "1. Осмотр макета")
        self.review(d, dict(state="revoked", version="1"))
        self.build()
        shutil.copyfile(self.docs / "old.docx", self.docs / "a.docx")
        self.assertEqual(self.build()["documents"], 1)
        (self.docs / "old.docx").unlink()
        self.build()
        self.assertEqual(self.search().search_documents("осмотр"), [])
        self.assertEqual(self.search(True).search_documents("осмотр")[0].document_id, "old")
        draft = self.root / "draft.json"
        argv = ["metadata", "--documents", str(self.docs), "--registry", str(self.registry), "--draft", str(draft)]
        output = StringIO()
        with patch.object(sys, "argv", argv), redirect_stdout(output):
            self.assertEqual(main(), 0)
        self.assertNotIn("a.docx", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["version_known"], 1)
        row = read_registry(draft)["a.docx"]
        self.assertEqual(row["fields"], {})
        self.assertEqual(row["evidence"][0]["field"], "version")

    def test_v1_migration_reuses_embedding_and_extracts_evidence(self):
        self.doc("a.docx", "Версия: DEMO", "1. Осмотр макета")
        self.build()
        previous = load_index(self.index)
        legacy = dict(previous.manifest, version=1, algorithm=1)
        del legacy["profiles"], legacy["registry_managed"]
        chunks = [{k: v for k, v in c.items() if k != "regulation"} for c in previous.chunks]
        publish(self.index, legacy, chunks, previous.vectors, previous)
        calls = self.encoder.calls
        metrics = self.build()
        self.assertEqual(metrics["reused_embeddings"], len(chunks))
        self.assertEqual(self.encoder.calls, calls)
        self.assertEqual(load_index(self.index).manifest["version"], 2)

    def test_inherited_date_conflict_and_table_declaration(self):
        doc = Document()
        table = doc.add_table(1, 2)
        table.cell(0, 0).text, table.cell(0, 1).text = "Регистрационный номер", "СИНТ-42"
        doc.add_paragraph("1. Осмотр макета")
        doc.save(self.docs / "a.docx")
        d, = load_directory(self.docs).documents
        self.assertEqual(extract(d)[0]["value"], "СИНТ-42")
        self.review(d, {"valid_until": "2050-01-01"}, {"1": {"valid_from": "2051-01-01"}})
        self.build()
        self.assertEqual(self.search().search_documents("макет"), [])
        _, info = self.search(True).search_with_context("макет")
        self.assertEqual(info["candidates"][0]["lifecycle"], "needs_review")

    def test_explicit_change_dates_keep_their_event_type(self):
        for label, expected in (("Дата замены", "replaced"), ("Дата отмены", "revoked")):
            d = self.doc("a.docx", label + ": 01.02.2025")
            rule = policy(profile(d.metadata, extract(d), None), None)
            self.assertEqual(lifecycle(rule, date(2025, 1, 31)), "unknown")
            self.assertEqual(lifecycle(rule, date(2025, 2, 1)), expected)
