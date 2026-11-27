"""Offline model for the synthetic demonstration corpus only; no network clients."""
import json

from backend.agent import EvidenceDraft
from backend.llm import AttemptMetrics, CallMetrics, ErrorCode, JsonMode, LLMError, LLMResult


class FixtureModel:
    """Deterministic evidence classification only for the browser-test corpus."""

    def complete_json(self, _instruction, data, schema):
        assert schema is EvidenceDraft
        payload = json.loads(data)
        sources = payload["sources"]
        current = payload["question"]
        if current.startswith("Сбой модели"):
            raise LLMError(ErrorCode.UNAVAILABLE)
        kind, field, question = "ANSWER", None, None
        if "акт испытаний" in current:
            selected = [item for item in sources if item["section"] in {"4.1", "4.2"}]
            kind = "CONFLICT"
        elif "фиксируют" in current:
            selected = [item for item in sources if item["section"] == "2.2"]
        elif "Кто принимает опытный макет" in current and not payload["confirmed_department"]:
            selected = [item for item in sources if item["section"] in {"1.1", "1.2"}]
            kind, field, question = "CLARIFY", "department", "Какое подразделение?"
        elif "Кто принимает опытный макет" in current and not payload["confirmed_stage"]:
            selected = [item for item in sources if item["section"] in {"1.2", "2.1"}]
            kind, field, question = "CLARIFY", "stage", "Какой этап?"
        elif "Кто принимает опытный макет" in current:
            selected = [item for item in sources if item["section"] == "2.1"]
        else:
            selected = [item for item in sources if item["source_type"] == "expert_answer"][:1]
            if not selected:
                kind = "NO_ANSWER"
        evidence = [{"source_id": item["source_id"], "quote": item["excerpt"]} for item in selected]
        value = schema.model_validate_json(json.dumps({
            "result_type": kind, "field": field, "question": question, "evidence": evidence,
        }))
        metrics = CallMetrics((AttemptMetrics("test-model", 0.05, JsonMode.OBJECT, 40, 10, 50),), 0.05)
        return LLMResult(value, metrics)

