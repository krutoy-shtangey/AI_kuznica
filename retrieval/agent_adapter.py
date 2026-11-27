"""Connect the regulation index to the backend's synchronous retrieval contract."""
import os
from pathlib import Path

from shared.contracts import ApprovedExpertAnswer, ExpertPublicationLookup, RetrievalError, ScopeFilter, SearchResult
from .embeddings import MODEL_ROOT
from .search import LongQuery, RegulationSearch


class AgentRetrieval(RegulationSearch):
    require_citation = True

    def __init__(self, publications: ExpertPublicationLookup, *, index: Path, model_root: Path):
        self.publications = publications
        super().__init__(index, model_root=model_root)

    def search_documents(self, question: str, top_k: int = 8, department: ScopeFilter = None,
                         stage: ScopeFilter = None) -> list[SearchResult]:
        try:
            return super().search_documents(question, top_k, department, stage)
        except LongQuery as exc:
            raise RetrievalError("Refined question exceeds the local encoder limit") from exc

    def upsert_expert_answer(self, answer: ApprovedExpertAnswer) -> None:
        raise RetrievalError("Expert indexing is unavailable")

    def delete_expert_answer(self, source_id: str, revision: int) -> None:
        raise RetrievalError("Expert indexing is unavailable")


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
