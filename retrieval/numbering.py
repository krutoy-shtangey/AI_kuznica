"""Interpret Word paragraph properties and the document's numbering definitions."""
import re
from collections import defaultdict

from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE as RT


def value(node, path, default=None):
    child = node.find("/".join(qn("w:" + tag) for tag in path.split("/"))) if node is not None else None
    return default if child is None else child.get(qn("w:val"), default)


def properties(paragraph):
    """Direct properties precede paragraph style properties and their base styles."""
    yield paragraph._p.pPr
    style = paragraph.style
    seen = set()
    while style is not None and style.style_id not in seen:
        seen.add(style.style_id)
        yield style.element.pPr
        style = style.base_style


def inherited(props, key):
    return next((v for p in props if (v := value(p, key)) is not None), None)


def format_counter(n, fmt):
    if fmt in ("decimal", "decimalZero"):
        return str(n).zfill(2 if fmt == "decimalZero" else 1)
    if fmt in ("upperLetter", "lowerLetter") and n > 0:
        result = chr(65 + (n - 1) % 26) * ((n - 1) // 26 + 1)
        return result.lower() if fmt == "lowerLetter" else result
    if fmt in ("upperRoman", "lowerRoman") and 0 < n < 4000:
        result = ""
        for base, glyph in zip((1000,900,500,400,100,90,50,40,10,9,5,4,1),
                               ("M","CM","D","CD","C","XC","L","XL","X","IX","V","IV","I")):
            count, n = divmod(n, base)
            result += glyph * count
        return result.lower() if fmt == "lowerRoman" else result
    return None


class Numbering:
    def __init__(self, document):
        self.abstracts = {}
        self.instances = {}
        self.counts = defaultdict(dict)
        try:
            root = document.part.part_related_by(RT.NUMBERING).element
        except KeyError:
            return
        for item in root.findall(qn("w:abstractNum")):
            self.abstracts[item.get(qn("w:abstractNumId"))] = item
        for item in root.findall(qn("w:num")):
            self.instances[item.get(qn("w:numId"))] = item

    def definition(self, instance, depth):
        abstract = self.abstracts.get(value(instance, "abstractNumId"))
        path = f"[@{qn('w:ilvl')}='{depth}']"
        override = instance.find(qn("w:lvlOverride") + path)
        level = override.find(qn("w:lvl")) if override is not None else None
        if level is None and abstract is not None:
            level = abstract.find(qn("w:lvl") + path)
        start = int(value(override, "startOverride", value(level, "start", "0")))
        return level, start

    def advance(self, props, style_id):
        num = inherited(props, "numPr/numId")
        if num in (None, "0"):
            return None, None, None
        instance = self.instances.get(num)
        depth = inherited(props, "numPr/ilvl")
        if instance is None:
            return int(depth or 0), None, None
        if depth is None:
            depth = next((i for i in range(9)
                          if value(self.definition(instance, i)[0], "pStyle") == style_id), 0)
        depth = int(depth)
        level, start = self.definition(instance, depth)
        counts = self.counts[num]
        for child in sorted(list(counts)):
            child_level, _ = self.definition(instance, child)
            restart = int(value(child_level, "lvlRestart", str(child)))
            if child > depth and 0 < restart and depth < restart:
                del counts[child]
        counts[depth] = counts.get(depth, start - 1) + 1
        pattern = value(level, "lvlText")
        fmt = value(level, "numFmt", "decimal")
        if pattern is None or fmt == "none":
            return depth, None, None
        if fmt == "bullet":
            return depth, pattern, None
        legal = value(level, "isLgl", "1") if level.find(qn("w:isLgl")) is not None else "0"
        def replace(match):
            index = int(match[1]) - 1
            ref, initial = self.definition(instance, index)
            if ref is None or index > depth:
                raise ValueError
            number_fmt = "decimal" if legal not in ("0", "false", "off") else value(ref, "numFmt", "decimal")
            rendered = format_counter(counts.get(index, initial), number_fmt)
            if rendered is None:
                raise ValueError
            return rendered
        try:
            label = re.sub(r"%([1-9])", replace, pattern)
        except ValueError:
            return depth, None, None
        return depth, label, label.rstrip(".)") if re.search(r"%[1-9]", pattern) else None
