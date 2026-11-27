"""Connect the regulation index to the backend's synchronous retrieval contract."""
from dataclasses import replace
import os
from pathlib import Path

import numpy as np

from shared.contracts import ApprovedExpertAnswer, ExpertPublicationLookup, RetrievalError, ScopeFilter, SearchResult
from .bm25 import BM25
from .experts import ExpertIndex
from .embeddings import MODEL_ROOT
from .search import LongQuery, RegulationSearch, Settings


class AgentRetrieval(RegulationSearch):
    require_citation = True
    encode_publications = True

    def __init__(self, publications: ExpertPublicationLookup, *, index: Path, model_root: Path,
                 settings: Settings = Settings()):
        self.publications = publications
        super().__init__(index, settings, model_root=model_root)
        try:
            self.experts = ExpertIndex(self.root)
        except Exception as exc:
            raise RetrievalError("Cannot open local expert index") from exc
        self.pool_marker = None
        self.expert_generation = None

    def search_documents(self, question: str, top_k: int = 8, department: ScopeFilter = None,
                         stage: ScopeFilter = None) -> list[SearchResult]:
        try:
            return super().search_documents(question, top_k, department, stage)
        except LongQuery as exc:
            raise RetrievalError("Refined question exceeds the local encoder limit") from exc

    def _rank_candidates(self, question, allowed, top_k, metrics, departments, stages, refs):
        previous = self.expert_generation if self.pool_marker == self.marker else None
        snapshot = self.experts.snapshot(previous, self.encoder)
        if snapshot is not None:
            generation, results, chunks, vectors = snapshot
            self.extra_results = results
            if results:
                self.pool_chunks = self.saved.chunks + chunks
                self.pool_ids = self.source_ids + [hit.source_id for hit in results]
                self.pool_vectors = np.vstack((self.saved.vectors, *vectors))
                self.pool_lexical = (BM25(self.pool_chunks, self.aliases)
                                     if self.settings.mode != "semantic" else None)
            self.pool_marker, self.expert_generation = self.marker, generation
        self.expert_hits = []
        if not self.extra_results:
            return super()._rank_candidates(question, allowed, top_k, metrics, departments, stages, refs)
        start = len(self.saved.chunks)
        candidates = {start + i: hit for i, hit in enumerate(self.extra_results)
                      if (not departments or not hit.department or hit.department in departments)
                      and (not stages or not hit.stage or hit.stage in stages)
                      and (not refs or hit.section in refs)}
        ids = [hit.source_id for hit in candidates.values()]
        published = {}
        for offset in range(0, len(ids), 500):
            published.update(self.publications.published_revisions(tuple(ids[offset:offset + 500])))
        allowed.update(i for i, hit in candidates.items()
                       if published.get(hit.source_id) == hit.expert_metadata.revision)
        order, scores = self._rank(question, allowed, top_k, metrics, self.pool_ids, self.pool_chunks,
                                   self.pool_vectors, self.pool_lexical, split_at=start)
        self.expert_hits = [replace(candidates[i], score=scores[i]) for i in order if i >= start]
        return [i for i in order if i < start], scores

    def _search(self, question, top_k, departments, stages, metrics):
        regulations = super()._search(question, top_k, departments, stages, metrics)
        if not self.expert_hits:
            return regulations
        hits = sorted(regulations + self.expert_hits, key=lambda hit: (-hit.score, hit.source_id))
        # Keep both types available to the agent when both are relevant and fit.
        if top_k >= 2 and regulations:
            pair = [regulations[0], self.expert_hits[0]]
            if sum(len(hit.text) for hit in pair) <= self.settings.context_chars:
                ids = {hit.source_id for hit in pair}
                hits = pair + [hit for hit in hits if hit.source_id not in ids]
        chosen = []
        used = 0
        for hit in hits:
            if used + len(hit.text) <= self.settings.context_chars:
                chosen.append(hit)
                used += len(hit.text)
                if len(chosen) == top_k:
                    break
        selected = {hit.source_id for hit in chosen}
        for detail in self.candidate_details:
            detail["selected"] = detail["selected"] and detail["source_id"] in selected
        metrics.returned, metrics.context_chars = len(chosen), used
        return sorted(chosen, key=lambda hit: (-hit.score, hit.source_id))

    def _apply_revision(self, source_id, revision, answer):
        with self.lock:
            try:
                self._refresh()
                self.experts.apply(source_id, revision, answer, self.publications, self.encoder)
            except RetrievalError:
                raise
            except Exception as exc:
                raise RetrievalError("Expert index update failed") from exc

    def upsert_expert_answer(self, answer: ApprovedExpertAnswer) -> None:
        self._apply_revision(answer.source_id, answer.metadata.revision, answer)

    def delete_expert_answer(self, source_id: str, revision: int) -> None:
        self._apply_revision(source_id, revision, None)


def retrieval_factory(publications: ExpertPublicationLookup) -> AgentRetrieval:
    return AgentRetrieval(
        publications,
        index=Path(os.environ.get("RETRIEVAL_INDEX", "data/index")),
        model_root=Path(os.environ.get("RETRIEVAL_MODEL_ROOT", MODEL_ROOT)),
    )


def create_local_app():
    """Uvicorn --factory entry point. Mail delivery is opt-in outside this launcher."""
    from backend.main import create_app

    return create_app(retrieval_factory=retrieval_factory,
                      quality_secret=os.environ.get("QUALITY_ADMIN_SECRET"))
