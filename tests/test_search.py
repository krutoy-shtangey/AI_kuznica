"""Synthetic retrieval acceptance tests and opt-in offline CPU benchmark."""
from dataclasses import replace
import json
import os
from pathlib import Path
from statistics import mean
import tempfile
import sys
from contextlib import redirect_stdout
from io import StringIO
from time import perf_counter
import unittest
from unittest.mock import patch

from docx import Document
import numpy as np

from retrieval.embeddings import Encoder, MODEL_ROOT
from retrieval.chunker import Config
from retrieval.index import update_index
from retrieval.search import LocalCrossEncoder, LongQuery, RegulationSearch, Settings, fuse, main
from shared.contracts import ApplicabilityScope, RetrievalError, SearchResult, SourceStatus, SourceType

# Source labels and judgments are specified before ranking. Each file yields one chunk.
CORPUS = {
    "access": ("1.1. Доступ к стенду оформляет инженер. Заявку утверждает руководитель смены.", "tech", "implementation"),
    "review": ("2.4. Доступ к стенду согласует дежурный аналитик ИБ после проверки полномочий.", "ib", "implementation"),
    "offer": ("3.2. Смету и коммерческое предложение до начала проекта готовит экономист.", "tech", "presale"),
    "backup": ("4.1. Резервную копию базы восстанавливает администратор. Если копия повреждена, восстановление запрещено.", None, None),
    "leave": ("5.3. Ежегодный отпуск сотрудника утверждает начальник отдела кадров.", None, None),
    "sample": ("6.2. Испытания опытного образца проводит метролог. Результаты заносят в протокол КЛМ.", "dbrct", "implementation"),
    "badge": ("7.1. Утерянный пропуск блокирует дежурный охранник до выдачи нового удостоверения.", "ib", None),
    "bill": ("8.2. Оплату счёта поставщика проводит бухгалтер после подписания накладной.", None, None),
}
ALIASES = {"бэкап": "резервная копия", "бэкапа": "резервная копия"}
DEV = [
    ("Кому поручена оплата счета поставщика?", {"bill"}),
    ("Как согласовать отпуск сотрудника?", {"leave"}),
    ("Как вернуть базу из бэкапа?", {"backup"}),
    ("Кто выдаёт разрешение работать на стенде?", {"access", "review"}),
    ("Как приготовить свекольный суп?", set()),
    ("В какой гостинице жить во время командировки?", set()),
    ("Как оформляют компенсацию стоматологии?", set()),
]
EVAL = [
    ("Кто оформляет доступ к стенду?", {"access", "review"}),
    ("Кто отвечает за отпуск?", {"leave"}),
    ("Кому поручить восстановление из бэкапа?", {"backup"}),
    ("КЛМ", {"sample"}),
    ("Что предусмотрено в пункте 3.2?", {"offer"}),
    ("Кто перечисляет деньги контрагенту за товар?", {"bill"}),
    ("Я потерял карточку для прохода. К кому обратиться?", {"badge"}),
    ("Кто испытывает опытный образец?", {"sample"}),
    ("Какая завтра температура воздуха?", set()),
    ("Где парковать личный автомобиль?", set()),
    ("Кто оплачивает обед сотрудника?", set()),
]


def build(root, encoder):
    docs = root / "sources"
    docs.mkdir(exist_ok=True)
    scopes = {}
    for key, (text, dept, stage) in CORPUS.items():
        doc = Document()
        doc.add_paragraph(text)
        doc.save(docs / (key + ".docx"))
        scopes[key + ".docx"] = ApplicabilityScope(dept, stage)
    update_index(docs, root / "index", encoder, scopes=scopes)


class Fake:
    model, revision, dimension, limit = "fake", "1", 3, 512

    def count(self, text):
        return len(text.split()) + 2

    def encode(self, texts):
        return np.array([[1, 0, 0] if "копи" in s else [0, 1, 0] for s in texts], np.float32)

    def encode_queries(self, texts):
        return np.array([[1, 0, 0] if "архив" in s else [0, 0, 1] for s in texts], np.float32)


class SearchTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.enc = Fake()
        build(self.root, self.enc)
        self.settings = Settings(mode="bm25", lexical_coverage=.30)
        self.index = self.root / "index"

    def search(self, **kwargs):
        return RegulationSearch(self.index, replace(self.settings, **kwargs), encoder=self.enc, aliases=ALIASES)

    def test_morphology_reference_synonym_and_contract(self):
        s = self.search()
        for query, expected in [("восстановление бэкапа", "backup"), ("клм", "sample"),
                                ("пункт 3.2", "offer"), ("счета поставщиков", "bill")]:
            r = s.search_documents(query)[0]
            self.assertIsInstance(r, SearchResult)
            self.assertEqual(r.text, CORPUS[expected][0])
            self.assertEqual(r.source_type, SourceType.REGULATION)
            self.assertIsNotNone(r.document_id)
            self.assertTrue(r.source_id.startswith("regulation:"))
            self.assertEqual(r.status, SourceStatus.UNKNOWN)
            self.assertIsNone(r.source_url)
        self.assertEqual(s.search_documents("пункт 99.2"), [])

    def test_scopes_and_ambiguity(self):
        s = self.search()
        question = "доступ к стенду"
        self.assertEqual({r.department for r in s.search_documents(question)}, {"ib", "tech"})
        self.assertEqual([r.department for r in s.search_documents(question, department="ib")], ["ib"])
        self.assertEqual(len(s.search_documents(question, department=("tech", "ib"))), 2)
        self.assertEqual(s.search_documents(question, stage="presale"), [])
        self.assertIsNone(s.search_documents("бэкап", department="ib", stage="presale")[0].stage)

    def test_semantic_budget_and_rrf(self):
        s = self.search(mode="semantic")
        self.assertEqual(s.search_documents("Вернуть архив")[0].document, "backup.docx")
        self.assertFalse(self.search(mode="hybrid", context_chars=4).search_documents("Вернуть архив"))
        self.assertEqual(fuse([(2, [0, 1, 0]), (1, [1])], 2), {0: 2/3, 1: 2/4 + 1/3})
        items = self.search(mode="hybrid").search_documents("архив бэкап")
        self.assertEqual(len(items), len({r.source_id for r in items}))
        with self.assertRaises(LongQuery):
            s.search_documents("архив " * 520)

    def test_input_empty_unknown_errors_and_reload(self):
        s = self.search()
        with self.assertRaises(ValueError):
            s.search_documents(" ")
        with self.assertRaises(ValueError):
            s.search_documents("макет", top_k=0)
        self.assertEqual(s.search_documents("астероиды"), [])
        initial = s.lexical
        s.search_documents("смета")
        self.assertIs(initial, s.lexical)
        docs = self.root / "empty"
        docs.mkdir()
        update_index(docs, self.index, self.enc)
        self.assertEqual(s.search_documents("смета"), [])
        self.assertIsNot(initial, s.lexical)
        (self.index / "CURRENT").write_text("corrupt")
        with self.assertRaises(RetrievalError):
            s.search_documents("смета")
        with self.assertRaises(RetrievalError):
            RegulationSearch(self.root / "absent")

    def test_incompatible_model_and_failed_inference(self):
        s = self.search(mode="hybrid")
        with patch.object(self.enc, "encode_queries", side_effect=OSError):
            with self.assertRaises(RetrievalError):
                s.search_documents("стенд")
        self.enc.revision = "different"
        with self.assertRaises(RetrievalError):
            self.search(mode="semantic")

    def test_optional_reranker_and_ties(self):
        class Tied:
            def score(self, question, passages):
                return [1.] * len(passages)
        s = RegulationSearch(self.index, self.settings, reranker=Tied())
        first = s.search_documents("стенд")
        self.assertEqual(len(first), 2)
        self.assertEqual([r.source_id for r in first], sorted(r.source_id for r in first))
        self.assertEqual(first, s.search_documents("стенд"))
        self.assertGreater(s.metrics.reranking_ms, 0)

    def test_neighbor_exception_is_separate_and_budgeted(self):
        docs = self.root / "conditions"
        docs.mkdir()
        doc = Document()
        doc.add_paragraph("2. Осмотр прибора выполняет инженер стенда до начала смены.")
        doc.add_paragraph("Если пломба повреждена, работы запрещены.")
        doc.save(docs / "rule.docx")
        update_index(docs, self.index, self.enc, Config(target=16, overlap=2))
        items = self.search(neighbors=True).search_documents("инженер")
        self.assertEqual(len(items), 2)
        self.assertEqual({i.section for i in items}, {"2"})
        self.assertTrue(any("запрещены" in i.text for i in items))
        self.assertEqual(len({i.source_id for i in items}), 2)

    def test_cli_stats_only(self):
        out = StringIO()
        with (patch.object(sys, "argv", ["search", "--mode", "bm25", "--index", str(self.index)]),
              patch("retrieval.search.getpass", return_value="метролог"), redirect_stdout(out)):
            self.assertEqual(main(), 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["returned"], 1)
        self.assertEqual(report["missing_department"], 3)
        self.assertNotIn("results", report)
        self.assertNotIn("метролог", out.getvalue())
        self.assertNotIn("sample.docx", out.getvalue())


def evaluate(search):
    summary = {key: [] for key in ("recall", "rr", "unknown_fp", "ms", "chars", "n",
                                    "embedding_ms", "bm25_ms", "semantic_ms", "fusion_ms", "reranking_ms")}
    for question, gold in EVAL:
        items = search.search_documents(question, top_k=5)
        names = [Path(r.document).stem for r in items]
        if gold:
            summary["recall"].append(len(gold.intersection(names)) / len(gold))
            summary["rr"].append(next((1/i for i, name in enumerate(names, 1) if name in gold), 0))
        else:
            summary["unknown_fp"].append(int(bool(names)))
        if len(gold) > 1:
            ambiguity = len(gold.intersection(names)) / len(gold)
        summary["n"].append(len(items))
        summary["chars"].append(search.metrics.context_chars)
        summary["ms"].append(search.metrics.total_ms)
        for key in ("embedding_ms", "bm25_ms", "semantic_ms", "fusion_ms", "reranking_ms"):
            summary[key].append(getattr(search.metrics, key))
    return dict(**{key: round(mean(values), 4) for key, values in summary.items()},
                max_ms=round(max(summary["ms"]), 3), ambiguous_recall=ambiguity,
                init_ms=round(search.cold_ms, 2))


@unittest.skipUnless(os.environ.get("TEST_LOCAL_E5") == "1", "requires local E5")
class OfflineEvaluation(unittest.TestCase):
    def test_real_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            started = perf_counter()
            enc = Encoder()
            print("E5_INIT_MS", round((perf_counter() - started) * 1000, 2))
            build(root, enc)
            for mode in ("bm25", "semantic", "hybrid"):
                search = RegulationSearch(root / "index", Settings(mode=mode), encoder=enc, aliases=ALIASES)
                search.search_documents("Кто проводит испытания опытного образца?")
                print("FIRST_QUERY_MS", mode, round(search.metrics.total_ms, 2))
                if mode != "hybrid":
                    grid = (.20, .35, .50, .65, .80) if mode == "bm25" else (.74, .76, .78, .80, .82, .84, .86)
                    key = "lexical_coverage" if mode == "bm25" else "cosine_floor"
                    for threshold in grid:
                        search.settings = replace(search.settings, **{key: threshold})
                        tp = fp = fn = 0
                        for q, gold in DEV:
                            predicted = {Path(r.document).stem for r in search.search_documents(q)}
                            tp += len(predicted & gold)
                            fp += len(predicted - gold)
                            fn += len(gold - predicted)
                        print("DEV", mode, threshold, tp, fp, fn)
                    search.settings = Settings(mode=mode)
                result = evaluate(search)
                print("EVAL", mode, json.dumps(result))
                self.assertGreater(result["recall"], 0)
                self.assertEqual(result["ambiguous_recall"], 1)
                self.assertEqual(result["unknown_fp"], 0)
            if os.environ.get("TEST_LOCAL_RERANK") == "1":
                directory = MODEL_ROOT / "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1/1427fd652930e4ba29e8149678df786c240d8825"
                started = perf_counter()
                search.reranker = LocalCrossEncoder(directory)
                print("RERANK_INIT_MS", round((perf_counter() - started) * 1000, 2))
                params = search.reranker.model.model.parameters()
                print("RERANK_WEIGHT_MIB", round(sum(p.numel() * p.element_size() for p in params) / 2**20, 1))
                print("EVAL rerank", json.dumps(evaluate(search)))
