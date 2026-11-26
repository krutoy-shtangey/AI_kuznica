import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from retrieval.loader import load_directory
from shared.contracts import ApplicabilityScope, SourceStatus


def tag(parent, name, **attributes):
    node = OxmlElement("w:" + name)
    for key, value in attributes.items():
        node.set(qn("w:" + key), str(value))
    parent.append(node)
    return node


class LoaderTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "Синтетический регламент.DOCX"
        self.doc = Document()

    def load(self, **kwargs):
        self.doc.save(self.path)
        batch = load_directory(self.root, **kwargs)
        self.assertEqual(batch.errors, 0)
        return batch.documents[0]

    def test_order_headings_metadata_and_tables(self):
        self.doc.add_heading("1. Раздел", 1)
        self.doc.add_heading("1.2 Подраздел", 2)
        self.doc.add_paragraph("1.2.3) Первый пункт")
        table = self.doc.add_table(3, 3)
        tag(table.rows[0]._tr.get_or_add_trPr(), "tblHeader")
        table.cell(0, 0).merge(table.cell(0, 1)).text = "Показатель"
        table.cell(0, 2).text = "Итого"
        table.cell(1, 0).merge(table.cell(2, 0)).text = "Группа"
        table.cell(1, 1).text = "А"
        table.cell(2, 1).text = "Б"
        self.doc.add_paragraph("После таблицы")
        self.doc.add_heading("Другой раздел", 1)
        result = self.load()
        first, second, clause, grid, after, last = result.blocks
        self.assertEqual([x.kind for x in result.blocks],
                         ["heading", "heading", "paragraph", "table", "paragraph", "heading"])
        self.assertEqual(second.headings, ((1, first.text), (2, second.text)))
        self.assertEqual(clause.text, "1.2.3) Первый пункт")
        self.assertEqual((clause.section, grid.section, after.section), ("1.2.3",) * 3)
        self.assertIsNone(last.section)
        self.assertEqual(last.headings, ((1, last.text),))
        self.assertEqual((grid.header_rows, grid.shape), ((0,), (3, 3)))
        coords = {(c.row, c.column): (c.rows, c.columns, c.blocks[0].text) for c in grid.cells}
        self.assertEqual(coords[0, 0], (1, 2, "Показатель"))
        self.assertEqual(coords[1, 0], (2, 1, "Группа"))
        self.assertEqual(coords[2, 1], (1, 1, "Б"))
        self.assertNotIn((2, 0), coords)
        self.assertEqual(grid.text.count("Группа"), 1)
        elements = list(result.elements())
        self.assertEqual([e.order for e in elements], list(range(len(elements))))
        self.assertTrue(all(e.metadata is result.metadata for e in elements))
        self.assertEqual(result.metadata.filename, self.path.name)
        self.assertEqual(result.metadata.checksum, hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(result.metadata.scope, ApplicabilityScope())
        self.assertEqual(result.metadata.status, SourceStatus.UNKNOWN)

    def test_numbering_nested_restart_override_and_style(self):
        root = self.doc.part.numbering_part.element
        abstract = tag(root, "abstractNum", abstractNumId=50)
        for i, pattern in enumerate(("%1.", "%1.%2)")):
            level = tag(abstract, "lvl", ilvl=i)
            tag(level, "start", val=1)
            tag(level, "numFmt", val="decimal")
            tag(level, "lvlText", val=pattern)
        instance = tag(root, "num", numId=50)
        tag(instance, "abstractNumId", val=50)
        tag(tag(instance, "lvlOverride", ilvl=0), "startOverride", val=4)
        for depth in (0, 1, 1, 0, 1):
            para = self.doc.add_paragraph("Действие")
            num = para._p.get_or_add_pPr().get_or_add_numPr()
            num.get_or_add_numId().val = 50
            num.get_or_add_ilvl().val = depth
        self.doc.add_paragraph("Список", style="List Number")
        self.doc.add_paragraph("Маркер", style="List Bullet")
        blocks = self.load().blocks
        self.assertEqual([b.label for b in blocks[:6]], ["4.", "4.1)", "4.2)", "5.", "5.1)", "1."])
        self.assertEqual([b.list_level for b in blocks[:5]], [0, 1, 1, 0, 1])
        self.assertEqual([b.section for b in blocks[:5]], ["4", "4.1", "4.2", "5", "5.1"])
        self.assertEqual(blocks[6].kind, "list_item")
        self.assertTrue(blocks[6].label)

    def test_nested_table(self):
        cell = self.doc.add_table(1, 1).cell(0, 0)
        cell.text = "До"
        cell.add_table(1, 1).cell(0, 0).text = "Внутри"
        cell.paragraphs[-1].text = "После"
        blocks = self.load().blocks[0].cells[0].blocks
        self.assertEqual([b.text for b in blocks], ["До", "Внутри", "После"])

    def test_inline_fields_controls_and_date(self):
        para = self.doc.add_paragraph("12.08.2025 ")
        control = tag(tag(para._p, "sdt"), "sdtContent")
        tag(tag(control, "r"), "t").text = "Образец"
        field = tag(para._p, "fldSimple", instr=" DOCPROPERTY Title ")
        tag(tag(field, "r"), "t").text = " А"
        tag(tag(tag(para._p, "del"), "r"), "delText").text = "старое"
        element = self.load().blocks[0]
        self.assertEqual(element.text, "12.08.2025 Образец А")
        self.assertIsNone(element.section)

    def test_revision_and_verified_metadata(self):
        old = self.load()
        self.doc.add_paragraph("Дополнение")
        new = self.load(scopes={self.path.name: ApplicabilityScope("ib", "presale")})
        self.assertEqual(old.metadata.document_id, new.metadata.document_id)
        self.assertNotEqual(old.metadata.checksum, new.metadata.checksum)
        self.assertEqual(new.blocks[0].metadata.scope.department, "ib")
        mapped = self.load(document_ids={self.path.name: "reg-1"},
                           statuses={self.path.name: SourceStatus.APPROVED})
        self.assertEqual((mapped.metadata.document_id, mapped.metadata.status),
                         ("reg-1", SourceStatus.APPROVED))

    def test_mixed_directory_and_private_cli(self):
        marker = "private-synthetic-marker"
        self.doc.add_paragraph(marker)
        self.doc.save(self.path)
        shutil.copyfile(self.path, self.root / "Дубликат.docx")
        (self.root / "0-corrupt.docx").write_bytes(b"broken")
        (self.root / "~$lock.docx").write_bytes(b"lock")
        (self.root / "other.pdf").write_bytes(b"unsupported")
        batch = load_directory(self.root)
        self.assertEqual((len(batch.documents), batch.errors, batch.duplicates, batch.unsupported),
                         (1, 1, 1, 1))
        run = subprocess.run([sys.executable, "-B", "-m", "retrieval.loader", str(self.root)],
                             capture_output=True, text=True)
        self.assertEqual(run.returncode, 1)
        self.assertEqual(run.stderr, "")
        self.assertEqual(json.loads(run.stdout),
                         dict(documents=1, paragraphs=1, tables=0, elements=1, errors=1))

    def test_empty_and_missing(self):
        empty = load_directory(self.root)
        self.assertEqual(empty.statistics(),
                         dict(documents=0, paragraphs=0, tables=0, elements=0, errors=0))
        missing = load_directory(self.root / "absent")
        self.assertEqual((missing.documents, missing.errors), ([], 1))
