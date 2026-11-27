"""Fixed synthetic Issue #8 benchmark. Enable with TEST_RETRIEVAL_EVAL=1."""
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from statistics import mean
from tempfile import TemporaryDirectory
from time import perf_counter
import unittest
from unittest.mock import patch

from docx import Document
import numpy as np

from retrieval.agent_adapter import AgentRetrieval
from retrieval.bm25 import BM25
from retrieval.embeddings import Encoder, MODEL_ROOT
from retrieval.index import update_index
from retrieval.search import LocalCrossEncoder, Settings
from shared.contracts import ApplicabilityScope, ApprovedExpertAnswer, ExpertAnswerMetadata


REGULATIONS = {
    "stand-tech": ("1.1", "Доступ к учебному стенду выдаёт инженер смены. Инженер вносит заявку в журнал.", "tech", "implementation"),
    "stand-ib": ("1.2", "Доступ к учебному стенду согласует аналитик ИБ после проверки полномочий.", "ib", "implementation"),
    "estimate": ("2.1", "Коммерческое предложение и смету на этапе пресейла рассчитывает экономист.", "tech", "presale"),
    "backup": ("3.1", "Администратор восстанавливает базу из резервной копии. Повреждённые копии использовать запрещено.", "tech", "implementation"),
    "inspection": ("4.1", "Приёмку опытного образца проводит метролог. Результат испытаний заносится в протокол КЛМ.", "dbrct", "implementation"),
    "badge": ("5.1", "Охранник блокирует потерянный пропуск и оформляет новое удостоверение для прохода.", "ib", None),
    "payment": ("6.1", "Оплату счёта поставщика проводит бухгалтер после подписания накладной.", None, None),
    "leave": ("7.1", "Ежегодный отпуск сотрудника согласует начальник отдела кадров.", None, None),
    "meter": ("8.1", "Поверку спектрометра выполняет техник через каждые шесть месяцев.", "tech", "implementation"),
    "film": ("9.1", "Пломбу для учебного контейнера выдаёт контролёр. Оттиск отмечают в ведомости.", "dbrct", "implementation"),
    "seals-old": ("10.1", "Розовые печати для учебных анкет выдаёт секретарь павильона.", None, None),
    "estimate-impl": ("11.1", "Оценку сметы на реализации проекта утверждает руководитель стройки.", "tech", "implementation"),
    "inspection-filing": ("12.1", "Протокол испытаний опытного образца передают в архив после подписи метролога.", "dbrct", "implementation"),
}

EXPERTS = {
    "telescope": ("Кто выдаёт жетоны на учебный телескоп?", "Жетоны выдаёт смотритель павильона."),
    "lantern": ("Как заменить бирюзовый фонарь в учебном тоннеле?", "Выдачу проводит картограф."),
    "draft": ("Кто ремонтирует оранжевую антенну на крыше?", "Устройство ремонтирует стажёр."),
}


@dataclass(frozen=True)
class Case:
    question: str
    category: str
    sources: tuple[str, ...] = ()
    department: str | None = None
    stage: str | None = None
    split: str = "control"

    @property
    def expected(self):
        return {source if source.startswith("expert_answer:") else
                f"regulation:{source}:{REGULATIONS[source][0]}" for source in self.sources}

    @property
    def behavior(self):
        return "all_sources" if len(self.sources) > 1 else ("source" if self.sources else "empty")


# Relevance was fixed from the fixture provisions and publication state before running retrieval.
CASES = (
    Case("Кто согласует ежегодный отпуск сотрудника?", "direct", ("leave",), split="calibration"),
    Case("Когда перечисляют деньги контрагенту за товар?", "paraphrase", ("payment",), split="calibration"),
    Case("К.Л.М.", "acronym", ("inspection",), split="calibration"),
    Case("пожалуйста подскажите кто и каким образом выдаёт пропуск", "short", ("badge",), split="calibration"),
    Case("Доступ к учебному стенду", "ambiguous", ("stand-tech", "stand-ib"), split="calibration"),
    Case("Сколько стоит обед сотрудника?", "unanswerable", split="calibration"),
    Case("Кто проверяет смету командировки?", "unanswerable", split="calibration"),
    Case("Кто выдаёт жетоны на учебный телескоп?", "expert", ("expert_answer:telescope",), split="calibration"),
    Case("Розовые печати для учебных анкет", "obsolete", split="calibration"),
    Case("Как заменить бирюзовый фонарь в учебном тоннеле?", "obsolete", split="calibration"),
    Case("Кто согласует доступ к учебному стенду?", "direct", ("stand-ib",), "ib", "implementation"),
    Case("Кто проводит поверку спектрометра?", "direct", ("meter",)),
    Case("Когда нельзя использовать резервную копию базы?", "direct", ("backup",)),
    Case("Потерял карточку для прохода, к кому обратиться?", "paraphrase", ("badge",)),
    Case("Кто определяет цену для подготовки сделки?", "paraphrase", ("estimate",), "tech", "presale"),
    Case("Кто отвечает за выдачу жетонов для учебного телескопа?", "expert", ("expert_answer:telescope",)),
    Case("Протокол К-Л-М", "acronym", ("inspection",), "dbrct", "implementation"),
    Case("Что такое КЛМ?", "acronym", ("inspection",)),
    Case("Пункт 8.1", "short", ("meter",)),
    Case("поверка спектрометра", "short", ("meter",)),
    Case("Смета на реализации проекта", "terminology", ("estimate-impl",), "tech", "implementation"),
    Case("Кто рассчитывает смету на пресейле?", "terminology", ("estimate",), "tech", "presale"),
    Case("Кто оформляет доступ к учебному стенду?", "ambiguous", ("stand-tech", "stand-ib")),
    Case("Как оформляют протокол испытаний опытного образца?", "multiple", ("inspection", "inspection-filing")),
    Case("Как устроены выдача и согласование доступа к стенду?", "multiple", ("stand-tech", "stand-ib")),
    Case("Где парковать личный автомобиль?", "unanswerable"),
    Case("Кто ремонтирует оранжевую антенну на крыше?", "obsolete"),
    Case("Срок выдачи полиса страхования питомца?", "unanswerable"),
    Case("спектромтер", "typo", ("meter",), split="calibration"),
    Case("резеврная копия", "typo", ("backup",), split="calibration"),
    Case("спeктрометр", "mixed_script", ("meter",), split="calibration"),
    Case("подскaжите про оплту счёта", "typo", ("payment",), split="calibration"),
    Case("   КЛМ   ", "normalization", ("inspection",), split="calibration"),
    Case("В каких случаях нельзя восстанавливать базу из копии?", "conditions", ("backup",), split="calibration"),
    Case("Как вернуть данные, если база пропала?", "paraphrase", ("backup",), split="calibration"),
    Case("Доступ к учебному стенду для аналитика ИБ", "refined", ("stand-ib",), "ib", "implementation", "calibration"),
    Case("Как получить жетоны, чтобы посмотреть в учебный телескоп?", "expert", ("expert_answer:telescope",), split="calibration"),
    Case("Кто выдаёт розовые печати для учебных анкет?", "obsolete", split="calibration"),
    Case("Как оплачивается компенсация процентов по ипотеке?", "unanswerable", split="calibration"),
    Case("Что делать с протоколом после испытаний опытного образца?", "multiple",
         ("inspection", "inspection-filing"), "dbrct", "implementation", "calibration"),
    Case("спектрoметра", "mixed_script", ("meter",)),
    Case("метролг", "typo", ("inspection", "inspection-filing"), "dbrct", "implementation"),
    Case("прoпуск", "mixed_script", ("badge",), "ib"),
    Case("   ПОВЕРКА    СПЕКТРОМЕТРА   ", "normalization", ("meter",)),
    Case("пункт 8.3", "unanswerable"),
    Case("А если резервная копия повреждена?", "conditions", ("backup",), "tech", "implementation"),
    Case("Когда документ о проверке макета отправляют на хранение?", "paraphrase", ("inspection-filing",)),
    Case("Доступ к учебному стенду, кто выдаёт его на реализации?", "ambiguous", ("stand-tech", "stand-ib"),
         stage="implementation"),
    Case("Кто проводит испытания образца и куда отправляют протокол?", "multiple", ("inspection", "inspection-filing")),
    Case("А если доступ оформляет Техническая дирекция на реализации?", "refined", ("stand-tech",), "tech", "implementation"),
    Case("Что со школьными жетонами для наблюдений через телескоп?", "expert", ("expert_answer:telescope",)),
    Case("Есть ли замена бирюзового фонаря в учебном тоннеле?", "obsolete"),
    Case("Кто назначает льготы многодетным?", "unanswerable"),
    Case("Кто чинит учебную антенну на крыше?", "obsolete"),
)


@dataclass(frozen=True)
class Measurement:
    times: list[float]
    context_chars: int


class Publications:
    revisions = {"expert_answer:telescope": 1}

    def published_revisions(self, source_ids):
        return {source: self.revisions[source] for source in source_ids if source in self.revisions}

    def index_questions(self, source_ids):
        return {source: EXPERTS[source.removeprefix("expert_answer:")][0] for source in source_ids}


def build(root, encoder):
    docs = root / "docs"
    docs.mkdir()
    entries = {}
    for identity, (section, text, dept, stage) in REGULATIONS.items():
        path = docs / f"{identity}.docx"
        document = Document()
        document.add_paragraph(f"{section}. {text}")
        document.save(path)
        fields = {"department": dept, "stage": stage}
        if identity == "seals-old":
            fields.update(state="revoked", change_on="2020-01-01")
        entries[path.name] = dict(id=identity, sha256=sha256(path.read_bytes()).hexdigest(),
                                  reviewer="Synthetic reviewer", basis="Synthetic fixture",
                                  fields={k: v for k, v in fields.items() if v is not None}, sections={})
    registry = root / "registry.json"
    registry.write_text(json.dumps(dict(format=1, documents=entries)), encoding="utf-8")
    update_index(docs, root / "index", encoder, registry=registry)


def make_search(root, encoder, mode):
    with patch("retrieval.search.Encoder", return_value=encoder):
        return AgentRetrieval(Publications(), index=root / "index", model_root=MODEL_ROOT,
                              settings=Settings(mode=mode))


def add_experts(search):
    for key, (_, answer) in EXPERTS.items():
        metadata = ExpertAnswerMetadata(key, datetime(2020, 1, 1, tzinfo=timezone.utc), ApplicabilityScope(), 1)
        search.upsert_expert_answer(ApprovedExpertAnswer(f"expert_answer:{key}", key, answer, metadata))


def metrics(samples):
    relevant = [(case, ids) for case, ids, _ in samples if case.expected]
    negative = [ids for case, ids, _ in samples if not case.expected]
    ranks = [next((i for i, identity in enumerate(ids, 1) if identity in case.expected), None)
             for case, ids in relevant]
    times = [ms for _, _, sample in samples for ms in sample.times]
    return dict(n=len(samples), positive=len(relevant), negative=len(negative),
                avg_context_chars=mean(ms.context_chars for _, _, ms in samples),
                hit=mean(bool(rank) for rank in ranks) if ranks else None,
                recall=mean(len(set(ids) & case.expected) / len(case.expected)
                            for case, ids in relevant) if ranks else None,
                mrr=mean(1 / rank if rank else 0 for rank in ranks) if ranks else None,
                fpr=mean(bool(ids) for ids in negative) if negative else None,
                avg_ms=mean(times), p50_ms=float(np.percentile(times, 50)), p95_ms=float(np.percentile(times, 95)))


class QueryNormalizationTests(unittest.TestCase):
    def test_known_acronyms_and_polite_intro(self):
        chunks = [dict(document="", department=None, stage=None, section=None, spans=[],
                       index_text="passage: Протокол КЛМ подписывает метролог."),
                  dict(document="", department=None, stage=None, section=None, spans=[],
                       index_text="passage: Охранник выдаёт пропуск.")]
        lexical = BM25(chunks)
        for query in ("К.Л.М.", "К-Л-М", "КЛМ"):
            self.assertEqual(set(lexical.scores(query, {0, 1}, .35)), {0})
        self.assertEqual(set(lexical.scores(
            "Пожалуйста, подскажите кто и каким образом выдаёт пропуск?", {0, 1}, .35)), {1})
        self.assertNotIn("абв", lexical.terms("а.б.в."))

    def test_conservative_repair(self):
        text = "Поверку спектрометра проводит метролог. Протокол КЛМ не подписывает техник."
        lexical = BM25([dict(document="", department=None, stage=None, section="8.1", spans=[],
                             index_text="passage: " + text)])
        for question in ("спектромтер", "спeктрометр", "метролг"):
            with self.subTest(question=question):
                self.assertEqual(set(lexical.scores(question, {0}, .35)), {0})
                self.assertFalse(lexical.scores(question, set(), .35))
        for query in ("8.3", "КЛН", "нелзя"):
            self.assertEqual(lexical.scores(query, {0}, .35), {})


@unittest.skipUnless(os.environ.get("TEST_RETRIEVAL_EVAL") == "1", "requires local E5 weights")
class RetrievalEvaluation(unittest.TestCase):
    def test_fixed_queries(self):
        import torch

        threads = torch.get_num_threads()
        self.addCleanup(torch.set_num_threads, threads)
        torch.set_num_threads(2)
        started = perf_counter()
        encoder = Encoder()
        cold_e5 = (perf_counter() - started) * 1000
        def rss():
            try:
                import psutil
            except ImportError:
                return None
            return psutil.Process().memory_info().rss

        e5_rss = rss()
        reranker = cold_reranker = None
        if os.environ.get("TEST_LOCAL_RERANK") == "1":
            path = MODEL_ROOT / "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1/1427fd652930e4ba29e8149678df786c240d8825"
            started = perf_counter()
            reranker = LocalCrossEncoder(path)
            cold_reranker = (perf_counter() - started) * 1000

        report = {"cold_e5_ms": cold_e5, "cold_reranker_ms": cold_reranker, "threads": 2, "modes": {},
                  "rss_e5_bytes": e5_rss, "rss_models_bytes": rss()}
        with TemporaryDirectory() as temp:
            root = Path(temp)
            build(root, encoder)
            add_experts(make_search(root, encoder, "hybrid"))
            modes = ("bm25", "semantic", "hybrid", "rerank") if reranker else ("bm25", "semantic", "hybrid")
            report["regulation_files_bytes"] = sum(p.stat().st_size for p in (root / "index").rglob("*")
                                                     if p.is_file() and not p.name.startswith("experts.sqlite3"))
            report["expert_db_bytes"] = (root / "index" / "experts.sqlite3").stat().st_size
            calibration_only = os.environ.get("RETRIEVAL_CALIBRATION_ONLY") == "1"
            if calibration_only:
                modes = ("hybrid",)
            for mode in modes:
                search = make_search(root, encoder, "hybrid" if mode == "rerank" else mode)
                if mode == "rerank":
                    search.reranker = reranker
                for _ in range(2):
                    search.search_documents(CASES[0].question, 5)
                groups = defaultdict(list)
                failures = {}
                for number, case in enumerate(CASES, 1):
                    if calibration_only and case.split != "calibration":
                        continue
                    first = None
                    times = []
                    for _ in range(3):
                        started = perf_counter()
                        hits = search.search_documents(case.question, 5, case.department, case.stage)
                        times.append((perf_counter() - started) * 1000)
                        ids = [hit.source_id for hit in hits]
                        if first is None:
                            first = ids
                        self.assertEqual(first, ids)
                        self.assertEqual(len(ids), len(set(ids)))
                        self.assertTrue(set(ids).isdisjoint({"expert_answer:draft", "expert_answer:lantern",
                                                              "regulation:seals-old:10.1"}))
                        for hit in hits:
                            if hit.source_id == "expert_answer:telescope":
                                self.assertEqual(hit.text, EXPERTS["telescope"][1])
                    sample = (case, first, Measurement(times, search.metrics.context_chars))
                    for group in ("all", case.split, f"category:{case.category}", f"{case.split}:{case.category}"):
                        groups[group].append(sample)
                    if case.expected - set(first) or not case.expected and first:
                        failures[number] = first
                    if mode == "hybrid" and number in {5, 11, 14, 16, 21, 22, 23, 24, 25}:
                        with self.subTest(case=number):
                            self.assertTrue(case.expected.issubset(first))
                    if not case.expected and number <= 28 and number != 9:
                        with self.subTest(case=number, mode=mode):
                            self.assertEqual(first, [])
                report["modes"][mode] = {key: metrics(value) for key, value in sorted(groups.items())}
                report["modes"][mode]["failures"] = failures
                if mode == "hybrid" and os.environ.get("RETRIEVAL_AUDIT_STRICT") == "1":
                    self.assertNotIn(9, failures, "stale-rule semantic distraction should be omitted")
                max_p95 = os.environ.get("RETRIEVAL_EVAL_MAX_P95_MS")
                if mode == "hybrid" and max_p95 is not None:
                    self.assertLessEqual(report["modes"][mode]["all"]["p95_ms"], float(max_p95))
            hybrid = make_search(root, encoder, "hybrid")
            grid = ({}, {"cosine_floor": .80}, {"cosine_floor": .84}, {"lexical_coverage": .50},
                    {"lexical_weight": 2.0, "rrf_k": 20}, {"semantic_weight": 2.0, "rrf_k": 20})
            report["calibration_grid"] = []
            for overrides in grid:
                hybrid.settings = replace(Settings(), **overrides)
                samples = []
                for case in CASES:
                    if case.split == "calibration":
                        hits = hybrid.search_documents(case.question, 5, case.department, case.stage)
                        samples.append((case, [hit.source_id for hit in hits],
                                        Measurement([hybrid.metrics.total_ms], hybrid.metrics.context_chars)))
                summary = metrics(samples)
                report["calibration_grid"].append(dict(overrides=overrides,
                                                       **{key: summary[key] for key in ("hit", "recall", "mrr", "fpr")}))
            if os.environ.get("RETRIEVAL_AUDIT_STRICT") == "1":
                from retrieval.audit_local import CitationSearch, audit

                local = CitationSearch(root / "index", encoder=encoder)
                summary = audit(local, [{"question": "Что предусмотрено в пункте 8.1?",
                                        "expected_ids": ["regulation:meter:8.1"]},
                                       {"question": "Что предусмотрено в пункте 99.99?", "expected_ids": []}])
                self.assertEqual((summary["hit_rate_at_5"], summary["false_positive_rate"]), (1, 0))
                self.assertFalse(any(isinstance(value, str) for value in summary.values()))
            # A physical tombstone must remain effective after reopening the adapter.
            search.delete_expert_answer("expert_answer:telescope", 2)
            reopened = make_search(root, encoder, "hybrid")
            self.assertNotIn("expert_answer:telescope",
                             {hit.source_id for hit in reopened.search_documents(EXPERTS["telescope"][0], 5)})

        print("RETRIEVAL_BENCHMARK", json.dumps(report, sort_keys=True))
