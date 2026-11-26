"""Evidence windows and explicitly confirmable applicability conditions."""

import re


def conditions_map(conditions: str | None) -> dict[str, str]:
    if not conditions:
        return {}
    facts = {}
    for clause in conditions.split(";"):
        match = re.fullmatch(r"\s*([\w -]{1,40})=([^;=\r\n]{1,80})\s*", clause)
        if not match:
            raise ValueError("invalid condition")
        key, value = (part.strip().casefold() for part in match.groups())
        if not key or not value or key in facts or len(facts) >= 4:
            raise ValueError("invalid condition")
        facts[key] = value
    return facts


_FACT_STOP = {"котор", "пожал", "такой", "какой", "какая", "почему", "нужно", "могут"}


def context_terms(value: str) -> set[str]:
    return {word[:5] for word in re.findall(r"[^\W_]{3,}", value.casefold())
            if word[:5] not in _FACT_STOP}


def evidence_window(text: str, question: str, limit: int) -> tuple[str, int] | None:
    """Find complete sentence/paragraph spans without joining nonadjacent text."""
    if len(text) <= limit:
        return text, 0
    terms = context_terms(question)
    boundaries = [0, *(match.end() for match in re.finditer(
        r"\n+| (?=[А-ЯA-Z])(?<=[.!?] )", text
    )), len(text)]
    spans = []
    for start, end in zip(boundaries, boundaries[1:]):
        raw = text[start:end]
        unit = raw.strip()
        if unit and len(unit) <= limit:
            offset = start + len(raw) - len(raw.lstrip())
            score = len(context_terms(unit) & terms)
            spans.append((offset, offset + len(unit), score))
    if not spans:
        # A paragraph with no sentence boundary may still have a self-contained clause.
        for match in re.finditer(r"[^;\n]+(?:;|$)", text):
            raw = match.group()
            unit = raw.strip()
            if unit and len(unit) <= limit:
                start = match.start() + len(raw) - len(raw.lstrip())
                spans.append((start, start + len(unit), len(context_terms(unit) & terms)))
    if not spans:
        return None
    index = max(range(len(spans)), key=lambda i: spans[i][2])
    start, end, score = spans[index]
    if not score:
        return None
    # Retain adjacent text where it fits, especially preceding applicability conditions.
    if index and text[spans[index - 1][1]:start].isspace() and end - spans[index - 1][0] <= limit:
        start = spans[index - 1][0]
    if index + 1 < len(spans) and text[end:spans[index + 1][0]].isspace() and spans[index + 1][1] - start <= limit:
        end = spans[index + 1][1]
    return text[start:end], start
