"""Windows entry point: explicit offline fixtures, never an external model call."""
import os
from pathlib import Path

from dotenv import dotenv_values

from backend.main import _bounded_env, create_app

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "storage" / "windows"


def create_application():
    mode = os.environ.get("REGLAMENT_MODE", "disabled")
    if mode not in {"disabled", "demo-offline"}:
        raise RuntimeError("This launcher accepts disabled or demo-offline only. No synthetic fallback.")
    frontend = ROOT / "frontend" / "dist"
    if not (frontend / "index.html").is_file():
        raise RuntimeError("Build frontend first: npm --prefix frontend run build")
    config = {**dotenv_values(ROOT / ".env", interpolate=False), **os.environ}
    model = None
    if mode == "demo-offline":
        from backend.demo_model import FixtureModel
        model = FixtureModel()
    # Do not load SMTP or LLM settings. Only the administrative secret is read.
    return create_app(
        demo_mode=mode == "demo-offline", model=model,
        db_path=STATE / "storage" / ("demo.sqlite3" if model else "maintenance.sqlite3"),
        quality_secret=config.get("QUALITY_ADMIN_SECRET"),
        quality_actor=config.get("QUALITY_ADMIN_NAME") or "quality-admin",
        frontend_dir=frontend, attempts=1,
        per_minute=_bounded_env("AGENT_PER_MINUTE", 12, 1, 120),
        daily_attempts=_bounded_env("AGENT_DAILY_ATTEMPTS", 80, 1, 1000),
    )
