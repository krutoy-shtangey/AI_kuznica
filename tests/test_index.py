import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from docx import Document
from docx.oxml import OxmlElement
import numpy as np

from retrieval.chunker import Config, chunk_document
from retrieval.embeddings import Encoder
from retrieval.index import IndexErrorLocal, load_index, update_index
from retrieval.loader import load_directory
from shared.contracts import ApplicabilityScope


class TestEncoder:
    model, revision, dimension, limit = "test", "v1", 4, 512

    def offsets(self, text):
        return [m.span() for m in re.finditer(r"\w+|[^\w\s]", text)]

    def count(self, text):
        return 2 + len(self.offsets(text))

    def encode(self, inputs):
        self.calls += len(inputs)
        return np.tile(np.array([1, 0, 0, 0], dtype=np.float32), (len(inputs), 1))

    def __init__(self):
        self.calls = 0


class IndexTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.docs = Path(temp.name) / "docs"
        self.docs.mkdir()
        self.index = Path(temp.name) / "index"
        self.encoder = TestEncoder()

    def build(self, **kwargs):
        return update_index(self.docs, self.index, self.encoder, **kwargs)

    def save(self, name, *paragraphs):
        doc = Document()
        for text in paragraphs:
            doc.add_paragraph(text)
        doc.save(self.docs / name)

    def test_source_spans_structure_and_context(self):
        doc = Document()
        doc.add_heading("Правила", 1)
        doc.add_paragraph("1. Подготовка")
        doc.add_paragraph("Если макет исправен, выполните действия.")
        doc.add_paragraph("Осмотреть", style="List Bullet")
        doc.add_paragraph("Проверить", style="List Bullet 2")
        doc.add_paragraph("1.1) Исключения")
        doc.add_paragraph("")
        doc.add_heading("Архив", 1)
        doc.add_paragraph("Завершение")
        doc.save(self.docs / "a.docx")
        loaded = load_directory(self.docs).documents[0]
        chunks = chunk_document(loaded, self.encoder)
        self.assertEqual(chunks, chunk_document(loaded, self.encoder))
        self.assertEqual(len({c["source_id"] for c in chunks}), len(chunks))
        first = next(c for c in chunks if c["section"] == "1")
        for needle in ("Если", "Осмотреть", "Проверить"):
            self.assertIn(needle, first["text"])
        self.assertNotIn("Исключения", first["text"])
        self.assertIn("Правила", first["index_text"])
        self.assertEqual(first["status"], "unknown")
        blocks = {b.order: b for b in loaded.elements()}
        for chunk in chunks:
            self.assertTrue(chunk["text"].strip())
            self.assertEqual(chunk["document_sha256"], loaded.metadata.checksum)
            source = [blocks[s["element"]].text[s["start"]:s["end"]] for s in chunk["spans"]]
            self.assertEqual(chunk["text"], "\n".join(source))

    def test_long_clause_coverage_overlap_and_budget(self):
        text = "2. " + "Этап испытания завершён. " * 300
        self.save("long.docx", text)
        parts = chunk_document(load_directory(self.docs).documents[0], self.encoder, Config(80, 96, 12))
        self.assertGreater(len(parts), 2)
        end = 0
        for p in parts:
            self.assertLessEqual(self.encoder.count(p["index_text"]), 96)
            span = p["spans"][0]
            self.assertLessEqual(span["start"], end)
            self.assertGreater(span["end"], end)
            self.assertEqual(p["text"], text[span["start"]:span["end"]])
            end = span["end"]
        self.assertEqual(end, len(text))
        self.assertLess(parts[1]["spans"][0]["start"], parts[0]["spans"][0]["end"])

    def test_table_headers_and_source_coordinates(self):
        doc = Document()
        table = doc.add_table(4, 2)
        table.rows[0]._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
        table.cell(0, 0).text, table.cell(0, 1).text = "Этап", "Ответственный"
        table.cell(1, 0).merge(table.cell(2, 0)).text = "Сборка"
        for row in range(1, 4):
            table.cell(row, 1).text = ("Инженер " * 60) + str(row)
        doc.save(self.docs / "table.docx")
        chunks = chunk_document(load_directory(self.docs).documents[0], self.encoder, Config(35, 48, 5))
        data = [c for c in chunks if "Инженер" in c["text"]]
        self.assertGreater(len(data), 3)
        for c in data:
            self.assertIn("Ответственный", c["index_text"])
            self.assertTrue(c["spans"][-1]["cells"])
            self.assertLessEqual(c["tokens"], 48)

    def test_incremental_lifecycle_and_reload(self):
        self.save("a.docx", "1. Первый пункт")
        self.assertEqual(self.build()["new_chunks"], 1)
        old = load_index(self.index)
        self.assertEqual(old.vectors.shape, (1, 4))
        self.assertEqual(self.build()["reused_embeddings"], 1)
        self.assertEqual(self.encoder.calls, 1)
        self.assertEqual(load_index(self.index).manifest, old.manifest)
        self.save("b.docx", "2. Второй пункт")
        self.assertEqual(self.build()["new_chunks"], 1)
        self.save("a.docx", "3. Изменение")
        stats = self.build()
        self.assertEqual((stats["updated_chunks"], stats["reused_embeddings"]), (1, 1))
        (self.docs / "b.docx").unlink()
        self.assertEqual(self.build()["parsed_documents"], 0)
        saved = load_index(self.index)
        self.assertEqual([c["text"] for c in saved.chunks], ["3. Изменение"])
        for field in ("revision", "model"):
            setattr(self.encoder, field, "changed")
            self.assertEqual(self.build()["reused_embeddings"], 0)
        self.assertEqual(self.build(config=Config(300, 512, 30))["updated_chunks"], 1)
        self.assertEqual(self.build(force=True)["reused_embeddings"], 0)

    def test_read_error_and_interrupted_publish(self):
        self.save("a.docx", "Текст")
        self.build()
        pointer = (self.index / "CURRENT").read_bytes()
        (self.docs / "a.docx").write_bytes(b"broken")
        with self.assertRaises(IndexErrorLocal):
            self.build()
        self.save("a.docx", "Изменение")
        with patch("retrieval.index.os.replace", side_effect=OSError):
            with self.assertRaises(OSError):
                self.build()
        self.assertEqual(pointer, (self.index / "CURRENT").read_bytes())
        self.assertEqual(load_index(self.index).chunks[0]["text"], "Текст")

    def test_corruption_and_empty(self):
        with self.assertRaises(IndexErrorLocal):
            load_index(self.index)
        self.build()
        empty = load_index(self.index)
        self.assertEqual(empty.vectors.shape, (0, 4))
        folder = self.index / empty.manifest["generation"]
        (folder / "embeddings.npy").write_bytes(b"broken")
        with self.assertRaises(IndexErrorLocal):
            self.build()
        self.build(force=True)
        self.assertEqual(load_index(self.index).chunks, [])

    def test_invalid_encoder_output_rejected(self):
        self.save("a.docx", "Текст")
        for shape in ((2, 4), (1, 3), (1, 4)):
            with patch.object(self.encoder, "encode", return_value=np.zeros(shape, np.float32)):
                with self.assertRaises(ValueError):
                    self.build()
        self.assertFalse((self.index / "CURRENT").exists())

    def test_verified_metadata_refresh_without_embedding(self):
        self.save("scope.docx", "Условия")
        self.build()
        stats = self.build(scopes={"scope.docx": ApplicabilityScope("ib", "presale")})
        chunk = load_index(self.index).chunks[0]
        self.assertEqual((chunk["department"], chunk["stage"]), ("ib", "presale"))
        self.assertEqual((stats["reused_embeddings"], self.encoder.calls), (1, 1))

    def test_oversized_heading(self):
        doc = Document()
        title = "Синтетический заголовок " * 150
        doc.add_heading(title, 1)
        doc.add_paragraph("Условие применения")
        doc.save(self.docs / "h.docx")
        parts = chunk_document(load_directory(self.docs).documents[0], self.encoder, Config(60, 80, 8))
        self.assertGreater(len(parts), 2)
        self.assertTrue(all(c["tokens"] <= 80 and c["headings"][0][1] == title for c in parts))

    def test_cli_check_without_model_or_document_content(self):
        self.save("private-filename.docx", "private-text")
        self.build()
        run = subprocess.run([sys.executable, "-B", "-m", "retrieval.index", "--check",
                              "--index", str(self.index)], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0)
        self.assertEqual(run.stderr, "")
        self.assertNotIn("private", run.stdout)
        self.assertEqual(json.loads(run.stdout)["chunks"], 1)

    @unittest.skipUnless(os.environ.get("TEST_LOCAL_E5") == "1", "requires local E5 weights")
    def test_real_russian_cpu(self):
        self.encoder = Encoder()
        self.save("демо.docx", "1. Испытание макета. " + "Инженер измеряет напряжение. " * 140,
                  "2. Отпуск согласует руководитель.")
        result = self.build()
        saved = load_index(self.index)
        query = self.encoder.encode_queries(["Кто согласует отпуск?"])
        self.assertEqual(query.shape, (1, 384))
        scores = saved.vectors @ query[0]
        self.assertEqual(saved.chunks[int(scores.argmax())]["section"], "2")
        self.assertLessEqual(max(c["tokens"] for c in saved.chunks), 512)
        with self.assertRaises(ValueError):
            self.encoder.encode_queries(["напряжение " * 800])
        second = self.build()
        self.assertEqual(second["reused_embeddings"], result["chunks"])
        print(json.dumps(dict(build=result, repeat=second, dimension=384,
                              matching_similarity=float(scores.max()))))
