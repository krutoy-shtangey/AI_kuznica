"""In-memory representation. All positions and orders are zero-based."""
from dataclasses import dataclass, field

from shared.contracts import ApplicabilityScope, SourceStatus


@dataclass(frozen=True)
class Metadata:
    document_id: str
    filename: str
    checksum: str
    scope: ApplicabilityScope = ApplicabilityScope()
    status: SourceStatus = SourceStatus.UNKNOWN


@dataclass
class Cell:
    row: int
    column: int
    columns: int
    rows: int = 1
    blocks: list["Element"] = field(default_factory=list)


@dataclass
class Element:
    metadata: Metadata
    order: int
    kind: str
    text: str
    section: str | None
    headings: tuple[tuple[int, str], ...]
    label: str | None = None
    list_level: int | None = None
    cells: list[Cell] = field(default_factory=list)
    header_rows: tuple[int, ...] = ()
    shape: tuple[int, int] | None = None


@dataclass
class LoadedDocument:
    metadata: Metadata
    blocks: list[Element]

    def elements(self):
        def walk(blocks):
            for block in blocks:
                yield block
                for cell in block.cells:
                    yield from walk(cell.blocks)
        return walk(self.blocks)


@dataclass
class Batch:
    documents: list[LoadedDocument] = field(default_factory=list)
    duplicates: int = 0
    unsupported: int = 0
    errors: int = 0
    reused: list[Metadata] = field(default_factory=list)

    def statistics(self):
        paragraphs = tables = 0
        for document in self.documents:
            for element in document.elements():
                if element.kind == "table":
                    tables += 1
                else:
                    paragraphs += 1
        return dict(documents=len(self.documents), paragraphs=paragraphs,
                    tables=tables, elements=paragraphs + tables, errors=self.errors)
