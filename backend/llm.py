"""Synchronous client for an OpenAI-compatible Chat Completions endpoint."""

import json
import logging
import math
import os
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from time import perf_counter, sleep
from typing import Generic, TypeVar
from urllib.parse import urlsplit

from dotenv import dotenv_values
from openai import (
    APIConnectionError, APIError, APIStatusError, APITimeoutError, DefaultHttpxClient, OpenAI,
)
from openai.types.chat import ChatCompletion
from pydantic import BaseModel, ConfigDict, ValidationError

from shared.contracts import AgentResult, AgentResultType


class JsonMode(StrEnum):
    AUTO = "auto"
    SCHEMA = "json_schema"
    OBJECT = "json_object"
    PROMPT = "prompt"


class ErrorCode(StrEnum):
    CONFIGURATION = "configuration"
    MISSING_KEY = "missing_api_key"
    AUTHORIZATION = "authorization"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED_FORMAT = "unsupported_format"
    REQUEST_REJECTED = "request_rejected"
    INVALID_JSON = "invalid_json"
    EMPTY_RESPONSE = "empty_response"
    INCOMPLETE_RESPONSE = "incomplete_response"
    API_ERROR = "api_error"


@dataclass(frozen=True)
class Tariff:
    input_per_million: Decimal
    output_per_million: Decimal
    currency: str
    region: str | None = None
    as_of: date | None = None

    def estimate(self, input_tokens: int | None, output_tokens: int | None) -> Decimal | None:
        if input_tokens is None or output_tokens is None:
            return None
        return (input_tokens * self.input_per_million + output_tokens * self.output_per_million) / 1_000_000


@dataclass(frozen=True)
class LLMSettings:
    base_url: str
    api_key: str = field(repr=False)
    model: str
    temperature: float = 0.2
    max_tokens: int = 512
    timeout: float = 30
    tariff: Tariff | None = None
    alibaba_tariff: Tariff | None = None
    json_mode: JsonMode = JsonMode.OBJECT

    @classmethod
    def from_env(cls, env_file: str | Path = Path(__file__).resolve().parents[1] / ".env") -> "LLMSettings":
        values = {**dotenv_values(env_file, interpolate=False), **os.environ}

        def get(name: str, default: str = "") -> str:
            value = values.get(name)
            return default if value is None else value.strip()

        def tariff(prefix: str, comparison: bool = False) -> Tariff | None:
            inputs = get(f"{prefix}_INPUT_PRICE_PER_MILLION")
            outputs = get(f"{prefix}_OUTPUT_PRICE_PER_MILLION")
            currency = get(f"{prefix}_CURRENCY").upper()
            region = get(f"{prefix}_REGION") if comparison else None
            as_of = get(f"{prefix}_AS_OF") if comparison else None
            if not any((inputs, outputs, region, as_of)):
                return None
            if not inputs or not outputs or len(currency) != 3 or not currency.isascii() or not currency.isalpha():
                raise ValueError
            if comparison and (not region or not as_of):
                raise ValueError
            prices = (Decimal(inputs), Decimal(outputs))
            if any(not price.is_finite() or price < 0 for price in prices):
                raise ValueError
            return Tariff(*prices, currency, region, date.fromisoformat(as_of) if as_of else None)

        try:
            settings = cls(
                base_url=get("LLM_BASE_URL", "https://api.proxyapi.ru/v1"),
                api_key=get("LLM_API_KEY"),
                model=get("LLM_MODEL", "qwen/qwen3-30b-a3b-instruct-2507"),
                temperature=float(get("LLM_TEMPERATURE", "0.2")),
                max_tokens=int(get("LLM_MAX_TOKENS", "512")),
                timeout=float(get("LLM_TIMEOUT", "30")),
                tariff=tariff("LLM"),
                alibaba_tariff=tariff("LLM_ALIBABA", comparison=True),
                json_mode=JsonMode(get("LLM_JSON_MODE", "json_object")),
            )
            endpoint = urlsplit(settings.base_url)
            if (
                endpoint.scheme not in {"http", "https"} or not endpoint.hostname
                or endpoint.username is not None or endpoint.password is not None
                or endpoint.query or endpoint.fragment or not settings.model
                or not 0 <= settings.temperature <= 2 or settings.max_tokens < 1
                or not math.isfinite(settings.timeout) or settings.timeout <= 0
            ):
                raise ValueError
        except (ValueError, InvalidOperation):
            raise LLMError(ErrorCode.CONFIGURATION) from None
        if not settings.api_key:
            raise LLMError(ErrorCode.MISSING_KEY)
        return settings


@dataclass(frozen=True)
class AttemptMetrics:
    model: str
    seconds: float
    mode: JsonMode | None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    provider_cost: Decimal | None = None
    provider_currency: str | None = None
    alibaba_estimate: Decimal | None = None
    alibaba_currency: str | None = None
    error: ErrorCode | None = None
    status_code: int | None = None


@dataclass(frozen=True)
class CallMetrics:
    attempts: tuple[AttemptMetrics, ...]
    seconds: float

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)


class LLMError(Exception):
    def __init__(self, code: ErrorCode, metrics: CallMetrics | None = None):
        super().__init__(code.value)
        self.code = code
        self.metrics = metrics


Value = TypeVar("Value")


@dataclass(frozen=True)
class LLMResult(Generic[Value]):
    value: Value = field(repr=False)
    metrics: CallMetrics


class AgentDraft(BaseModel):
    """Only fields the model can propose. Application IDs come from the caller."""

    model_config = ConfigDict(extra="forbid", strict=True)

    result_type: AgentResultType
    text: str | None
    source_ids: list[str]
    clarification_question: str | None

    def to_result(self, dialogue_id: str, *, appeal_id: str | None = None) -> AgentResult:
        return AgentResult(
            result_type=self.result_type,
            dialogue_id=dialogue_id,
            text=self.text,
            source_ids=tuple(self.source_ids),
            clarification_question=self.clarification_question,
            appeal_id=appeal_id,
        )


_RETRYABLE = {ErrorCode.RATE_LIMIT, ErrorCode.TIMEOUT, ErrorCode.UNAVAILABLE}
_FALLBACK = {JsonMode.SCHEMA: JsonMode.OBJECT, JsonMode.OBJECT: JsonMode.PROMPT}
_FUNDS_CODES = {"insufficient_quota", "insufficient_balance", "billing_hard_limit_reached"}


def _classify(error: APIError, mode: JsonMode | None) -> ErrorCode:
    if isinstance(error, APITimeoutError):
        return ErrorCode.TIMEOUT
    if isinstance(error, APIConnectionError):
        return ErrorCode.UNAVAILABLE
    if not isinstance(error, APIStatusError):
        return ErrorCode.API_ERROR

    status = error.status_code
    if status in (401, 403):
        return ErrorCode.AUTHORIZATION
    if status == 402 or (isinstance(error.code, str) and error.code in _FUNDS_CODES):
        return ErrorCode.INSUFFICIENT_FUNDS
    if status == 429:
        return ErrorCode.RATE_LIMIT
    if status in (408, 504):
        return ErrorCode.TIMEOUT
    if 500 <= status <= 599:
        return ErrorCode.UNAVAILABLE
    if status in (400, 422) and mode in _FALLBACK:
        detail = str(error.body).casefold()
        format_name = any(name in detail for name in (
            "response_format", "json_schema", "json_object", "structured output",
        ))
        rejected = any(word in detail for word in (
            "unsupported", "not supported", "not support", "doesn't support", "does not support",
            "unrecognized", "unknown", "not allowed", "not available", "only accepts",
        ))
        if format_name and rejected:
            return ErrorCode.UNSUPPORTED_FORMAT
    return ErrorCode.REQUEST_REJECTED


class LLMClient:
    def __init__(self, settings: LLMSettings, *, max_attempts: int = 3, sdk: OpenAI | None = None):
        if not settings.api_key.strip():
            raise LLMError(ErrorCode.MISSING_KEY)
        if not 1 <= max_attempts <= 3:
            raise ValueError("max_attempts must be from 1 to 3")
        self.settings = settings
        self.max_attempts = max_attempts
        self._detected_mode = JsonMode.SCHEMA
        # The SDK's debug logger can emit request bodies. Keep it silent.
        for logger in ("openai._base_client", "openai._legacy_response", "openai._response"):
            logging.getLogger(logger).disabled = True
        self._sdk = (
            OpenAI(
                api_key=settings.api_key, base_url=settings.base_url, timeout=settings.timeout,
                max_retries=0, http_client=DefaultHttpxClient(follow_redirects=False, trust_env=False),
            )
            if sdk is None else sdk.with_options(max_retries=0, timeout=settings.timeout)
        )

    def close(self) -> None:
        self._sdk.close()

    def complete_text(self, system: str, user: str) -> LLMResult[str]:
        return self._complete(system, user, None)

    def complete_json(self, system: str, user: str, schema: type[Value]) -> LLMResult[Value]:
        if not issubclass(schema, BaseModel) or schema.model_json_schema().get("type") != "object":
            raise ValueError("schema must be an object-shaped Pydantic model")
        return self._complete(system, user, schema)

    def _complete(self, system, user, schema):
        started = perf_counter()
        attempts = []
        automatic = schema is not None and self.settings.json_mode is JsonMode.AUTO
        mode = None if schema is None else self._detected_mode if automatic else self.settings.json_mode
        json_schema = schema.model_json_schema() if schema is not None else None

        for index in range(self.max_attempts):
            params = {}
            instruction = system
            if mode is JsonMode.SCHEMA:
                params["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": schema.__name__, "strict": True, "schema": json_schema},
                }
                instruction += "\nReturn only a JSON object matching the response schema."
            elif mode in (JsonMode.OBJECT, JsonMode.PROMPT):
                if mode is JsonMode.OBJECT:
                    params["response_format"] = {"type": "json_object"}
                instruction += "\nReturn only a JSON object matching this schema: " + json.dumps(json_schema)

            completion = None
            status = None
            attempt_start = perf_counter()
            try:
                completion = self._sdk.chat.completions.create(
                    model=self.settings.model,
                    messages=[{"role": "system", "content": instruction}, {"role": "user", "content": user}],
                    temperature=self.settings.temperature,
                    max_tokens=self.settings.max_tokens,
                    **params,
                )
                value = self._decode(completion, schema)
            except APIError as exc:
                error = _classify(exc, mode)
                status = exc.status_code if isinstance(exc, APIStatusError) else None
            except LLMError as exc:
                error = exc.code
            else:
                error = None

            attempts.append(self._measure(completion, perf_counter() - attempt_start, mode, error, status))
            if error is None:
                return LLMResult(value, CallMetrics(tuple(attempts), perf_counter() - started))
            if automatic and error is ErrorCode.UNSUPPORTED_FORMAT and mode in _FALLBACK:
                mode = _FALLBACK[mode]
                self._detected_mode = mode
            elif error in _RETRYABLE and index + 1 < self.max_attempts:
                sleep(0.25 * (2 ** index))
            else:
                break

        raise LLMError(error, CallMetrics(tuple(attempts), perf_counter() - started))

    @staticmethod
    def _decode(completion: ChatCompletion, schema: type[BaseModel] | None):
        choices = getattr(completion, "choices", None)
        if not choices:
            raise LLMError(ErrorCode.EMPTY_RESPONSE)
        choice = choices[0]
        if choice.finish_reason != "stop" or getattr(choice.message, "refusal", None):
            raise LLMError(ErrorCode.INCOMPLETE_RESPONSE)
        content = choice.message.content
        if not isinstance(content, str) or not content.strip():
            raise LLMError(ErrorCode.EMPTY_RESPONSE)
        if schema is None:
            return content.strip()
        try:
            return schema.model_validate_json(content, strict=True)
        except ValidationError:
            raise LLMError(ErrorCode.INVALID_JSON) from None

    def _measure(self, completion, seconds, mode, error, status):
        usage = getattr(completion, "usage", None)
        input_tokens = getattr(usage, "prompt_tokens", None)
        output_tokens = getattr(usage, "completion_tokens", None)
        total_tokens = getattr(usage, "total_tokens", None)
        tariff = self.settings.tariff
        alibaba = self.settings.alibaba_tariff
        return AttemptMetrics(
            model=getattr(completion, "model", None) or self.settings.model,
            seconds=seconds, mode=mode, input_tokens=input_tokens,
            output_tokens=output_tokens, total_tokens=total_tokens,
            provider_cost=tariff.estimate(input_tokens, output_tokens) if tariff else None,
            provider_currency=tariff.currency if tariff else None,
            alibaba_estimate=alibaba.estimate(input_tokens, output_tokens) if alibaba else None,
            alibaba_currency=alibaba.currency if alibaba else None,
            error=error, status_code=status,
        )
