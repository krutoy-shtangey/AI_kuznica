"""Browser-test harness using the real FastAPI and SQLite with no paid services."""

import atexit
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.agent import EvidenceDraft
from backend.llm import AttemptMetrics, CallMetrics, ErrorCode, JsonMode, LLMError, LLMResult
from backend.main import create_app
from backend.quality_mail import MailFailed


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


class FailedMail:
    def send(self, _appeal):
        raise MailFailed


if __name__ == "__main__":
    directory = ROOT / "storage"
    directory.mkdir(exist_ok=True)
    database = TemporaryDirectory(dir=directory, prefix="frontend-test-")
    atexit.register(database.cleanup)
    frontend = ROOT / "frontend" / "dist"
    uvicorn.run(create_app(
        demo_mode=True, model=FixtureModel(), db_path=Path(database.name) / "test.sqlite3",
        quality_secret=os.environ["UI_TEST_ADMIN_SECRET"], quality_actor="Специалист проверки",
        mailer=FailedMail(), per_minute=120, daily_attempts=300,
        frontend_dir=frontend if (frontend / "index.html").is_file() else None,
    ), host="127.0.0.1", port=int(os.environ.get("UI_TEST_API_PORT", "8117")), access_log=False)
