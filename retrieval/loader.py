"""Load local DOCX regulations. Run as a module for anonymous aggregate counts."""
import argparse
import hashlib
import json
import re
import stat
from io import BytesIO
from itertools import count
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph

from shared.contracts import ApplicabilityScope, SourceStatus
from .models import Batch, Cell, Element, LoadedDocument, Metadata
from .numbering import Numbering, inherited, properties

# Require a delimiter for single numbers to avoid treating "30 дней" as a clause.
CLAUSE = re.compile(
    r"^\s*(?:(?:пункт|п\.)\s+(\d+(?:\.\d+)*)|(\d+(?:\.\d+)+|\d+(?=[.)])))"
    r"[.)]?(?=\s|$)", re.IGNORECASE,
)
DATE_PREFIX = re.compile(r"^\s*(?:\d{1,2}\.\d{1,2}\.\d{4}|\d{4}\.\d{1,2}\.\d{1,2})(?=\W|$)")


def paragraph_text(paragraph):
    # Include displayed field results, inline content controls and accepted insertions.
    # Drawing and deleted-text subtrees are separate from the paragraph's reading flow.
    nodes = paragraph._p.xpath(
        ".//w:r[not(ancestor::w:del or ancestor::w:moveFrom or ancestor::w:drawing"
        " or ancestor::w:pict or ancestor::w:object or ancestor::*[local-name()='Fallback'])]"
    )
    return "".join(run.text for run in nodes)


def body_blocks(container, parent):
    for child in container:
        if child.tag == qn("w:p"):
            yield Paragraph(child, parent)
        elif child.tag == qn("w:tbl"):
            yield Table(child, parent)
        elif child.tag in {qn("w:sdt"), qn("w:sdtContent"), qn("w:customXml"),
                           qn("w:ins"), qn("w:moveTo")}:
            yield from body_blocks(child, parent)


class Reader:
    def __init__(self, document, metadata):
        self.document = document
        self.meta = metadata
        self.numbering = Numbering(document)
        self.order = count()

    def blocks(self, container, parent, headings=(), section=None):
        result = []
        for block in body_blocks(container, parent):
            order = next(self.order)
            if isinstance(block, Table):
                element = Element(self.meta, order, "table", "", section, headings)
                self.table(block, element)
            else:
                props = tuple(properties(block))
                outline = inherited(props, "outlineLvl")
                depth, label, automatic = self.numbering.advance(props, block.style.style_id)
                text = paragraph_text(block)
                match = None if DATE_PREFIX.match(text) else CLAUSE.match(text)
                clause = automatic or ((match[1] or match[2]) if match else None)
                if outline is not None and 0 <= int(outline) < 9:
                    level = int(outline) + 1
                    headings = tuple(h for h in headings if h[0] < level) + ((level, text),)
                    section = clause
                    kind = "heading"
                elif depth is not None:
                    kind = "list_item"
                else:
                    kind = "paragraph"
                if clause:
                    section = clause
                element = Element(self.meta, order, kind, text, section, headings, label, depth)
            result.append(element)
        return result

    def table(self, table, element):
        header_rows = []
        active = {}
        lines = []
        for row_index, row in enumerate(table.rows):
            flag = row._tr.find("./" + qn("w:trPr") + "/" + qn("w:tblHeader"))
            if flag is not None and flag.get(qn("w:val")) not in ("0", "false", "off"):
                header_rows.append(row_index)
            column = row.grid_cols_before
            continuing = {}
            row_text = []
            for tc in row._tr.tc_lst:
                span = tc.grid_span
                key = column, span
                if tc.vMerge == "continue" and key in active:
                    cell = active[key]
                    cell.rows += 1
                    continuing[key] = cell
                else:
                    cell = Cell(row_index, column, span)
                    cell.blocks = self.blocks(tc, _Cell(tc, table), element.headings, element.section)
                    element.cells.append(cell)
                    if tc.vMerge == "restart":
                        continuing[key] = cell
                    row_text.append("\n".join(block.text for block in cell.blocks))
                column += span
            active = continuing
            lines.append("\t".join(row_text))
        element.text = "\n".join(lines)
        element.header_rows = tuple(header_rows)
        element.shape = len(table.rows), len(table.columns)


def load_directory(directory, *, document_ids=None, scopes=None, statuses=None, known_checksums=None,
                   ids_by_checksum=None):
    """Mappings keyed by exact filename may supply verified metadata from the caller."""
    batch = Batch()
    seen = set()
    try:
        paths = sorted(Path(directory).iterdir(), key=lambda p: (
            p.name not in (document_ids or {}), p.name.casefold(), p.name))
    except OSError:
        batch.errors += 1
        return batch
    for path in paths:
        try:
            if path.name.startswith("~$") or not stat.S_ISREG(path.lstat().st_mode):
                continue
            if path.suffix.lower() != ".docx":
                batch.unsupported += 1
                continue
            data = path.read_bytes()
            checksum = hashlib.sha256(data).hexdigest()
            if checksum in seen:
                batch.duplicates += 1
                continue
            identifier = (document_ids or {}).get(path.name) or (ids_by_checksum or {}).get(checksum) or str(
                uuid5(NAMESPACE_URL, "AI_kuznica/regulation/" + path.name.casefold()))
            meta = Metadata(identifier, path.name, checksum,
                            (scopes or {}).get(path.name, ApplicabilityScope()),
                            (statuses or {}).get(path.name, SourceStatus.UNKNOWN))
            if known_checksums and known_checksums.get(identifier) == checksum:
                batch.reused.append(meta)
                seen.add(checksum)
                continue
            document = Document(BytesIO(data))
            reader = Reader(document, meta)
            blocks = reader.blocks(document.element.body, document)
        except Exception:
            # Isolate parser/ZIP/IO failures per input. Exceptions can contain source text.
            batch.errors += 1
            continue
        batch.documents.append(LoadedDocument(meta, blocks))
        seen.add(checksum)
    return batch


def main():
    parser = argparse.ArgumentParser(description="Print anonymous DOCX ingestion counts.")
    parser.add_argument("directory", type=Path)
    batch = load_directory(parser.parse_args().directory)
    print(json.dumps(batch.statistics()))
    return int(batch.errors > 0)


if __name__ == "__main__":
    raise SystemExit(main())
