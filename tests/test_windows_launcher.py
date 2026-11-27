"""Offline launch must survive restarts without constructing network clients."""
import os
import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from backend.agent import EvidenceDraft
from backend.main import create_app, require_corporate_endpoint
from backend.llm import LLMSettings
from deploy.windows import app as launcher


class WindowsLauncherTests(unittest.TestCase):
    def test_proxyapi_daily_limit_is_opt_in_and_capped_at_150(self):
        settings = LLMSettings("https://api.proxyapi.ru/v1", "synthetic-test-key",
                               "qwen/qwen3-30b-a3b-instruct-2507")
        with (patch.dict(os.environ, {"REGLAMENT_MODE": "retrieval-proxyapi",
                                      "REGLAMENT_ALLOW_CORPORATE_PROXYAPI": "1"}),
              patch.object(Path, "is_file", return_value=True),
              patch.object(launcher, "dotenv_values", return_value={}),
              patch.object(launcher.LLMSettings, "from_env", return_value=settings),
              patch.object(launcher, "create_app") as factory):
            os.environ.pop("AGENT_DAILY_ATTEMPTS", None)
            launcher.create_application()
            self.assertEqual(factory.call_args.kwargs["daily_attempts"], 15)
            os.environ["AGENT_DAILY_ATTEMPTS"] = "150"
            launcher.create_application()
            self.assertEqual(factory.call_args.kwargs["daily_attempts"], 150)
            self.assertEqual(factory.call_args.kwargs["attempts"], 1)
            os.environ["AGENT_DAILY_ATTEMPTS"] = "151"
            with self.assertRaisesRegex(ValueError, "AGENT_DAILY_ATTEMPTS"):
                launcher.create_application()

    def test_work_mode_persists_limits_and_separate_ngrok_opt_in_in_dotenv(self):
        settings = LLMSettings("https://api.proxyapi.ru/v1", "synthetic-test-key",
                               "qwen/qwen3-30b-a3b-instruct-2507")
        config = {"REGLAMENT_ALLOW_CORPORATE_PROXYAPI": "1", "AGENT_DAILY_ATTEMPTS": "150",
                  "AGENT_PER_MINUTE": "12", "REGLAMENT_ALLOW_PUBLIC": "1",
                  "REGLAMENT_PUBLIC_HOST": "assigned.ngrok-free.app"}
        with (patch.dict(os.environ, {"REGLAMENT_MODE": "retrieval-proxyapi"}, clear=True),
              patch.object(Path, "is_file", return_value=True),
              patch.object(launcher, "dotenv_values", return_value=config),
              patch.object(launcher.LLMSettings, "from_env", return_value=settings),
              patch.object(launcher, "create_app") as factory):
            launcher.create_application()
            self.assertEqual(factory.call_args.kwargs["daily_attempts"], 150)
            self.assertEqual(factory.call_args.kwargs["per_minute"], 12)
            self.assertEqual(factory.return_value.add_middleware.call_args.kwargs["public_host"],
                             "assigned.ngrok-free.app")
            os.environ["REGLAMENT_MODE"] = "retrieval-offline"
            launcher.create_application()
            self.assertIsNone(factory.return_value.add_middleware.call_args.kwargs["public_host"])
            os.environ.update(REGLAMENT_MODE="retrieval-proxyapi", REGLAMENT_PUBLIC_HOST="*.ngrok-free.app")
            with self.assertRaisesRegex(ValueError, "REGLAMENT_PUBLIC_HOST"):
                launcher.create_application()

    def test_corporate_proxyapi_requires_explicit_mode_and_exact_destination(self):
        settings = LLMSettings("https://api.proxyapi.ru/v1", "synthetic-test-key",
                               "qwen/qwen3-30b-a3b-instruct-2507")
        with self.assertRaises(ValueError):
            require_corporate_endpoint(settings, False)
        require_corporate_endpoint(settings, True)
        for change in ({"base_url": "http://api.proxyapi.ru/v1"},
                       {"base_url": "https://api.proxyapi.ru:444/v1"},
                       {"base_url": "https://api.proxyapi.ru/v2"},
                       {"base_url": "https://api.proxyapi.ru.example.invalid/v1"},
                       {"model": "other-model"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                require_corporate_endpoint(replace(settings, **change), True)
        with (patch.dict(os.environ, {"REGLAMENT_MODE": "retrieval-proxyapi",
                                      "REGLAMENT_ALLOW_CORPORATE_PROXYAPI": "0"}),
              patch.object(Path, "is_file", return_value=True),
              patch.object(launcher, "dotenv_values", return_value={})):
            with self.assertRaisesRegex(RuntimeError, "REGLAMENT_ALLOW_CORPORATE_PROXYAPI"):
                launcher.create_application()

    def test_approved_mode_constructs_one_managed_client_without_calling_it(self):
        settings = LLMSettings("https://api.proxyapi.ru/v1", "synthetic-test-key",
                               "qwen/qwen3-30b-a3b-instruct-2507", max_tokens=1200)
        with TemporaryDirectory() as directory:
            app = create_app(retrieval=object(), db_path=Path(directory) / "state.sqlite3",
                             allow_corporate_proxyapi=True, workers=False)
            with (patch("backend.main.LLMSettings.from_env", return_value=settings),
                  patch("backend.main.LLMClient") as client_factory,
                  TestClient(app) as client):
                self.assertEqual(client.get("/health").json()["model"], "proxyapi-qwen")
                client_factory.assert_called_once_with(replace(settings, max_tokens=768), max_attempts=1)
                client_factory.return_value.complete_json.assert_not_called()

    def test_private_catalogue_rejects_forwarded_and_cross_origin_requests(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            app = create_app(db_path=root / "private.sqlite3", workers=False, frontend_dir=root,
                             catalogue=lambda: [{"document": "Local fixture"}], model_is_fixture=True)
            app.add_middleware(launcher.DirectLocalRequests)
            with TestClient(app, base_url="http://127.0.0.1:8001") as client:
                response = client.get("/api/knowledge")
                self.assertEqual(response.json()["documents"], [{"document": "Local fixture"}])
                self.assertEqual(response.headers["x-robots-tag"], "noindex, nofollow")
                self.assertEqual(client.get("/api/health").json()["model"], "offline-fixture")
                for headers in ({"Host": "external.invalid"}, {"X-Forwarded-Host": "external.invalid"},
                                {"Origin": "https://external.invalid"}, {"Sec-Fetch-Site": "cross-site"}):
                    with self.subTest(headers=headers):
                        response = client.get("/api/knowledge", headers=headers)
                        self.assertEqual(response.status_code, 403)
                        self.assertNotIn("Local fixture", response.text)

    def test_ngrok_allows_only_public_routes_with_tls_and_isolated_dialogues(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "index.html").write_text("<title>fixture</title>", encoding="utf-8")
            app = create_app(db_path=root / "test.sqlite3", workers=False, frontend_dir=root,
                             catalogue=lambda: [], quality_secret="x" * 40)
            host = "assigned.ngrok-free.app"
            app.add_middleware(launcher.DirectLocalRequests, port=8002, public_host=host)
            with TestClient(app, base_url=f"https://{host}", headers={"X-Forwarded-Proto": "https"}) as client:
                self.assertEqual(client.get("/", headers={"Sec-Fetch-Site": "cross-site"}).status_code, 200)
                self.assertEqual(client.get("/robots.txt").text, "User-agent: *\nDisallow: /\n")
                self.assertEqual(client.get("/api/knowledge").status_code, 200)
                dialogue = client.post("/api/dialogues", headers={"Origin": f"https://{host}"}).json()
                history = f"/api/dialogues/{dialogue['dialogue_id']}/messages"
                self.assertEqual(client.get(history, headers={"Authorization": "Bearer " + "z" * 40}).status_code, 404)
                self.assertEqual(client.get(history, headers={
                    "Authorization": f"Bearer {dialogue['access_token']}",
                }).status_code, 200)
                for path in ("/api/quality/appeals", "/api/docs", "/openapi.json", "/.env",
                             "/storage/windows/index/CURRENT", "/assets/%2e%2e/index.html", "/docs"):
                    with self.subTest(path=path):
                        blocked = client.get(path, headers={"Authorization": "Bearer " + "x" * 40})
                        self.assertEqual(blocked.status_code, 403)
                        self.assertEqual(blocked.headers["x-robots-tag"], "noindex, nofollow")
                for headers in ({"X-Forwarded-Proto": "http"}, {"Host": "unknown.ngrok-free.app"},
                                {"Origin": "https://unrelated.invalid"}, {"Sec-Fetch-Site": "cross-site"},
                                {"Host": "127.0.0.1:8002"}):
                    with self.subTest(headers=headers):
                        self.assertEqual(client.post("/api/dialogues", headers=headers).status_code, 403)

    def test_fixture_quotes_only_a_requested_unambiguous_section(self):
        source = {"source_id": "source", "source_type": "regulation", "section": "8.4", "excerpt": "Test quote"}
        model = launcher.RetrievalFixture()
        payload = {"question": "Покажи пункт 8.4", "sources": [source]}
        result = model.complete_json("", json.dumps(payload), EvidenceDraft)
        self.assertEqual(result.value.evidence[0].quote, "Test quote")
        self.assertEqual(result.metrics.attempt_count, 0)
        payload["sources"].append(dict(source, source_id="second"))
        result = model.complete_json("", json.dumps(payload), EvidenceDraft)
        self.assertEqual(result.value.result_type, "NO_ANSWER")

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
