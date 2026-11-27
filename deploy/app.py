"""VPS entry point for the existing application factory."""

import os
from pathlib import Path
from urllib.parse import urlsplit

from backend.llm import LLMSettings
from backend.main import _bounded_env, create_app

ROOT = Path(__file__).resolve().parents[1]


def create_application():
    mode = os.environ.get("REGLAMENT_MODE", "disabled")
    if mode not in {"disabled", "demo", "retrieval"}:
        raise ValueError("REGLAMENT_MODE must be disabled, demo, or retrieval.")
    state = Path(os.environ.get("REGLAMENT_STATE_DIR", "/var/lib/reglament-ai"))
    if not state.is_absolute():
        raise ValueError("REGLAMENT_STATE_DIR must be an absolute path.")
    frontend = ROOT / "frontend" / "dist"
    if not (frontend / "index.html").is_file():
        raise RuntimeError("Build the frontend before starting the service.")

    retrieval_factory = None
    if mode == "retrieval":
        # Fail before loading the index, in addition to the backend's own guard.
        endpoint = urlsplit(LLMSettings.from_env().base_url)
        if endpoint.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise RuntimeError("Corporate retrieval requires a local generation endpoint in this release.")
        from retrieval.agent_adapter import retrieval_factory

        os.environ.setdefault("RETRIEVAL_INDEX", str(state / "index"))
        os.environ.setdefault("RETRIEVAL_MODEL_ROOT", str(state / "models"))

    db_name = {"disabled": "maintenance.sqlite3", "demo": "demo.sqlite3", "retrieval": "agent.sqlite3"}[mode]
    return create_app(
        retrieval_factory=retrieval_factory,
        demo_mode=mode == "demo",
        db_path=state / "storage" / db_name,
        load_quality_env=True,
        frontend_dir=frontend,
        attempts=1,
        per_minute=_bounded_env("AGENT_PER_MINUTE", 12, 1, 120),
        daily_attempts=_bounded_env("AGENT_DAILY_ATTEMPTS", 80, 1, 1000),
    )
