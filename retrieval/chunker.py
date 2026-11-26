"""Structural chunking. Source offsets refer to Python character indexes in Element.text."""
from dataclasses import asdict, dataclass
from hashlib import sha256
from itertools import groupby
import json

from shared.contracts import SourceType
from .models import Element


@dataclass(frozen=True)
class Config:
    target: int = 320
    maximum: int = 512
    overlap: int = 40

    def __post_init__(self):
        if not 0 <= self.overlap < self.target <= self.maximum <= 512:
            raise ValueError("Invalid chunk budget")


@dataclass(frozen=True)
class Span:
    element: int
    start: int
    end: int
    section: str | None
    # Nested table coordinates (table order, row, column, row span, column span).
    cells: tuple = ()


@dataclass(frozen=True)
class Unit:
    block: Element
    context: str
    group: tuple
    cells: tuple = ()


def table_units(table, parents=(), prefix=""):
    headers = table.header_rows or (0,)
    header_cells = [cell for cell in table.cells if cell.row in headers]
    merged_stubs = [cell for cell in table.cells if cell.rows > 1 and cell.row not in headers]
    for cell in table.cells:
        pos = parents + ((table.order, cell.row, cell.column, cell.rows, cell.columns),)
        labels = []
        for head in header_cells:
            if (head.row < cell.row
                    and head.column < cell.column + cell.columns
                    and cell.column < head.column + head.columns):
                labels.extend(b.text for b in head.blocks if b.text.strip())
        # A vertically merged stub can label several successive rows.
        for stub in merged_stubs:
            if stub.column < cell.column and stub.row <= cell.row < stub.row + stub.rows:
                labels.extend(b.text for b in stub.blocks if b.text.strip())
        context = "\n".join(filter(None, [prefix, *labels]))
        for block in cell.blocks:
            if block.kind == "table":
                yield from table_units(block, pos, context)
            else:
                yield Unit(block, context, ("table", table.order, cell.row), pos)


def units(document):
    list_group = None
    previous = None
    for block in document.blocks:
        if block.kind == "table":
            yield from table_units(block)
            list_group = previous = None
            continue
        if not block.text.strip():
            continue
        if block.kind == "list_item":
            if list_group is None:
                list_group = previous or ("list", block.order)
            group = list_group
        else:
            list_group = None
            group = ("section", block.section, block.headings)
        previous = group
        yield Unit(block, "", (block.headings, group))


def chunk_document(document, encoder, config=Config()):
    meta = document.metadata
    if config.maximum > encoder.limit:
        raise ValueError("Configured maximum exceeds encoder capacity")
    chunks = []

    def emit(parts, heading):
        original = "\n".join(part[0] for part in parts)
        indexed = heading + "\n".join(part[1] for part in parts)
        tokens = encoder.count(indexed)
        if tokens > config.maximum:
            raise ValueError("Chunk exceeds maximum")
        spans = [asdict(part[2]) for part in parts]
        identity = json.dumps([meta.document_id, spans], sort_keys=True).encode()
        chunks.append(dict(source_id="regulation:" + sha256(identity).hexdigest(),
                           document_id=meta.document_id, document=meta.filename,
                           section=spans[0]["section"], headings=parts[0][3],
                           text=original, index_text=indexed, department=meta.scope.department,
                           stage=meta.scope.stage, status=meta.status.value,
                           source_type=SourceType.REGULATION.value, order=len(chunks),
                           spans=spans, document_sha256=meta.checksum,
                           checksum=sha256(original.encode()).hexdigest(), tokens=tokens))

    for (_, path), siblings in groupby(units(document), key=lambda unit: (unit.group, unit.block.headings)):
        pending = []
        # Keep complete nearby titles when they fit. Every title is also indexed
        # as an original element, and the full path remains available for citation.
        titles = []
        for _, title in reversed(path):
            candidate = [title, *titles]
            if encoder.count("passage: " + "\n".join(candidate)) < config.target - config.overlap:
                titles = candidate
        heading = "passage: " + "".join(title + "\n" for title in titles)
        for unit in siblings:
            b = unit.block
            if not b.text.strip():
                continue
            label = b.label or b.section
            prefix = (unit.context + "\n" if unit.context else "") + (label + " " if label else "")
            def part(start, end):
                raw = b.text[start:end]
                return raw, prefix + raw, Span(b.order, start, end, b.section, unit.cells), b.headings

            full = part(0, len(b.text))
            candidate = heading + "\n".join(p[1] for p in [*pending, full])
            if pending and encoder.count(candidate) > config.target:
                emit(pending, heading)
                pending = []
            if encoder.count(heading + full[1]) <= config.target:
                pending.append(full)
                continue
            # Use original character offsets, never decode normalized tokenizer output.
            start = 0
            while start < len(b.text):
                rest = b.text[start:]
                offsets = encoder.offsets(rest)
                if not offsets or not rest.strip():
                    break
                budget = max(config.target, encoder.count(heading + prefix) + 1)
                budget = min(budget, config.maximum)
                limit = min(len(offsets), budget)
                end = len(rest) if limit == len(offsets) else offsets[limit][0]
                while end > 0 and encoder.count(heading + prefix + rest[:end]) > budget:
                    limit -= 1
                    if limit <= 0:
                        end = 0
                        break
                    end = offsets[limit][0]
                if end <= 0 or not rest[:end].strip():
                    raise ValueError("Source context leaves insufficient token budget")
                if end < len(rest):
                    # Prefer a sentence/word boundary near the token boundary.
                    floor = offsets[min(limit - 1, int(limit * .8))][0]
                    for separator in (". ", "; ", "\n", " "):
                        boundary = rest.rfind(separator, floor, end)
                        if boundary >= 0:
                            end = boundary + len(separator)
                            break
                emit([part(start, start + end)], heading)
                if end == len(rest):
                    break
                used = [a for a, z in offsets[:limit] if z <= end]
                advance = end
                if config.overlap and len(used) > config.overlap:
                    advance = used[-config.overlap] or end
                start += advance
        if pending:
            emit(pending, heading)
    return chunks
