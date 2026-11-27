"""Windows entry point for the isolated offline modes and the approved ProxyAPI."""
import os
from pathlib import Path
import json
import re

from dotenv import dotenv_values
from starlette.responses import PlainTextResponse

from backend.main import _bounded_env, create_app, require_corporate_endpoint
from backend.llm import CallMetrics, LLMResult, LLMSettings
from shared.contracts import AgentResultType

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "storage" / "windows"
PRIVATE_PORTS = {"retrieval-offline": 8001, "retrieval-proxyapi": 8002}


class RetrievalFixture:
    """Exercise citations and expert publication using only retrieved evidence."""

    def complete_json(self, _system, user, schema):
        payload = json.loads(user)
        match = re.match(r"^покажи пункт\s+(\d+(?:\.\d+)*)(?![\d.])", payload["question"].lower())
        official = [item for item in payload["sources"] if item["source_type"] == "regulation"]
        if match:
            selected = [
                item for item in official
                if (item["section"] or "").rsplit(" / ", 1)[-1] == match[1]
            ]
            selected = selected if len(selected) == 1 else []
        elif not official:
            selected = [item for item in payload["sources"] if item["source_type"] == "expert_answer"][:1]
        else:
            selected = []
        draft = schema.model_validate({
            "result_type": AgentResultType.ANSWER if selected else AgentResultType.NO_ANSWER,
            "field": None, "question": None,
            "evidence": [{"source_id": item["source_id"], "quote": item["excerpt"]} for item in selected],
        })
        return LLMResult(draft, CallMetrics((), 0))


class DirectLocalRequests:
    public_dialogue = re.compile(
        r"/api/dialogues/[0-9a-f-]{36}/(?:messages|appeals(?:/[0-9a-f-]{36})?)"
    )

    def __init__(self, app, port=8001, public_host=None):
        self.app = app
        self.host = f"127.0.0.1:{port}".encode()
        self.public_host = public_host.encode("ascii") if public_host else None

    def allows(self, scope):
        headers = scope["headers"]
        hosts = [value for name, value in headers if name == b"host"]
        entry_page = False
        if hosts == [self.host]:
            if any(name == b"forwarded" or name.startswith((b"x-forwarded-", b"ngrok-"))
                   for name, _ in headers):
                return False
            origin = b"http://" + self.host
        elif self.public_host and hosts == [self.public_host]:
            if [value for name, value in headers if name == b"x-forwarded-proto"] != [b"https"]:
                return False
            path = scope["path"]
            entry_page = path == "/" and scope["method"] in {"GET", "HEAD"}
            if not (path in {"/", "/robots.txt", "/api/health", "/api/knowledge", "/api/dialogues"}
                    or (path.startswith("/assets/") and all(
                        part not in {"", ".", ".."} for part in path[len("/assets/"):].split("/")
                    ) and "\\" not in path)
                    or self.public_dialogue.fullmatch(path)):
                return False
            origin = b"https://" + self.public_host
        else:
            return False
        return all((name != b"origin" or value == origin) and
                   (name != b"sec-fetch-site" or entry_page or value in {b"none", b"same-origin"})
                   for name, value in headers)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not self.allows(scope):
            response = PlainTextResponse("Access denied", status_code=403,
                                         headers={"X-Robots-Tag": "noindex, nofollow"})
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_application():
    mode = os.environ.get("REGLAMENT_MODE", "disabled")
    if mode not in {"disabled", "demo-offline", *PRIVATE_PORTS}:
        raise RuntimeError("Unknown Windows server mode. No synthetic fallback.")
    frontend = ROOT / "frontend" / "dist"
    if not (frontend / "index.html").is_file():
        raise RuntimeError("Build frontend first: npm --prefix frontend run build")
    config = {**dotenv_values(ROOT / ".env", interpolate=False), **os.environ}
    proxyapi = mode == "retrieval-proxyapi"
    public_host = None
    if proxyapi:
        if config.get("REGLAMENT_ALLOW_CORPORATE_PROXYAPI") != "1":
            raise RuntimeError("Set REGLAMENT_ALLOW_CORPORATE_PROXYAPI=1 for the approved corpus.")
        require_corporate_endpoint(LLMSettings.from_env(ROOT / ".env"), True)
        public_flag = config.get("REGLAMENT_ALLOW_PUBLIC", "0")
        if public_flag not in {"0", "1"}:
            raise ValueError("REGLAMENT_ALLOW_PUBLIC must be 0 or 1")
        if public_flag == "1":
            public_host = (config.get("REGLAMENT_PUBLIC_HOST") or "").lower()
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.ngrok(?:-free)?\.(?:app|dev)", public_host):
                raise ValueError("Set REGLAMENT_PUBLIC_HOST to the exact assigned ngrok hostname.")
    model = None
    factory = None
    catalogue = None
    db_name = "maintenance.sqlite3"
    if mode == "demo-offline":
        from backend.demo_model import FixtureModel
        model = FixtureModel()
        db_name = "demo.sqlite3"
    elif mode in PRIVATE_PORTS:
        os.environ["RETRIEVAL_INDEX"] = str(STATE / "index")
        os.environ["RETRIEVAL_MODEL_ROOT"] = str(STATE / "models")
        from retrieval.agent_adapter import retrieval_factory
        from retrieval.metadata import lifecycle, policy

        documents = []

        def factory(publications):
            adapter = retrieval_factory(publications)
            manifest = adapter.saved.manifest
            if not manifest["documents"] or not manifest["count"]:
                raise RuntimeError("The local regulation index is empty.")
            for doc_id, profile in manifest["profiles"].items():
                fields = profile["fields"]
                documents.append({
                    "source_id": "document:" + doc_id, "source_type": "regulation",
                    "document": fields.get("title") or Path(profile["document"]).stem,
                    "section": None, "excerpt": "", "source_url": None,
                    "department": fields.get("department"), "stage": fields.get("stage"),
                    "status": "approved" if lifecycle(policy(profile, None)) == "approved" else "unknown",
                    "approved_at": None, "appeal_id": None, "revision": None,
                })
            return adapter

        def catalogue():
            return documents
        if not proxyapi:
            model = RetrievalFixture()
        # Both local modes share the quality DB and its expert index. The Windows
        # supervisor holds one lock for both modes.
        db_name = "retrieval-test.sqlite3"
    app = create_app(
        demo_mode=mode == "demo-offline", model=model,
        retrieval_factory=factory, catalogue=catalogue, model_is_fixture=mode == "retrieval-offline",
        allow_corporate_proxyapi=proxyapi, load_quality_env=proxyapi,
        db_path=STATE / "storage" / db_name,
        quality_secret=config.get("QUALITY_ADMIN_SECRET"),
        quality_actor=config.get("QUALITY_ADMIN_NAME") or "quality-admin",
        frontend_dir=frontend, attempts=1,
        per_minute=_bounded_env("AGENT_PER_MINUTE", 12, 1, 120, config=config),
        daily_attempts=_bounded_env("AGENT_DAILY_ATTEMPTS", 15 if proxyapi else 80, 1, 150 if proxyapi else 1000,
                                    config=config),
    )
    if mode in PRIVATE_PORTS:
        app.add_middleware(DirectLocalRequests, port=PRIVATE_PORTS[mode], public_host=public_host)
    return app
