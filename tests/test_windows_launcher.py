"""Offline launch must survive restarts without constructing network clients."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from deploy.windows import app as launcher


class WindowsLauncherTests(unittest.TestCase):
    def test_offline_restart_isolation_and_no_external_clients(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            frontend = root / "frontend" / "dist"
            frontend.mkdir(parents=True)
            (frontend / "index.html").write_text("<!doctype html><title>fixture</title>", encoding="utf-8")
            with (patch.object(launcher, "ROOT", root),
                  patch.object(launcher, "STATE", root / "state"),
                  patch.dict(os.environ, {"REGLAMENT_MODE": "demo-offline", "SMTP_ENABLED": "1"}),
                  patch.object(launcher, "dotenv_values", return_value={"QUALITY_ADMIN_SECRET": "x" * 40}),
                  patch("backend.main.LLMClient.__init__", side_effect=AssertionError("External LLM constructed")),
                  patch("backend.main.SMTPMailer", side_effect=AssertionError("SMTP constructed"))):
                with TestClient(launcher.create_application()) as client:
                    self.assertEqual(client.get("/api/health").json()["retrieval"], "demo")
                    self.assertEqual(client.get("/").status_code, 200)
                    self.assertEqual(client.get("/api/quality/appeals").status_code, 401)
                    dialogue = client.post("/api/dialogues").json()
                    url = f"/api/dialogues/{dialogue['dialogue_id']}/messages"
                    headers = {"Authorization": "Bearer " + dialogue["access_token"]}
                    response = client.post(url, headers=headers, json={"message": "Кто принимает опытный макет?"})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["result"]["result_type"], "CLARIFY")
                with TestClient(launcher.create_application()) as client:
                    self.assertEqual(len(client.get(url, headers=headers).json()["messages"]), 1)
                    self.assertEqual(client.get(url, headers={"Authorization": "Bearer " + "y" * 40}).status_code, 404)

    def test_unsupported_mode_never_falls_back(self):
        with patch.dict(os.environ, {"REGLAMENT_MODE": "retrieval"}):
            with self.assertRaisesRegex(RuntimeError, "No synthetic fallback"):
                launcher.create_application()


if __name__ == "__main__":
    unittest.main()
