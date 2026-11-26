"""Opt-in synthetic search fixture for a local demonstration."""

import re

from shared.contracts import SearchResult, SourceStatus, SourceType

_FIXTURES = (
    ("ib", "presale", "1.1", "Опытный макет на пресейле принимает дежурный наблюдатель."),
    ("tech", "presale", "1.2", "Опытный макет на пресейле принимает координатор демонстрации."),
    ("tech", "implementation", "2.1", "Опытный макет на этапе реализации принимает руководитель стенда."),
    ("tech", "implementation", "2.2", "На этапе реализации руководитель стенда фиксирует приёмку опытного макета в журнале «Сапфир»."),
    ("dbrct", "implementation", "3.1", "Калибровку макета согласует инженер учебной мастерской."),
    ("tech", "implementation", "4.1", "Акт испытаний опытного макета подписывает руководитель стенда."),
    ("tech", "implementation", "4.2", "Акт испытаний опытного макета подписывает координатор испытаний."),
    ("tech", "implementation", "5.1", "Для синего макета нужен бумажный ярлык. Для красного макета нужен магнитный ярлык."),
)
_IGNORED = {"какой", "какая", "кто", "куда", "когда", "где", "проект", "это", "этап", "реали",
            "пресе", "дирек", "техни", "опытн", "доста", "поряд", "отвеч", "сведе"}


def _words(text):
    return {word[:5] for word in re.findall(r"[а-яё0-9]{4,}", text.casefold())} - _IGNORED


class DemoRetrieval:
    def search_documents(self, question, top_k=8, department=None, stage=None):
        current, *context = question.splitlines()
        words, previous = _words(current), _words(" ".join(context))
        found = []
        for scope, phase, section, text in _FIXTURES:
            if department and scope not in ((department,) if isinstance(department, str) else department):
                continue
            if stage and phase not in ((stage,) if isinstance(stage, str) else stage):
                continue
            hits = _words(text)
            direct_hits = words & hits
            if direct_hits:
                score = 4 * len(direct_hits) + len(previous & hits)
                found.append(SearchResult(
                    source_id=f"regulation:demo-{scope}:{section}", source_type=SourceType.REGULATION,
                    document=f"Синтетический демонстрационный регламент ({scope})",
                    text=text, section=section, score=float(score), status=SourceStatus.UNKNOWN,
                    department=scope, stage=phase, document_id=f"demo-{scope}",
                ))
        return sorted(found, key=lambda source: -source.score)[:top_k]

    def upsert_expert_answer(self, answer):
        raise NotImplementedError("The synthetic index is read-only")

    def delete_expert_answer(self, source_id, revision):
        raise NotImplementedError("The synthetic index is read-only")
