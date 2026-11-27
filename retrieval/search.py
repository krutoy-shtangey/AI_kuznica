"""Offline search over regulations. One instance per application, safe to share across threads."""
import argparse
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from getpass import getpass
import json
from math import isfinite
from pathlib import Path
import re
from threading import RLock
from time import perf_counter
from typing import Protocol

import numpy as np

from shared.contracts import (INITIAL_DEPARTMENT_IDS, INITIAL_STAGE_IDS, RetrievalError,
                              ScopeFilter, SearchResult, SourceStatus, SourceType)
from .bm25 import BM25
from .embeddings import MODEL_ROOT, Encoder
from .index import load_index
from .metadata import lifecycle, relationships


@dataclass(frozen=True)
class Settings:
    mode: str = "hybrid"
    cosine_floor: float = .82
    lexical_coverage: float = .35
    rrf_k: int = 60
    lexical_weight: float = 1
    semantic_weight: float = 1
    depth: int = 32
    context_chars: int = 6000
    neighbors: bool = False
    include_history: bool = False

    def __post_init__(self):
        if (self.mode not in ("bm25", "semantic", "hybrid")
                or not -1 <= self.cosine_floor <= 1 or not 0 <= self.lexical_coverage <= 1
                or self.rrf_k < 0 or self.depth < 1 or self.context_chars < 1
                or not all(isfinite(w) and w > 0 for w in (self.lexical_weight, self.semantic_weight))):
            raise ValueError("Invalid search configuration")


class Reranker(Protocol):
    def score(self, question: str, passages: list[str]) -> list[float]: ...


class LocalCrossEncoder:
    """Optional local model. Reject overlong pairs instead of truncating them."""
    def __init__(self, directory):
        from sentence_transformers import CrossEncoder
        path = Path(directory).resolve(strict=True)
        self.model = CrossEncoder(str(path), device="cpu", local_files_only=True,
                                  trust_remote_code=False, model_kwargs={"use_safetensors": True})

    def score(self, question, passages):
        pairs = [(question, text) for text in passages]
        lengths = self.model.tokenizer([question] * len(pairs), passages, truncation=False,
                                       verbose=False, return_length=True)["length"]
        if max(lengths) > self.model.max_length:
            raise ValueError("Reranker pair exceeds model input limit")
        return self.model.predict(pairs, show_progress_bar=False).tolist()


def fuse(rankings, k):
    result = defaultdict(float)
    for weight, ranking in rankings:
        for rank, row in enumerate(dict.fromkeys(ranking), 1):
            result[row] += weight / (k + rank)
    return dict(result)


def scope(value):
    if value is None:
        return ()
    values = (value,) if isinstance(value, str) else value
    if not isinstance(values, tuple) or not values or not all(isinstance(v, str) and v.strip() for v in values):
        raise ValueError("Expected an ID or a nonempty tuple of IDs")
    return values


REFERENCE = re.compile(r"\b(?:пункт(?:а|е|у)?|подпункт(?:а|е|у)?|пп?\.?)\s*(\d+(?:\.\d+)*)", re.I)
CONDITION = re.compile(r"\b(если|кроме|исключени\w*|при условии|запрещ\w*|нельзя|только)\b", re.I)


@dataclass
class Timings:
    embedding_ms: float = 0
    bm25_ms: float = 0
    semantic_ms: float = 0
    fusion_ms: float = 0
    reranking_ms: float = 0
    total_ms: float = 0
    returned: int = 0
    context_chars: int = 0


def ms(start):
    return (perf_counter() - start) * 1000


class RegulationSearch:
    require_citation = False
    encode_publications = False

    def __init__(self, index="data/index", settings=Settings(), *, encoder=None, aliases=None,
                 reranker: Reranker | None = None, model_root=MODEL_ROOT):
        start = perf_counter()
        self.root, self.settings = Path(index), settings
        self.encoder, self.aliases, self.reranker = encoder, dict(aliases or {}), reranker
        self.owns_encoder = encoder is None
        self.model_root = model_root
        self.lock = RLock()
        self.marker = None
        self.metrics = Timings()
        try:
            self._refresh()
        except Exception as exc:
            raise RetrievalError("Cannot initialize local regulation search") from exc
        self.cold_ms = ms(start)

    def _refresh(self):
        marker = (self.root / "CURRENT").read_bytes()
        if marker == self.marker:
            return
        saved = load_index(self.root)
        if any(c["source_type"] != SourceType.REGULATION for c in saved.chunks):
            raise RetrievalError("Unexpected source type in the regulation index")
        manifest = saved.manifest
        if manifest["version"] < 2:
            raise RetrievalError("Update the local index to extract regulation metadata")
        if self.encode_publications or self.settings.mode != "bm25":
            model_key = (manifest["model"], manifest["revision"])
            encoder = self.encoder
            if self.owns_encoder and (encoder is None or (encoder.model, encoder.revision) != model_key):
                encoder = Encoder(model=model_key[0], revision=model_key[1], model_root=self.model_root)
            if (encoder.model, encoder.revision, encoder.dimension) != (*model_key, manifest["dimension"]):
                raise RetrievalError("Index requires a different query encoder")
        lexical = BM25(saved.chunks, self.aliases) if self.settings.mode != "semantic" else None
        positions = {(c["document_id"], c["order"]): i for i, c in enumerate(saved.chunks)}
        paths = defaultdict(set)
        for c in saved.chunks:
            paths[c["document_id"], c["section"]].add(tuple(t for _, t in c["headings"]))
        citations = []
        for c in saved.chunks:
            section = c["section"]
            if section and len(paths[c["document_id"], section]) > 1:
                section = " / ".join([*(t for _, t in c["headings"]), section])
            citations.append(f"regulation:{c['document_id']}:{section}" if section else None)
        counts = Counter(citations)
        self.citations = citations
        self.source_ids = [ref if ref and counts[ref] == 1 else c["source_id"]
                           for ref, c in zip(citations, saved.chunks)]
        self.metadata_counts = {f"missing_{key}": sum(c[key] is None for c in saved.chunks)
                                for key in ("department", "stage")}
        self.metadata_counts["indexed_chunks"] = len(saved.chunks)
        if self.encode_publications or self.settings.mode != "bm25":
            self.encoder = encoder
        self.saved, self.lexical, self.positions, self.marker = saved, lexical, positions, marker

    def search_documents(self, question: str, top_k: int = 8, department: ScopeFilter = None,
                         stage: ScopeFilter = None) -> list[SearchResult]:
        if not isinstance(question, str) or not question.strip() or type(top_k) is not int or top_k < 1:
            raise ValueError("Expected a nonempty question and positive integer top_k")
        departments, stages = scope(department), scope(stage)
        if set(departments) - set(INITIAL_DEPARTMENT_IDS) or set(stages) - set(INITIAL_STAGE_IDS):
            raise ValueError("Unknown scope ID")
        start = perf_counter()
        with self.lock:
            metrics = Timings()
            try:
                self._refresh()
                if self.settings.mode != "bm25" and self.encoder.count("query: " + question) > self.encoder.limit:
                    raise LongQuery("Question exceeds E5 input limit; split it into complete questions")
                self.candidate_details = []
                results = self._search(question, top_k, departments, stages, metrics)
            except LongQuery:
                raise
            except Exception as exc:
                raise RetrievalError("Local regulation search failed") from exc
            finally:
                metrics.total_ms = ms(start)
                self.metrics = metrics
            return results

    def search_with_context(self, question, top_k=8, department=None, stage=None):
        """Return contract results and local metadata for eligible alternatives, including overflow."""
        with self.lock:
            results = self.search_documents(question, top_k, department, stage)
            return results, dict(generation=self.saved.manifest["generation"], candidates=self.candidate_details,
                                 relationships=relationships(self.candidate_details))

    def _rank_candidates(self, question, allowed, top_k, metrics, departments, stages, refs):
        return self._rank(question, allowed, top_k, metrics, self.source_ids, self.saved.chunks,
                          self.saved.vectors, self.lexical)

    def _rank(self, question, allowed, top_k, metrics, source_ids, chunks, vectors, lexical, *, split_at=None):
        if not allowed:
            return [], {}
        cfg = self.settings
        depth = max(cfg.depth, top_k)
        def ranked(scores):
            return sorted(scores, key=lambda i: (-scores[i], source_ids[i]))

        def shortlist(scores):
            order = ranked(scores)
            if split_at is None or top_k < 2 or len(order) <= depth:
                return order[:depth]
            kept = order[:depth]
            lower_type = kept[0] < split_at
            if all((i < split_at) == lower_type for i in kept):
                other = next((i for i in order[depth:] if (i < split_at) != lower_type), None)
                if other is not None:
                    kept[-1] = other
            return kept

        rankings = []
        if lexical is not None:
            t = perf_counter()
            scores = lexical.scores(question, allowed, cfg.lexical_coverage)
            rankings.append((cfg.lexical_weight, shortlist(scores)))
            metrics.bm25_ms = ms(t)
        if cfg.mode != "bm25":
            t = perf_counter()
            query = self.encoder.encode_queries([question])[0]
            if (query.shape != (self.encoder.dimension,) or not np.isfinite(query).all()
                    or not np.isclose(np.linalg.norm(query), 1, atol=1e-4)):
                raise RetrievalError("Invalid query vector")
            metrics.embedding_ms = ms(t)
            t = perf_counter()
            values = vectors @ query
            scores = {i: float(values[i]) for i in allowed if values[i] >= cfg.cosine_floor}
            rankings.append((cfg.semantic_weight, shortlist(scores)))
            metrics.semantic_ms = ms(t)
        t = perf_counter()
        if cfg.mode == "hybrid":
            scores = fuse(rankings, cfg.rrf_k)
        order = shortlist(scores)
        metrics.fusion_ms = ms(t)
        if self.reranker and order:
            t = perf_counter()
            values = self.reranker.score(question, [chunks[i]["index_text"].removeprefix("passage: ") for i in order])
            if len(values) != len(order) or not all(isfinite(v) for v in values):
                raise RetrievalError("Invalid reranker output")
            scores = dict(zip(order, values))
            order = ranked(scores)
            metrics.reranking_ms = ms(t)

        return order, scores

    def _search(self, question, top_k, departments, stages, metrics):
        chunks, cfg = self.saved.chunks, self.settings
        refs = set(REFERENCE.findall(question))
        today = date.today()
        states = [lifecycle(c["regulation"], today) for c in chunks]
        allowed = {i for i, c in enumerate(chunks)
                   if (not self.require_citation or self.citations[i])
                   and (cfg.include_history or states[i] in ("unknown", "approved"))
                   and (not departments or c["department"] is None or c["department"] in departments)
                   and (not stages or c["stage"] is None or c["stage"] in stages)
                   and (not refs or refs.intersection({c["section"], *(s["section"] for s in c["spans"])}))}
        order, scores = self._rank_candidates(question, allowed, top_k, metrics, departments, stages, refs)
        def ranked(scores):
            return sorted(scores, key=lambda i: (-scores[i], self.source_ids[i]))

        # Reserve a candidate for each applicable known scope before filling spare slots.
        first = {}
        for i in order:
            c = chunks[i]
            fields = c["regulation"]["fields"]
            key = (c["department"], c["stage"], fields.get("process"), fields.get("business"),
                   fields.get("scope"), c["document_id"])
            first.setdefault(key, i)
        priority = list(dict.fromkeys([*first.values(), *order]))
        chosen = {}
        chosen_citations = set()
        def include(i, score):
            if self.require_citation and self.citations[i] in chosen_citations:
                return False
            size = len(chunks[i]["text"])
            if i not in chosen and len(chosen) < top_k and metrics.context_chars + size <= cfg.context_chars:
                chosen[i] = score
                chosen_citations.add(self.citations[i])
                metrics.context_chars += size
                return True
            return False
        for i in priority:
            include(i, scores[i])
        if cfg.neighbors:
            for i in list(chosen):
                c = chunks[i]
                for delta in (-1, 1):
                    j = self.positions.get((c["document_id"], c["order"] + delta))
                    if j in allowed:
                        n = chunks[j]
                        if (n["headings"] == c["headings"] and n["section"] == c["section"]
                                and CONDITION.search(n["text"])):
                            include(j, min(chosen.values()))
        results = []
        for i in dict.fromkeys([*order, *chosen]):
            c = chunks[i]
            fields = dict(c["regulation"]["fields"])
            record = self.saved.manifest["profiles"][c["document_id"]]
            source_id = self.citations[i] if self.require_citation else self.source_ids[i]
            self.candidate_details.append(dict(source_id=source_id, citation_id=self.citations[i],
                chunk_id=c["source_id"], document_id=c["document_id"],
                section=c["section"], lifecycle=states[i], fields=fields, selected=i in chosen,
                chunk_sha256=c["checksum"], document_sha256=c["document_sha256"],
                validity_basis=c["regulation"]["guards"], headings=c["headings"],
                reviewer=record["reviewer"], basis=record["basis"]))
        for i in ranked(chosen):
            c = chunks[i]
            source_id = self.citations[i] if self.require_citation else self.source_ids[i]
            section = source_id.removeprefix(f"regulation:{c['document_id']}:") if self.require_citation else c["section"]
            results.append(SearchResult(source_id=source_id, document_id=c["document_id"],
                                        source_type=SourceType(c["source_type"]),
                                        status=SourceStatus.APPROVED if states[i] == "approved" else SourceStatus.UNKNOWN,
                                        document=c["document"], text=c["text"], section=section,
                                        department=c["department"], stage=c["stage"], score=float(chosen[i]),
                                        source_url=c.get("source_url")))
        metrics.returned = len(results)
        return results


class LongQuery(ValueError):
    """Caller can split a question without losing terms to model truncation."""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, default=Path("data/index"))
    parser.add_argument("--mode", choices=("hybrid", "bm25", "semantic"), default="hybrid")
    parser.add_argument("--department", action="append")
    parser.add_argument("--stage", action="append")
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--context-chars", type=int, default=6000)
    parser.add_argument("--cosine-floor", type=float, default=Settings.cosine_floor)
    parser.add_argument("--lexical-coverage", type=float, default=Settings.lexical_coverage)
    parser.add_argument("--show", action="store_true", help="display source text locally")
    parser.add_argument("--neighbors", action="store_true")
    parser.add_argument("--include-history", action="store_true", help="also include ineligible and review-only sources")
    parser.add_argument("--aliases", type=Path, help="local JSON object mapping approved variants to terms")
    args = parser.parse_args()
    try:
        aliases = json.loads(args.aliases.read_text(encoding="utf-8-sig")) if args.aliases else {}
        if not isinstance(aliases, dict) or not all(isinstance(v, str) and v.strip() for v in aliases.values()):
            raise ValueError("Invalid alias dictionary")
        settings = Settings(mode=args.mode, context_chars=args.context_chars, neighbors=args.neighbors,
                            include_history=args.include_history,
                            cosine_floor=args.cosine_floor, lexical_coverage=args.lexical_coverage)
        search = RegulationSearch(args.index, settings, aliases=aliases)
        items, context = search.search_with_context(getpass("Вопрос: "), args.top_k, tuple(args.department or ()) or None,
                                                     tuple(args.stage or ()) or None)
        output = dict(**asdict(search.metrics), **search.metadata_counts, cold_ms=search.cold_ms)
        if args.show:
            output["results"] = [asdict(item) for item in items]
            output["context"] = context
        print(json.dumps(output, ensure_ascii=False))
        return 0
    except LongQuery:
        print('{"error":"query_too_long"}')
    except RetrievalError:
        print('{"error":"retrieval_unavailable"}')
    except (ValueError, OSError, EOFError):
        print('{"error":"invalid_input"}')
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
