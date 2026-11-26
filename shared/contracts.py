"""Shared domain contracts for the assistant and retrieval modules."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, TypeAlias


INITIAL_DEPARTMENT_IDS = ("ib", "tech", "dbrct")
INITIAL_STAGE_IDS = ("presale", "implementation")

# A filter accepts one ID, a non-empty tuple of alternatives, or no restriction.
ScopeFilter: TypeAlias = str | tuple[str, ...] | None


class SourceType(StrEnum):
    REGULATION = "regulation"
    EXPERT_ANSWER = "expert_answer"


class SourceStatus(StrEnum):
    APPROVED = "approved"
    UNKNOWN = "unknown"


class AgentResultType(StrEnum):
    ANSWER = "ANSWER"
    CLARIFY = "CLARIFY"
    NO_ANSWER = "NO_ANSWER"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True)
class ApplicabilityScope:
    department: str | None = None
    stage: str | None = None
    conditions: str | None = None


@dataclass(frozen=True)
class ExpertAnswerMetadata:
    appeal_id: str
    approved_at: datetime
    scope: ApplicabilityScope
    revision: int


@dataclass(frozen=True)
class SearchResult:
    source_id: str
    source_type: SourceType
    document: str
    text: str
    status: SourceStatus
    score: float
    section: str | None = None
    department: str | None = None
    stage: str | None = None
    source_url: str | None = None
    expert_metadata: ExpertAnswerMetadata | None = None
    document_id: str | None = None


@dataclass(frozen=True)
class ApprovedExpertAnswer:
    source_id: str
    document: str
    text: str
    metadata: ExpertAnswerMetadata
    section: str | None = None
    source_url: str | None = None


@dataclass(frozen=True)
class AgentResult:
    result_type: AgentResultType
    dialogue_id: str
    text: str | None = None
    source_ids: tuple[str, ...] = ()
    clarification_question: str | None = None
    appeal_id: str | None = None


class RetrievalError(Exception):
    """A technical failure of document search or an index operation."""


class ExpertPublicationLookup(Protocol):
    def published_revisions(self, source_ids: tuple[str, ...]) -> dict[str, int]:
        """Return current approved revisions from the authoritative store."""
        ...


class Retrieval(Protocol):
    def search_documents(
        self,
        question: str,
        top_k: int = 8,
        department: ScopeFilter = None,
        stage: ScopeFilter = None,
    ) -> list[SearchResult]:
        """Return ranked candidates, including only currently approved expert revisions."""
        ...

    def upsert_expert_answer(self, answer: ApprovedExpertAnswer) -> None:
        """Index a confirmed revision under its stable source identifier."""
        ...

    def delete_expert_answer(self, source_id: str, revision: int) -> None:
        """Remove an expert answer and record the removal revision."""
        ...
