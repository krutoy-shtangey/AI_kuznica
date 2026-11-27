"""Browser-test harness using the real FastAPI and SQLite with no paid services."""

import atexit
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.demo_model import FixtureModel
from backend.main import create_app
from backend.quality_mail import MailFailed


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
