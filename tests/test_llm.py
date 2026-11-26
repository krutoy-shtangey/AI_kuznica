import json
import os
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import OpenAI

from backend.llm import AgentDraft, ErrorCode, JsonMode, LLMClient, LLMError, LLMSettings, Tariff
from shared.contracts import AgentResultType


_MISSING = object()


def response(content="готово", usage=_MISSING, finish_reason="stop"):
    data = {
        "id": "demo-completion", "object": "chat.completion", "created": 1, "model": "served-demo",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                     "finish_reason": finish_reason}],
    }
    if usage is not _MISSING:
        data["usage"] = usage
    return httpx.Response(200, json=data)


def failure(status, message="synthetic provider error", code=None):
    return httpx.Response(status, json={"error": {"message": message, "code": code}})


def draft(result_type="ANSWER"):
    return json.dumps({
        "result_type": result_type, "text": "ответ", "source_ids": ["regulation:demo:1"],
        "clarification_question": None,
    })


class ConfigTests(unittest.TestCase):
    @patch.dict(os.environ, {"LLM_MODEL": "env-model"}, clear=True)
    @patch("backend.llm.dotenv_values", return_value={
        "LLM_API_KEY": "synthetic-secret", "LLM_MODEL": "file-model",
        "LLM_INPUT_PRICE_PER_MILLION": "6.53", "LLM_OUTPUT_PRICE_PER_MILLION": "26.32",
        "LLM_CURRENCY": "RUB", "LLM_ALIBABA_INPUT_PRICE_PER_MILLION": "1.468",
        "LLM_ALIBABA_OUTPUT_PRICE_PER_MILLION": "5.871", "LLM_ALIBABA_CURRENCY": "CNY",
        "LLM_ALIBABA_REGION": "Singapore", "LLM_ALIBABA_AS_OF": "2026-11-26",
    })
    def test_loads_file_without_overriding_environment(self, dotenv):
        settings = LLMSettings.from_env(Path("synthetic.env"))
        dotenv.assert_called_once_with(Path("synthetic.env"), interpolate=False)
        self.assertEqual(settings.model, "env-model")
        self.assertEqual(settings.alibaba_tariff.as_of, date(2026, 11, 26))
        self.assertNotIn("synthetic-secret", repr(settings))

    @patch.dict(os.environ, {}, clear=True)
    @patch("backend.llm.dotenv_values", return_value={})
    def test_missing_key(self, _dotenv):
        with self.assertRaises(LLMError) as result:
            LLMSettings.from_env()
        self.assertEqual(result.exception.code, ErrorCode.MISSING_KEY)

    @patch.dict(os.environ, {"LLM_API_KEY": "synthetic", "LLM_TIMEOUT": "nan"}, clear=True)
    @patch("backend.llm.dotenv_values", return_value={})
    def test_invalid_config(self, _dotenv):
        with self.assertRaises(LLMError) as result:
            LLMSettings.from_env()
        self.assertEqual(result.exception.code, ErrorCode.CONFIGURATION)


class ClientTests(unittest.TestCase):
    def client(self, *replies, mode=JsonMode.AUTO, attempts=3):
        self.requests = []
        replies = iter(replies)

        def send(request):
            self.requests.append(json.loads(request.content))
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            return reply

        sdk = OpenAI(
            api_key="synthetic", base_url="https://fake.invalid/v1",
            http_client=httpx.Client(transport=httpx.MockTransport(send)),
        )
        self.addCleanup(sdk.close)
        settings = LLMSettings(
            base_url="https://fake.invalid/v1", api_key="synthetic", model="demo",
            tariff=Tariff(Decimal("6.53"), Decimal("26.32"), "RUB"),
            alibaba_tariff=Tariff(Decimal("1.468"), Decimal("5.871"), "CNY", "Singapore", date(2026, 11, 26)),
            json_mode=mode,
        )
        return LLMClient(settings, sdk=sdk, max_attempts=attempts)

    def assert_error(self, code, calls, invoke):
        with self.assertRaises(LLMError) as result:
            invoke()
        error = result.exception
        self.assertEqual(error.code, code)
        self.assertEqual(error.metrics.attempt_count, calls)
        self.assertEqual(len(self.requests), calls)
        self.assertEqual(str(error), code.value)
        self.assertIsNone(error.__cause__)
        return error

    def test_chat_and_separate_tariffs(self):
        client = self.client(response(usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}))
        result = client.complete_text("инструкция", "вопрос")
        self.assertEqual(result.value, "готово")
        self.assertEqual(self.requests[0]["messages"], [
            {"role": "system", "content": "инструкция"}, {"role": "user", "content": "вопрос"},
        ])
        self.assertNotIn("tools", self.requests[0])
        metric, = result.metrics.attempts
        self.assertEqual((metric.model, metric.input_tokens, metric.output_tokens, metric.total_tokens),
                         ("served-demo", 10, 5, 15))
        self.assertEqual(metric.provider_cost, Decimal("0.00019690"))
        self.assertEqual(metric.alibaba_estimate, Decimal("0.000044035"))
        self.assertGreaterEqual(result.metrics.seconds, metric.seconds)
        self.assertNotIn("готово", repr(result))

    def test_unknown_and_partial_usage(self):
        client = self.client(response(), response(usage={"prompt_tokens": 5, "completion_tokens": 2}))
        unknown, = client.complete_text("s", "u").metrics.attempts
        partial, = client.complete_text("s", "u").metrics.attempts
        self.assertEqual((unknown.input_tokens, unknown.output_tokens, unknown.total_tokens), (None, None, None))
        self.assertIsNone(unknown.provider_cost)
        self.assertIsNone(unknown.alibaba_estimate)
        self.assertIsNone(partial.total_tokens)
        self.assertIsNotNone(partial.provider_cost)

    def test_convert_all_types_and_use_application_ids(self):
        client = self.client(*(response(draft(value)) for value in AgentResultType))
        for value in AgentResultType:
            result = client.complete_json("s", "u", AgentDraft).value.to_result("dialogue", appeal_id="appeal")
            self.assertEqual((result.result_type, result.dialogue_id, result.appeal_id), (value, "dialogue", "appeal"))
        self.assertTrue(self.requests[0]["response_format"]["json_schema"]["strict"])

    def test_explicit_rejections_negotiate_and_remember_format(self):
        client = self.client(
            failure(400, "response_format json_schema not supported"),
            failure(422, "response_format json_object unsupported"),
            response(draft()), response(draft()),
        )
        result = client.complete_json("s", "u", AgentDraft)
        self.assertEqual([metric.mode for metric in result.metrics.attempts],
                         [JsonMode.SCHEMA, JsonMode.OBJECT, JsonMode.PROMPT])
        self.assertIn("Return only a JSON object", self.requests[2]["messages"][0]["content"])
        self.assertNotIn("response_format", self.requests[2])
        self.assertEqual(client.complete_json("s", "u", AgentDraft).metrics.attempt_count, 1)
        self.assertNotIn("response_format", self.requests[3])

    def test_json_object_works_after_schema_rejection(self):
        client = self.client(failure(400, "response_format json_schema not supported"), response(draft()))
        result = client.complete_json("s", "u", AgentDraft)
        self.assertEqual(result.metrics.attempt_count, 2)
        self.assertEqual(self.requests[1]["response_format"], {"type": "json_object"})

    def test_does_not_retry_permanent_errors(self):
        for status, code, expected in [
            (401, None, ErrorCode.AUTHORIZATION), (403, None, ErrorCode.AUTHORIZATION),
            (402, None, ErrorCode.INSUFFICIENT_FUNDS),
            (429, "insufficient_quota", ErrorCode.INSUFFICIENT_FUNDS),
            (400, None, ErrorCode.REQUEST_REJECTED),
        ]:
            with self.subTest(status=status, code=code):
                client = self.client(failure(status, code=code))
                self.assert_error(expected, 1, lambda: client.complete_json("s", "u", AgentDraft))

    @patch("backend.llm.sleep")
    def test_limits_http_transport_retries(self, delay):
        client = self.client(failure(429), failure(503), failure(504))
        error = self.assert_error(ErrorCode.TIMEOUT, 3, lambda: client.complete_text("s", "u"))
        self.assertEqual([metric.error for metric in error.metrics.attempts],
                         [ErrorCode.RATE_LIMIT, ErrorCode.UNAVAILABLE, ErrorCode.TIMEOUT])
        self.assertEqual(delay.call_count, 2)

    @patch("backend.llm.sleep")
    def test_network_retry_and_recovery(self, _delay):
        client = self.client(httpx.ConnectError("synthetic"), httpx.ReadTimeout("synthetic"), response())
        result = client.complete_text("s", "u")
        self.assertEqual([metric.error for metric in result.metrics.attempts],
                         [ErrorCode.UNAVAILABLE, ErrorCode.TIMEOUT, None])

    def test_single_attempt_never_falls_back(self):
        client = self.client(failure(400, "response_format json_schema is unsupported"), attempts=1)
        self.assert_error(ErrorCode.UNSUPPORTED_FORMAT, 1, lambda: client.complete_json("s", "u", AgentDraft))

    def test_invalid_json_and_incomplete_or_empty_output(self):
        for content, finish, expected in [
            ("not json", "stop", ErrorCode.INVALID_JSON), ("{}", "stop", ErrorCode.INVALID_JSON),
            (draft("INVALID"), "stop", ErrorCode.INVALID_JSON),
            (" ", "stop", ErrorCode.EMPTY_RESPONSE), (None, "stop", ErrorCode.EMPTY_RESPONSE),
            ("{}", "length", ErrorCode.INCOMPLETE_RESPONSE),
        ]:
            with self.subTest(content=content, finish=finish):
                client = self.client(response(content, usage={"prompt_tokens": 2, "completion_tokens": 1,
                                                             "total_tokens": 3}, finish_reason=finish))
                error = self.assert_error(expected, 1, lambda: client.complete_json("s", "u", AgentDraft))
                self.assertEqual(error.metrics.attempts[0].total_tokens, 3)
