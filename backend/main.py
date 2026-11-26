"""Minimal FastAPI entry point for an explicitly configured retrieval adapter."""

import json
import os
import sqlite3
from contextlib import asynccontextmanager, closing
from dataclasses import asdict, replace
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, StringConstraints

from backend.agent import Agent, AgentFailure, ContextMode, Model
from backend.demo_retrieval import DemoRetrieval
from backend.llm import LLMClient, LLMSettings
from backend.storage import AccessError, Store, dump, summarize
from shared.contracts import AgentResult, ExpertPublicationLookup, Retrieval

ROOT = Path(__file__).resolve().parents[1]
MAX_BODY = 16_384
Bearer = HTTPBearer(auto_error=False)

_ERRORS = {
    "dialogue_not_found": (404, "Диалог не найден."),
    "dialogue_busy": (409, "В диалоге уже обрабатывается сообщение. Повторите позже."),
    "dialogue_limit": (429, "В диалоге достигнут предел сообщений."),
    "rate_limit": (429, "Достигнут лимит запросов. Повторите позже."),
    "daily_budget": (429, "Достигнут лимит обращений к модели за последние 24 часа."),
    "retrieval_unavailable": (503, "Поиск временно недоступен."),
    "retrieval_not_configured": (503, "Поиск не подключён."),
    "publication_unavailable": (503, "Статус разъяснений сейчас проверить невозможно."),
    "model_unavailable": (503, "Языковая модель временно недоступна."),
    "source_changed": (409, "Источник изменился. Повторите вопрос."),
    "source_inconsistent": (502, "Поиск вернул разные версии одного источника."),
    "evidence_invalid": (502, "Ответ модели не удалось подтвердить по источникам."),
    "model_invalid_response": (502, "Модель вернула непригодный результат."),
    "storage_unavailable": (503, "Хранилище диалогов временно недоступно."),
    "internal_error": (500, "Внутренняя ошибка обработки запроса."),
}


class MessageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    context: ContextMode = "auto"


class DialogueCreated(BaseModel):
    dialogue_id: str
    access_token: str


class MessageReply(BaseModel):
    result: AgentResult
    sources: list[dict]
    metrics: dict
    dialogue_usage: dict


def _error(code, **extra):
    status, message = _ERRORS[code]
    return JSONResponse(status_code=status, content={"code": code, "message": message, **extra})


def _token(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(Bearer)]) -> str:
    if credentials is None or not 20 <= len(credentials.credentials) <= 100:
        raise AccessError("dialogue_not_found")
    return credentials.credentials


class RequestLimits:
    """Bound the complete request body even when Content-Length is absent."""

    def __init__(self, app, mode):
        self.app, self.mode = app, mode

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def private_send(event):
            if event["type"] == "http.response.start":
                event["headers"] = [*event.get("headers", []), (b"x-robots-tag", b"noindex"),
                                    (b"cache-control", b"no-store"), (b"x-agent-mode", self.mode)]
            await send(event)

        if scope["method"] != "POST":
            await self.app(scope, receive, private_send)
            return
        body = bytearray()
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            body.extend(event.get("body", b""))
            if len(body) > MAX_BODY:
                response = JSONResponse({"code": "body_too_large", "message": "Запрос слишком большой."}, status_code=413)
                await response(scope, receive, private_send)
                return
            if not event.get("more_body"):
                break

        delivered = False

        async def replay():
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay, private_send)


def _bounded_env(name: str, default: int, lower: int, upper: int) -> int:
    value = int(os.environ.get(name, default))
    if not lower <= value <= upper:
        raise ValueError(f"{name} must be between {lower} and {upper}")
    return value


def create_app(*, retrieval: Retrieval | None = None, model: Model | None = None,
               publications: ExpertPublicationLookup | None = None, db_path: Path | None = None,
               demo_mode: bool = False, attempts: int = 1, per_minute: int = 12, daily_attempts: int = 80) -> FastAPI:
    if retrieval is not None and demo_mode:
        raise ValueError("Select only one retrieval adapter")
    if not 1 <= attempts <= 3 or min(per_minute, daily_attempts) < 1:
        raise ValueError("Invalid limits")
    if isinstance(model, LLMClient):
        if model.max_attempts > attempts:
            raise ValueError("The injected LLM exceeds the reserved attempt limit")
        if not demo_mode and urlsplit(model.settings.base_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Corporate retrieval requires a local endpoint in this server configuration.")
    if demo_mode:
        retrieval = DemoRetrieval()
    default_db = "demo.sqlite3" if demo_mode else "agent.sqlite3"
    store = Store(db_path or ROOT / "storage" / default_db, per_minute=per_minute, daily_attempts=daily_attempts)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store.initialize()
        active_model = model
        owned_model = None
        if retrieval is not None and active_model is None:
            settings = LLMSettings.from_env()
            if not demo_mode and urlsplit(settings.base_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise RuntimeError("Corporate retrieval requires a local endpoint in this server configuration.")
            owned_model = active_model = LLMClient(
                replace(settings, max_tokens=min(settings.max_tokens, 512), timeout=min(settings.timeout, 40)),
                max_attempts=attempts,
            )
        app.state.agent = Agent(retrieval, active_model, publications) if retrieval and active_model else None
        try:
            yield
        finally:
            if owned_model is not None:
                owned_model.close()

    app = FastAPI(title="ИИКузница", version="0.1.0", lifespan=lifespan)
    mode = "demo" if demo_mode else "configured" if retrieval is not None else "disabled"
    app.add_middleware(RequestLimits, mode=mode.encode("ascii"))

    @app.exception_handler(AccessError)
    async def access_error(_request: Request, error: AccessError):
        return _error(error.code)

    @app.exception_handler(sqlite3.Error)
    async def storage_error(_request: Request, _error_exc: sqlite3.Error):
        return _error("storage_unavailable")

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _error_exc: RequestValidationError):
        return JSONResponse(status_code=422, content={"code": "invalid_request", "message": "Некорректный запрос."})

    @app.get("/health")
    def health():
        with closing(store.connection()) as db:
            db.execute("SELECT 1 FROM dialogues LIMIT 1")
        return {"status": "ok", "retrieval": mode}

    @app.post("/dialogues", status_code=201)
    def create_dialogue() -> DialogueCreated:
        dialogue_id, token = store.create()
        return DialogueCreated(dialogue_id=dialogue_id, access_token=token)

    @app.post("/dialogues/{dialogue_id}/messages", response_model=MessageReply)
    def send_message(dialogue_id: UUID, body: MessageInput, token: Annotated[str, Depends(_token)]):
        agent = app.state.agent
        if agent is None:
            return _error("retrieval_not_configured")
        dialogue_id = str(dialogue_id)
        lease, previous = store.begin(dialogue_id, token, attempts)
        try:
            outcome = agent.reply(dialogue_id, previous, body.message, body.context)
        except AgentFailure as error:
            totals = store.finish(dialogue_id, lease, body.message, error=error)
            measures = asdict(error.metrics) if error.metrics else None
            return _error(error.code, metrics=json.loads(dump({
                "call": measures, "totals": summarize([measures]),
            })), dialogue_usage=totals)
        except Exception:
            # Keep provider responses and messages out of framework tracebacks.
            store.finish(dialogue_id, lease, body.message, error=AgentFailure("internal_error"))
            return _error("internal_error")

        totals = store.finish(dialogue_id, lease, body.message, outcome=outcome)
        measures = asdict(outcome.metrics) if outcome.metrics else None
        return {
            "result": outcome.result, "sources": list(outcome.sources),
            "metrics": {"call": measures, "totals": summarize([measures])}, "dialogue_usage": totals,
        }

    @app.get("/dialogues/{dialogue_id}/messages")
    def history(dialogue_id: UUID, token: Annotated[str, Depends(_token)],
                before: Annotated[int | None, Query(ge=1)] = None,
                limit: Annotated[int, Query(ge=1, le=100)] = 30):
        turns, cursor, totals = store.history(str(dialogue_id), token, before, limit)
        agent = app.state.agent
        try:
            for turn in turns:
                if any(source["source_type"] == "expert_answer" for source in turn["sources"]) and (
                    agent is None or not agent.published_sources(tuple(turn["sources"]))
                ):
                    turn["result"], turn["sources"], turn["error"] = None, [], "source_changed"
        except AgentFailure as error:
            return _error(error.code)
        return {"dialogue_id": str(dialogue_id), "messages": turns, "next_before": cursor, "usage": totals}

    return app


app = create_app(
    demo_mode=os.environ.get("AGENT_DEMO_MODE") == "1",
    per_minute=_bounded_env("AGENT_PER_MINUTE", 12, 1, 120),
    daily_attempts=_bounded_env("AGENT_DAILY_ATTEMPTS", 80, 1, 1000),
)
