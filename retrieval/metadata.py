"""Local evidence extraction, revision-bound review and lifecycle evaluation."""
import argparse
from datetime import date, datetime
import json
from pathlib import Path
import re

from shared.contracts import INITIAL_DEPARTMENT_IDS, INITIAL_STAGE_IDS
from .loader import load_directory


LABELS = {
    "название": "title", "название документа": "title", "наименование документа": "title",
    "регистрационный номер": "registration", "версия": "version", "редакция": "version",
    "дата утверждения": "approved_on", "дата вступления в силу": "valid_from",
    "действует с": "valid_from", "действует по": "valid_until", "дата окончания действия": "valid_until",
    "статус": "state", "подразделение": "department", "применяется в подразделении": "department",
    "направление бизнеса": "business", "этап проекта": "stage", "область применения": "scope",
    "заменён документом": "replaced_by", "заменен документом": "replaced_by",
    "дата отмены": "change_on", "дата замены": "change_on", "бизнес-процесс": "process",
    "процесс": "process",
}
ALIASES = {
    "department": {"дирекция иб": "ib", "иб": "ib", "техническая дирекция": "tech", "дбрцт": "dbrct"},
    "stage": {"пресейл": "presale", "пресейловая деятельность": "presale", "реализация проекта": "implementation"},
    "state": {"действует": "approved", "действующий": "approved", "утверждён": "approved",
              "отменён": "revoked", "отменен": "revoked", "отменённый": "revoked",
              "утратил силу": "revoked", "заменён": "replaced", "заменен": "replaced"},
}
DATES = {"approved_on", "valid_from", "valid_until", "change_on"}
FIELDS = set(LABELS.values())
IDS = {"department": INITIAL_DEPARTMENT_IDS, "stage": INITIAL_STAGE_IDS}
STATES = {"approved", "unknown", "revoked", "replaced"}
DECLARATIONS = (
    ("state", r"(?:настоящий (?:регламент|документ) )?(отменён|отменен|утратил силу|заменён|заменен)"),
    ("replaced_by", r"настоящий (?:регламент|документ) замен[её]н документом\s+(.+)"),
    ("approved_on", r"утвержд[её]н(?:а|о)?\s+(.+)"),
    ("valid_from", r"(?:вступает в силу|действует)\s+с\s+(.+)"),
    ("valid_until", r"действует\s+по\s+(.+?)(?:\s+включительно)?"),
    ("change_on", r"(?:отмен[её]н|утратил силу)\s+с\s+(.+)"),
    ("version", r"(?:версия|редакция)\s+(?:№\s*)?(\d+(?:\.\d+)*)"),
    ("registration", r"регистрационный\s+(?:номер\s+|№\s*)(\S+)"),
)


def normalized_date(value):
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def extract(document, aliases=None):
    """Only labeled declarations; evidence is never itself a signed review."""
    evidence = []
    def record(text, block):
        label, sep, raw = text.partition(":")
        field = LABELS.get(label.strip().casefold())
        if not sep or not field:
            for field, pattern in DECLARATIONS:
                match = re.fullmatch(pattern, text.strip(), flags=re.I)
                if match:
                    raw = match[1]
                    break
            else:
                return
        if not raw.strip():
            return
        raw = raw.strip()
        value = raw
        if field in DATES:
            value = normalized_date(raw)
        elif field in ALIASES:
            vocab = {**ALIASES[field], **(aliases or {}).get(field, {})}
            value = vocab.get(raw.casefold(), raw if raw in IDS.get(field, STATES) else None)
        event = None
        if field == "change_on":
            event = "replaced" if label.strip().casefold() == "дата замены" else "revoked"
        evidence.append(dict(field=field, value=value, event=event, section=block.section, element=block.order,
                             headings=[title for _, title in block.headings],
                             document_id=document.metadata.document_id, quote=text))

    for block in document.elements():
        if block.kind == "table":
            rows = {}
            for cell in block.cells:
                rows.setdefault(cell.row, []).append(cell)
            for cells in rows.values():
                if len(cells) == 2 and cells[0].blocks and cells[1].blocks:
                    label = cells[0].blocks[0].text.rstrip(": ")
                    record(label + ": " + cells[1].blocks[0].text, cells[1].blocks[0])
        else:
            for line in block.text.splitlines():
                record(line, block)
    return evidence


def validate_fields(fields):
    if not isinstance(fields, dict) or set(fields) - FIELDS:
        raise ValueError("Invalid registry fields")
    for key, val in fields.items():
        if not isinstance(val, str) or not val.strip():
            raise ValueError("Expected one nonempty value per reviewed field")
        if key in IDS and val not in IDS[key] or key == "state" and val not in STATES:
            raise ValueError("Unknown registry identifier")
        if key in DATES and normalized_date(val) != val:
            raise ValueError("Expected ISO date")
    if fields.get("valid_from", "") > fields.get("valid_until", "9999-12-31"):
        raise ValueError("Inverted validity interval")


def read_registry(path):
    if path is None:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=unique_keys)
    if data.get("format") != 1 or not isinstance(data.get("documents"), dict):
        raise ValueError("Invalid registry format")
    ids, checksums = set(), set()
    for name, row in data["documents"].items():
        if not isinstance(row, dict) or set(row) - {"id", "sha256", "reviewer", "basis", "fields", "sections", "evidence"}:
            raise ValueError("Unexpected review record fields")
        if (Path(name).name != name or not isinstance(row.get("id"), str) or not row["id"].strip()
                or row["id"] in ids or row["sha256"] in checksums or not re.fullmatch("[a-f0-9]{64}", row["sha256"])):
            raise ValueError("Invalid document identity")
        ids.add(row["id"])
        checksums.add(row["sha256"])
        validate_fields(row.get("fields", {}))
        for section, fields in row.get("sections", {}).items():
            if not re.fullmatch(r"(?:.+::)?\d+(?:\.\d+)*", section):
                raise ValueError("Expected a numbered section")
            validate_fields(fields)
        if row.get("fields") or row.get("sections"):
            if not all(isinstance(row.get(k), str) and row[k].strip() for k in ("reviewer", "basis")):
                raise ValueError("Missing review provenance")
    return data["documents"]


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate registry key")
        result[key] = value
    return result


def profile(meta, evidence, entry):
    stale = bool(entry and entry["sha256"] != meta.checksum)
    reviewed = entry if entry and not stale else {}
    fields = dict(reviewed.get("fields", {}))
    # Existing programmatic loader arguments are explicitly verified by its caller.
    if not stale:
        for key, value in (("department", meta.scope.department), ("stage", meta.scope.stage),
                           ("state", meta.status.value), ("scope", meta.scope.conditions)):
            if value is not None:
                fields.setdefault(key, value)
    return dict(document=meta.filename, fields=fields, sections=reviewed.get("sections", {}),
                reviewer=reviewed.get("reviewer"), basis=reviewed.get("basis"),
                stale=stale, evidence=evidence)


def layers(section):
    yield None
    if section:
        parts = section.split(".")
        for i in range(1, len(parts) + 1):
            yield ".".join(parts[:i])


def policy(record, section, headings=()):
    """An ancestor restriction remains binding under a narrower reviewed scope."""
    fields, guards = {}, []
    for key in layers(section):
        confirmed = dict(record["fields"] if key is None else record["sections"].get(key, {}))
        if key is not None:
            for depth in range(1, len(headings) + 1):
                selector = " / ".join(headings[:depth]) + "::" + key
                confirmed.update(record["sections"].get(selector, {}))
        fields.update(confirmed)
        declared = {}
        events = set()
        for item in record["evidence"]:
            path = item.get("headings", [])
            if item["section"] == key and list(headings[:len(path)]) == path:
                declared.setdefault(item["field"], set()).add(item["value"])
                if item.get("event"):
                    events.add(item["event"])
        guard = dict(confirmed)
        conflicts = []
        if len(events) > 1:
            conflicts.append("change_on")
        elif events:
            guard["event"] = next(iter(events))
        for name in ("state", "valid_from", "valid_until", "change_on", "replaced_by"):
            values = declared.get(name, set())
            manual = confirmed.get(name)
            if manual == "unknown":
                manual = None
            if len(values) > 1 or None in values or (manual and values and manual not in values):
                conflicts.append(name)
            elif values and not manual:
                val = next(iter(values))
                if name != "state" or val in ("revoked", "replaced"):
                    guard[name] = val
        if guard.get("valid_from", "") > guard.get("valid_until", "9999-12-31"):
            conflicts.append("dates")
        if guard.get("state") == "approved" and guard.get("replaced_by") and not guard.get("change_on"):
            conflicts.append("replacement")
        guards.append(dict(**guard, conflicts=conflicts))
    return dict(fields=fields, guards=guards, stale=record["stale"])


def lifecycle(rule, today=None):
    day = (today or date.today()).isoformat()
    if rule["stale"]:
        return "needs_review"
    lower = max(g.get("valid_from", "") for g in rule["guards"])
    upper = min(g.get("valid_until", "9999-12-31") for g in rule["guards"])
    if lower > upper or any(g["conflicts"] for g in rule["guards"]):
        return "needs_review"
    for g in rule["guards"]:
        if g.get("approved_on", "") > day:
            return "needs_review"
        if g.get("valid_from", "") > day:
            return "future"
        if g.get("valid_until", "9999-12-31") < day:
            return "expired"
        if g.get("change_on", "") <= day:
            if g.get("state") == "revoked":
                return "revoked"
            if g.get("state") == "replaced" or g.get("replaced_by"):
                return "replaced"
            if g.get("change_on"):
                return g.get("event", "needs_review")
    return "approved" if rule["fields"].get("state") == "approved" else "unknown"


def statistics(profiles):
    result = {"documents": len(profiles)}
    for key in ("status", "version", "department", "stage"):
        known = sum((lifecycle(policy(p, None)) not in ("unknown", "needs_review") if key == "status"
                     else bool(p["fields"].get(key))) for p in profiles)
        result[key + "_known"] = known
        result[key + "_unknown"] = len(profiles) - known
        field = "state" if key == "status" else key
        result[key + "_declared"] = sum(any(e["field"] == field and e["value"] is not None
                                            for e in p["evidence"]) for p in profiles)
    return result


def relationships(candidates):
    """Describe candidate pairs without deciding whether either quotation is authoritative."""
    pairs = []
    for i, left in enumerate(candidates):
        for right in candidates[i + 1:]:
            if left["document_id"] == right["document_id"]:
                continue
            a, b = left["fields"], right["fields"]
            if any(c["lifecycle"] not in ("unknown", "approved") for c in (left, right)):
                kind = "review_history"
            elif any(a.get(k) and b.get(k) and a[k] != b[k]
                     for k in ("department", "stage", "business", "process", "scope")):
                kind = "different_applicability"
            elif a.get("process") and a["process"] == b.get("process") and left["chunk_sha256"] != right["chunk_sha256"]:
                kind = "review_same_process"
            else:
                kind = "verify_applicability"
            pairs.append(dict(left=left["source_id"], right=right["source_id"], reason=kind))
    return pairs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--documents", default="data/documents")
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--draft", type=Path)
    args = parser.parse_args()
    try:
        entries = read_registry(args.registry)
        by_hash = {v["sha256"]: v for v in entries.values()}
        batch = load_directory(args.documents, document_ids={k: v["id"] for k, v in entries.items()},
                               ids_by_checksum={k: v["id"] for k, v in by_hash.items()})
        if batch.errors:
            raise ValueError("Incomplete corpus")
        rows, profiles = {}, []
        for doc in batch.documents:
            meta = doc.metadata
            evidence = extract(doc)
            profiles.append(profile(meta, evidence, entries.get(meta.filename) or by_hash.get(meta.checksum)))
            rows[meta.filename] = dict(id=meta.document_id, sha256=meta.checksum, reviewer=None, basis=None,
                                       fields={}, sections={}, evidence=evidence)
        if args.draft:
            with args.draft.open("x", encoding="utf-8") as stream:
                json.dump(dict(format=1, documents=rows), stream, ensure_ascii=False, indent=2)
        print(json.dumps(statistics(profiles)))
        return 0
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        print('{"error":"registry_or_document_error"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
