"""The exaone provider: one stateless HTTP request per turn against the EXAONE API (docs/design/exaone-provider.md)."""

import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from env_policy import provider_env
from testenv import isolate_data_dir, restore_data_dir

import llm_runner
from llm_runner import BUILTIN_PROVIDER, DEFAULT_PROVIDER_ORDER, KNOWN_PROVIDERS, LLMRunner

KEY = "exaone-key-123"


def setUpModule() -> None:
    # Keep a real ~/.guardian/llm.env on the test host out of these tests.
    os.environ["GUARDIAN_LLM_ENV"] = "/nonexistent/guardian-llm.env"


def _answer(content: str, finish_reason: Optional[str] = "stop",
            reasoning: Optional[str] = "the hidden chain of thought") -> Dict[str, Any]:
    """A chat.completions body; reasoning_content rides along so tests can prove it is dropped."""
    message: Dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return {"id": "chatcmpl-1", "object": "chat.completion",
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}]}


def _stream_answer(*content: str, finish_reason: Optional[str] = "stop") -> List[Any]:
    """A canonical streamed answer (the wire format of 2026-11-10): role, reasoning, content pieces, finish, usage,
    [DONE]. Entries are dicts (json-dumped as data lines) or raw strings the fake writes as-is."""
    chunks: List[Any] = [
        {"id": "chatcmpl-1", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]},
        {"choices": [{"index": 0, "delta": {"reasoning_content": "the hidden chain of thought"}}]},
    ]
    chunks += [{"choices": [{"index": 0, "delta": {"content": piece}}]} for piece in content]
    chunks += [
        {"choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason, "matched_stop": 53}]},
        {"choices": [], "usage": {"completion_tokens": 12, "prompt_tokens": 34, "reasoning_tokens": 8}},
        "[DONE]",
    ]
    return chunks


def _sse_line(item: Any) -> str:
    """One SSE item as its wire string: "[DONE]" becomes data: [DONE], any other string is written as-is,
    dicts become data lines (json-dumped)."""
    if item == "[DONE]":
        return "data: [DONE]\n\n"
    if isinstance(item, str):
        return item
    return f"data: {json.dumps(item)}\n\n"


class _FakeExaone:
    """Local fake of the EXAONE API: records every request, answers from a script (the last entry repeats)."""

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.script: List[Dict[str, Any]] = []
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                with outer.lock:
                    spec = outer.script.pop(0) if len(outer.script) > 1 else outer.script[0]
                    outer.requests.append({
                        "at": time.monotonic(),
                        "command": self.command,
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "content_type": self.headers.get("Content-Type"),
                        "accept": self.headers.get("Accept"),
                        "body": json.loads(body.decode("utf-8")) if body else None,
                    })
                if spec.get("delay"):
                    time.sleep(spec["delay"])
                if spec.get("sse") is not None:
                    self._write_sse(spec)
                    return
                answer = spec.get("body")
                if spec.get("echo_auth"):  # a server that quotes the request header back in its error body
                    answer = {"detail": f"Authorization received: {self.headers.get('Authorization')}"}
                payload = json.dumps(answer or {}).encode("utf-8")
                self.send_response(spec["status"])
                for name, value in (spec.get("headers") or {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _write_sse(self, spec: Dict[str, Any]) -> None:
                """Streams a text/event-stream answer (spec "sse"): no Content-Length, one flushed line at a time,
                then the connection closes. Dicts become data lines, "[DONE]" becomes data: [DONE], any other
                string is written as-is; spec "gap" seconds are slept between lines. spec "chunked" answers
                HTTP/1.1 with Transfer-Encoding: chunked (the real API's framing): every SSE line is one chunk
                (<hex len>\\r\\n<bytes>\\r\\n); spec "cut" then announces a chunk of more bytes than it sends and
                drops the connection, so the client reads a body cut mid-chunk."""
                chunked = bool(spec.get("chunked"))
                if chunked:
                    self.protocol_version = "HTTP/1.1"  # chunked framing needs HTTP/1.1
                self.send_response(spec.get("status", 200))
                for name, value in (spec.get("headers") or {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                if chunked:
                    self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for index, item in enumerate(spec["sse"]):
                    if index and spec.get("gap"):
                        time.sleep(spec["gap"])
                    data = _sse_line(item).encode("utf-8")
                    frame = f"{len(data):x}\r\n".encode("ascii") + data + b"\r\n" if chunked else data
                    try:
                        self.wfile.write(frame)
                        self.wfile.flush()
                    except OSError:
                        break  # the client gave up (a timeout test): stop writing to the dead socket
                else:
                    if chunked:
                        # 0\r\n\r\n terminates the body; "cut" instead announces 100 bytes (0x64), sends 10 and
                        # lets the closing socket cut the body mid-chunk.
                        self._sse_tail(b"64\r\n" + b"x" * 10 if spec.get("cut") else b"0\r\n\r\n")
                self.close_connection = True

            def _sse_tail(self, data: bytes) -> None:
                """Writes the last chunked framing bytes; a dead socket (a timeout test) is fine to ignore."""
                try:
                    self.wfile.write(data)
                    self.wfile.flush()
                except OSError:
                    pass

            def log_message(self, *_args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class ExaoneTestCase(unittest.TestCase):
    """Shared setup: loopback http allowed, pacing and retry jitter off, a fake API on 127.0.0.1."""

    def setUp(self) -> None:
        for patcher in (
            patch.object(llm_runner, "_EXAONE_ALLOW_LOOPBACK_HTTP", True),
            patch.object(llm_runner, "EXAONE_MIN_INTERVAL", 0.0),
            patch("llm_runner.random.uniform", return_value=0.0),
            # The fake is loopback: never let a proxy configured on the test host (env or macOS system) take it.
            patch("urllib.request.proxy_bypass", return_value=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        llm_runner._EXAONE_NEXT_START = 0.0
        self.fake = _FakeExaone()
        self.addCleanup(self.fake.close)

    def _runner(self, extra_env: Optional[Dict[str, str]] = None) -> LLMRunner:
        env = {"EXAONE_API_KEY": KEY, "EXAONE_BASE_URL": self.fake.base_url, **(extra_env or {})}
        with patch.dict(os.environ, env, clear=False), \
                patch("llm_runner.shutil.which", return_value=None):
            return LLMRunner(providers=["exaone"])

    def _runner_without_stream_variable(self, extra_env: Optional[Dict[str, str]] = None) -> LLMRunner:
        """A runner built with EXAONE_STREAM absent from the environment: patch.dict(clear=False) keeps the
        developer's real os.environ, so the variable must be removed inside the patch for the default (streaming
        on) to be what the test sees, whatever the shell exports."""
        with patch.dict(os.environ, {"EXAONE_API_KEY": KEY, "EXAONE_BASE_URL": self.fake.base_url,
                                     **(extra_env or {})}, clear=False):
            os.environ.pop("EXAONE_STREAM", None)
            with patch("llm_runner.shutil.which", return_value=None):
                return LLMRunner(providers=["exaone"])

    def _wait_for_requests(self, count: int, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while len(self.fake.requests) < count and time.monotonic() < deadline:
            time.sleep(0.01)


class JsonAnswerTestCase(ExaoneTestCase):
    """The non-streamed JSON path: EXAONE_STREAM=0 (the diagnosis switch, kept working byte for byte) makes every
    request ask for one JSON answer. The classes that read a JSON answer inherit this; ExaoneTestCase itself sets
    nothing, so the streaming default stays tested."""

    def _runner(self, extra_env: Optional[Dict[str, str]] = None) -> LLMRunner:
        return super()._runner({"EXAONE_STREAM": "0", **(extra_env or {})})


class RequestShapeTests(JsonAnswerTestCase):
    def test_sends_one_user_message_with_the_documented_body(self) -> None:
        self.fake.script = [{"status": 200, "body": _answer("PONG")}]
        self._runner().run_session_prompt("hello exaone", providers=["exaone"])

        self.assertEqual(len(self.fake.requests), 1)
        request = self.fake.requests[0]
        self.assertEqual((request["command"], request["path"]), ("POST", "/chat/completions"))
        self.assertEqual(request["authorization"], f"Bearer {KEY}")
        self.assertEqual(request["content_type"], "application/json")
        self.assertEqual(request["accept"], "application/json")
        body = request["body"]
        self.assertEqual(body["model"], "k-exaone_v2.5")
        self.assertEqual(len(body["messages"]), 1)
        self.assertEqual(body["messages"][0]["role"], "user")
        self.assertTrue(body["messages"][0]["content"].endswith("Task: hello exaone"))
        self.assertEqual(body["temperature"], 1.0)
        self.assertEqual(body["top_p"], 0.95)
        self.assertEqual(body["reasoning_effort"], "high")
        self.assertEqual(body["max_tokens"], 65536)
        self.assertIs(body["stream"], False)
        self.assertNotIn("tools", body)
        self.assertNotIn("chat_template_kwargs", body)

    def test_reasoning_effort_setting_is_honoured_and_unknown_falls_back(self) -> None:
        self.fake.script = [{"status": 200, "body": _answer("PONG")}, {"status": 200, "body": _answer("PONG")}]
        self._runner({"EXAONE_REASONING_EFFORT": "low"}).run_session_prompt("hi", providers=["exaone"])
        self._runner({"EXAONE_REASONING_EFFORT": "turbo"}).run_session_prompt("hi", providers=["exaone"])
        self.assertEqual([r["body"]["reasoning_effort"] for r in self.fake.requests], ["low", "high"])


class AnswerTests(JsonAnswerTestCase):
    def test_answer_is_stripped_and_reasoning_content_is_dropped(self) -> None:
        self.fake.script = [{"status": 200, "body": _answer("\n\nPONG")}]
        result = self._runner().run_session_prompt("hello", providers=["exaone"])
        self.assertEqual((result.provider, result.text, result.session_id), ("exaone-api", "PONG", None))
        self.assertNotIn("hidden chain", result.text)

    def test_run_prompt_is_labelled_exaone_api_too(self) -> None:
        self.fake.script = [{"status": 200, "body": _answer("PONG")}]
        name, text = self._runner().run_prompt("ping")
        self.assertEqual((name, text), ("exaone-api", "PONG"))

    def test_empty_content_fails(self) -> None:
        self.fake.script = [{"status": 200, "body": _answer("")}]
        with self.assertLogs("llm_runner", level="WARNING"):
            result = self._runner().run_session_prompt("hello", providers=["exaone"])
        self.assertEqual(result.provider, BUILTIN_PROVIDER)
        self.assertEqual(len(self.fake.requests), 1)
        self.assertIn("empty answer", result.failures[0])
        self.assertTrue(result.failures[0].startswith("exaone:"))

    def test_finish_reason_length_names_max_tokens(self) -> None:
        self.fake.script = [{"status": 200, "body": _answer("", finish_reason="length")}]
        with self.assertLogs("llm_runner", level="WARNING"):
            result = self._runner().run_session_prompt("hello", providers=["exaone"])
        self.assertEqual(result.provider, BUILTIN_PROVIDER)
        self.assertIn("max_tokens", result.failures[0])
        self.assertIn("finish_reason length", result.failures[0])


class StreamTests(ExaoneTestCase):
    """Streaming, on by default (EXAONE_STREAM=0 is the off switch): the request carries stream: true and
    Accept: text/event-stream, the SSE body is read line by line (docs/design/exaone-provider.md "Streaming")."""

    def _stream_failure(self, extra_env: Optional[Dict[str, str]] = None) -> RuntimeError:
        runner = self._runner({"EXAONE_STREAM": "1", **(extra_env or {})})
        with self.assertRaises(RuntimeError) as ctx:
            runner._invoke_exaone("hello")
        # Every failure below happens after the body started: it must fail the turn without another attempt.
        self.assertEqual(len(self.fake.requests), 1)
        return ctx.exception

    def test_stream_flag_parsing(self) -> None:
        # Streaming is on by default: only 0/false/no/off (case-insensitive, stripped) turns it off.
        for value, expected in (("0", False), ("false", False), ("NO", False), (" Off ", False),
                                ("1", True), ("true", True), ("maybe", True), ("", True)):
            _key, _base, params = self._runner({"EXAONE_STREAM": value})._exaone_config()
            self.assertIs(params["stream"], expected, value)
        _key, _base, params = self._runner_without_stream_variable()._exaone_config()
        self.assertIs(params["stream"], True)  # on by default: no EXAONE_STREAM at all streams

    def test_no_exaone_stream_variable_streams_by_default(self) -> None:
        # The default with no EXAONE_STREAM at all (the developer's shell variable removed inside the patch):
        # the request carries stream: true and asks for text/event-stream, exactly as with EXAONE_STREAM=1.
        self.fake.script = [{"status": 200, "sse": _stream_answer("PONG")}]
        result = self._runner_without_stream_variable().run_session_prompt("hello exaone", providers=["exaone"])

        self.assertEqual((result.provider, result.text), ("exaone-api", "PONG"))
        self.assertEqual(len(self.fake.requests), 1)
        request = self.fake.requests[0]
        self.assertEqual(request["accept"], "text/event-stream")
        self.assertEqual(request["content_type"], "application/json")
        self.assertIs(request["body"]["stream"], True)

    def test_idle_timeout_setting_falls_back_to_the_default(self) -> None:
        for value in ("abc", "0", "-1"):
            _key, _base, params = self._runner({"EXAONE_IDLE_TIMEOUT": value})._exaone_config()
            self.assertEqual(params["idle_timeout"], llm_runner.EXAONE_DEFAULT_IDLE_TIMEOUT, value)
        _key, _base, params = self._runner({"EXAONE_IDLE_TIMEOUT": "1.5"})._exaone_config()
        self.assertEqual(params["idle_timeout"], 1.5)

    def test_stream_flag_turns_the_request_into_sse(self) -> None:
        self.fake.script = [{"status": 200, "sse": _stream_answer("PONG")}]
        self._runner({"EXAONE_STREAM": "1"}).run_session_prompt("hello exaone", providers=["exaone"])

        self.assertEqual(len(self.fake.requests), 1)
        request = self.fake.requests[0]
        self.assertEqual(request["accept"], "text/event-stream")
        self.assertEqual(request["content_type"], "application/json")
        body = request["body"]
        self.assertIs(body["stream"], True)
        self.assertEqual(body["model"], "k-exaone_v2.5")  # the rest of the body is unchanged
        self.assertEqual((body["temperature"], body["top_p"]), (1.0, 0.95))
        self.assertEqual((body["reasoning_effort"], body["max_tokens"]), ("high", 65536))
        self.assertNotIn("stream_options", body)

    def test_the_answer_is_the_joined_content_and_reasoning_stays_hidden(self) -> None:
        # Non-data lines (a comment, event:, id:, retry:) ride along so tests prove they are ignored.
        sse = [": keep-alive\n\n", "event: chunk\n", "id: 7\n", "retry: 100\n\n"] + _stream_answer("\n\nP", "ON", "G\n\n")
        self.fake.script = [{"status": 200, "sse": sse}]
        result = self._runner({"EXAONE_STREAM": "1"}).run_session_prompt("hello", providers=["exaone"])
        self.assertEqual((result.provider, result.text, result.session_id), ("exaone-api", "PONG", None))
        self.assertNotIn("hidden chain", result.text)

    def test_a_chunked_stream_answer_is_joined_as_today(self) -> None:
        # Transfer-Encoding: chunked is the real API's framing: http.client decodes it through a different
        # readline path than the identity-framed answers above.
        self.fake.script = [{"status": 200, "sse": _stream_answer("\n\nP", "ON", "G\n\n"), "chunked": True}]
        result = self._runner({"EXAONE_STREAM": "1"}).run_session_prompt("hello", providers=["exaone"])
        self.assertEqual((result.provider, result.text, result.session_id), ("exaone-api", "PONG", None))
        self.assertNotIn("hidden chain", result.text)

    def test_a_chunked_stream_idle_stall_fails_after_the_idle_timeout(self) -> None:
        sse = [{"choices": [{"index": 0, "delta": {"content": "P"}}]}, "[DONE]"]
        self.fake.script = [{"status": 200, "sse": sse, "chunked": True, "gap": 1.0}]
        error = self._stream_failure({"EXAONE_IDLE_TIMEOUT": "0.3"})
        self.assertIn("no data for", str(error))
        self.assertIn("0.3 s (EXAONE_IDLE_TIMEOUT)", str(error))

    def test_a_chunked_stream_cut_mid_chunk_fails_without_a_resend(self) -> None:
        sse = [{"choices": [{"index": 0, "delta": {"content": "PON"}}]}]
        self.fake.script = [{"status": 200, "sse": sse, "chunked": True, "cut": True}]
        error = self._stream_failure()  # the lying chunk header ends as an IncompleteRead while reading
        self.assertIn("request failed", str(error))
        self.assertIn("IncompleteRead", str(error))

    def test_finish_reason_length_names_max_tokens(self) -> None:
        self.fake.script = [{"status": 200, "sse": _stream_answer("PONG", finish_reason="length")}]
        error = self._stream_failure()
        self.assertIn("max_tokens", str(error))
        self.assertIn("finish_reason length", str(error))

    def test_an_error_chunk_fails_with_the_key_redacted(self) -> None:
        sse = [
            {"choices": [{"index": 0, "delta": {"content": "PAR"}}]},
            {"error": {"message": f"Authorization received: Bearer {KEY}", "code": 500}},
            "[DONE]",
        ]
        self.fake.script = [{"status": 200, "sse": sse}]
        error = self._stream_failure()
        self.assertIn("<redacted>", str(error))
        self.assertNotIn(KEY, str(error))

    def test_a_key_straddling_the_cut_is_redacted_not_truncated(self) -> None:
        # Both error texts are 190 filler characters followed by the key, so the 200-char cut lands 10
        # characters into the key: the old truncate-first order left "exaone-key" behind; redacting first cannot.
        def assert_no_key_prefix(text: str) -> None:
            self.assertNotIn(KEY, text)
            for size in range(8, len(KEY) + 1):
                self.assertNotIn(KEY[:size], text, KEY[:size])

        payload = json.dumps({"error": {"message": "f" * 190 + KEY}})
        with self.assertRaises(RuntimeError) as error:
            llm_runner._exaone_stream_chunk(payload, [], KEY)
        self.assertIn("stream error:", str(error.exception))
        assert_no_key_prefix(str(error.exception))

        payload = "f" * 190 + KEY
        with self.assertRaises(RuntimeError) as error:
            llm_runner._exaone_stream_chunk(payload, [], KEY)
        self.assertIn("malformed stream chunk:", str(error.exception))
        assert_no_key_prefix(str(error.exception))

    def test_a_body_that_ends_without_done_fails(self) -> None:
        self.fake.script = [{"status": 200, "sse": _stream_answer("PONG")[:-1]}]
        self.assertIn("stream ended early", str(self._stream_failure()))

    def test_done_without_a_finish_reason_fails(self) -> None:
        sse = [{"choices": [{"index": 0, "delta": {"content": "PONG"}}]}, "[DONE]"]
        self.fake.script = [{"status": 200, "sse": sse}]
        self.assertIn("stream ended early", str(self._stream_failure()))

    def test_an_empty_finish_reason_does_not_count_as_a_finish(self) -> None:
        sse = [{"choices": [{"index": 0, "delta": {"content": "PONG"}, "finish_reason": ""}]}, "[DONE]"]
        self.fake.script = [{"status": 200, "sse": sse}]
        self.assertIn("stream ended early", str(self._stream_failure()))

    def test_a_non_json_data_line_fails_as_malformed(self) -> None:
        self.fake.script = [{"status": 200, "sse": ["data: {not json}\n\n", "[DONE]"]}]
        error = self._stream_failure()
        self.assertIn("malformed stream chunk", str(error))
        self.assertNotIn(KEY, str(error))

    def test_an_idle_stream_fails_after_the_idle_timeout(self) -> None:
        sse = [{"choices": [{"index": 0, "delta": {"content": "P"}}]}, "[DONE]"]
        self.fake.script = [{"status": 200, "sse": sse, "gap": 1.0}]
        error = self._stream_failure({"EXAONE_IDLE_TIMEOUT": "0.3"})
        self.assertIn("no data for", str(error))
        self.assertIn("0.3 s (EXAONE_IDLE_TIMEOUT)", str(error))

    def test_a_stream_past_the_total_deadline_fails(self) -> None:
        filler = [{"choices": [{"index": 0, "delta": {"content": "x"}}]} for _ in range(10)]
        self.fake.script = [{"status": 200, "sse": filler + ["[DONE]"], "gap": 0.15}]
        error = self._stream_failure({"EXAONE_TIMEOUT": "0.6", "EXAONE_IDLE_TIMEOUT": "5"})
        self.assertIn("total deadline", str(error))
        self.assertIn("0.6 s (EXAONE_TIMEOUT)", str(error))

    def test_a_stream_past_the_runner_timeout_names_the_turn_timeout(self) -> None:
        filler = [{"choices": [{"index": 0, "delta": {"content": "x"}}]} for _ in range(10)]
        self.fake.script = [{"status": 200, "sse": filler + ["[DONE]"], "gap": 0.15}]
        error = self._stream_failure({"GUARDIAN_LLM_TIMEOUT": "1"})  # no EXAONE_TIMEOUT: the runner's own limit
        self.assertIn("total deadline", str(error))
        self.assertIn("(turn timeout)", str(error))

    def test_a_retry_that_would_pass_the_total_deadline_fails_without_a_resend(self) -> None:
        self.fake.script = [
            {"status": 429, "headers": {"retry-after-ms": "400"}, "body": {"detail": "RateLimitError,second limit(1)"}},
        ]
        error = self._stream_failure({"EXAONE_TIMEOUT": "0.2"})
        self.assertIn("total deadline", str(error))
        self.assertIn("0.2 s (EXAONE_TIMEOUT)", str(error))

    def test_the_deadline_text_names_the_source_of_the_limit(self) -> None:
        _key, _base, params = self._runner({"EXAONE_TIMEOUT": "9"})._exaone_config()
        self.assertEqual(params["timeout_source"], "EXAONE_TIMEOUT")
        _key, _base, params = self._runner()._exaone_config()
        self.assertEqual(params["timeout_source"], "turn timeout")

    def test_a_429_before_the_body_is_retried_and_the_stream_answers(self) -> None:
        self.fake.script = [
            {"status": 429, "headers": {"retry-after-ms": "50"}, "body": {"detail": "RateLimitError,second limit(1)"}},
            {"status": 200, "sse": _stream_answer("PONG")},
        ]
        result = self._runner({"EXAONE_STREAM": "1"}).run_session_prompt("hello", providers=["exaone"])
        self.assertEqual((result.provider, result.text), ("exaone-api", "PONG"))
        self.assertEqual(len(self.fake.requests), 2)


class RetryTests(JsonAnswerTestCase):
    def test_429_with_short_retry_after_ms_is_retried_once(self) -> None:
        self.fake.script = [
            {"status": 429, "headers": {"retry-after-ms": "50"}, "body": {"detail": "RateLimitError,second limit(1)"}},
            {"status": 200, "body": _answer("PONG")},
        ]
        result = self._runner().run_session_prompt("hello", providers=["exaone"])
        self.assertEqual((result.provider, result.text), ("exaone-api", "PONG"))
        self.assertEqual(len(self.fake.requests), 2)


class TurnStatsTests(ExaoneTestCase):
    """Per-turn stats of a successful exaone turn (docs/design/exaone-provider.md "Deadline margin", option 1):
    elapsed time, retries, first byte, finish reason, prompt bytes and the usage token counts, so the deadline
    margin is read from events.jsonl instead of inferred from event gaps. A failing turn carries nothing."""

    def setUp(self) -> None:
        super().setUp()
        isolate_data_dir()
        self.addCleanup(restore_data_dir)

    def test_a_streamed_turn_with_usage_carries_the_full_stats(self) -> None:
        # The final usage chunk arrives with empty choices; reasoning_tokens rides top level or inside
        # completion_tokens_details (the two wire shapes seen on 2026-11-10 and in the OpenAI-compatible form).
        top_level = {"completion_tokens": 12, "prompt_tokens": 34, "reasoning_tokens": 8}
        nested = {"completion_tokens": 12, "prompt_tokens": 34,
                  "completion_tokens_details": {"reasoning_tokens": 8}}
        for usage in (top_level, nested):
            with self.subTest(usage=usage):
                self.fake.script = [{"status": 200,
                                     "sse": _stream_answer("PONG")[:-2] + [{"choices": [], "usage": usage},
                                                                           "[DONE]"]}]
                result = self._runner({"EXAONE_STREAM": "1", "EXAONE_TIMEOUT": "90"}).run_session_prompt(
                    "hello", providers=["exaone"])
                self.assertEqual(result.provider, "exaone-api")
                stats = result.stats
                self.assertEqual({key: stats[key] for key in ("timeout_s", "timeout_source", "effort", "stream",
                                                              "retries", "finish_reason")},
                                 {"timeout_s": 90.0, "timeout_source": "EXAONE_TIMEOUT", "effort": "high",
                                  "stream": True, "retries": 0, "finish_reason": "stop"})
                self.assertEqual({key: stats[key] for key in ("prompt_tokens", "completion_tokens",
                                                              "reasoning_tokens")},
                                 {"prompt_tokens": 34, "completion_tokens": 12, "reasoning_tokens": 8})
                for key in ("elapsed_s", "first_byte_s"):
                    self.assertIsInstance(stats[key], float, key)
                    self.assertGreaterEqual(stats[key], 0.0, key)
                sent = self.fake.requests[0]["body"]["messages"][0]["content"]
                self.assertEqual(stats["prompt_bytes"], len(sent.encode("utf-8")))
                self.assertNotIn(KEY, json.dumps(stats))  # never the key

    def test_a_streamed_turn_without_usage_has_no_token_counts(self) -> None:
        self.fake.script = [{"status": 200, "sse": _stream_answer("PONG")[:-2] + ["[DONE]"]}]
        result = self._runner({"EXAONE_STREAM": "1"}).run_session_prompt("hello", providers=["exaone"])
        self.assertEqual((result.provider, result.text), ("exaone-api", "PONG"))  # the turn still succeeds
        self.assertEqual({key for key in result.stats if key.endswith("_tokens")}, set())
        self.assertEqual(result.stats["retries"], 0)
        self.assertEqual(result.stats["finish_reason"], "stop")
        self.assertIn("first_byte_s", result.stats)

    def test_a_429_retry_then_success_counts_the_retry(self) -> None:
        self.fake.script = [
            {"status": 429, "headers": {"retry-after-ms": "50"}, "body": {"detail": "RateLimitError,second limit(1)"}},
            {"status": 200, "sse": _stream_answer("PONG")},
        ]
        result = self._runner({"EXAONE_STREAM": "1"}).run_session_prompt("hello", providers=["exaone"])
        self.assertEqual(result.provider, "exaone-api")
        self.assertEqual(result.stats["retries"], 1)
        self.assertGreaterEqual(result.stats["elapsed_s"], 0.05)  # the retry pause is part of the turn

    def test_a_json_turn_reads_usage_from_the_payload_and_has_no_first_byte(self) -> None:
        body = _answer("PONG")
        body["usage"] = {"completion_tokens": 12, "prompt_tokens": 34, "reasoning_tokens": 8}
        self.fake.script = [{"status": 200, "body": body}]
        result = self._runner({"EXAONE_STREAM": "0"}).run_session_prompt("hello", providers=["exaone"])
        stats = result.stats
        self.assertIs(stats["stream"], False)
        self.assertEqual({key: stats[key] for key in ("prompt_tokens", "completion_tokens", "reasoning_tokens")},
                         {"prompt_tokens": 34, "completion_tokens": 12, "reasoning_tokens": 8})
        self.assertEqual((stats["retries"], stats["finish_reason"]), (0, "stop"))
        self.assertNotIn("first_byte_s", stats)  # one JSON body: no stream to measure a first byte on

    def test_a_failing_turn_falls_back_with_empty_stats(self) -> None:
        self.fake.script = [{"status": 429, "body": {"detail": "RateLimitError"}}]  # no retry-after: fails at once
        with self.assertLogs("llm_runner", level="WARNING"):
            result = self._runner({"EXAONE_STREAM": "1"}).run_session_prompt("hello", providers=["exaone"])
        self.assertEqual(result.provider, BUILTIN_PROVIDER)
        self.assertEqual(result.stats, {})

    def test_two_threads_on_one_runner_do_not_see_each_others_stats(self) -> None:
        # The runner is shared by the parallel agents of one debate: each thread's result must carry its own
        # turn's numbers, and a thread that ran no turn (this main thread) must see none at all.
        self.fake.script = [{"status": 200, "sse": _stream_answer("PONG"), "delay": 0.1}]
        runner = self._runner({"EXAONE_STREAM": "1"})
        prompts = ("short question", "a much longer question, " * 20)
        results: List[Any] = []
        errors: List[str] = []

        def call(prompt: str) -> None:
            try:
                results.append((prompt, runner.run_session_prompt(prompt, providers=["exaone"])))
            except Exception as exc:  # noqa: BLE001 - reported through the assertion below
                errors.append(str(exc))

        threads = [threading.Thread(target=call, args=(prompt,)) for prompt in prompts]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10.0)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        for prompt, result in results:
            self.assertEqual(result.provider, "exaone-api")
            sent = next(r["body"]["messages"][0]["content"] for r in self.fake.requests
                        if r["body"]["messages"][0]["content"].endswith(prompt))
            # Its own turn's prompt size, not the other thread's (the two prompts differ in length).
            self.assertEqual(result.stats["prompt_bytes"], len(sent.encode("utf-8")))
            self.assertGreaterEqual(result.stats["elapsed_s"], 0.0)
        # The main thread ran no turn, so its thread-local holds no stats at all (a plain instance attribute
        # would still carry the numbers of whichever thread finished last).
        self.assertEqual(getattr(runner._turn, "stats", {}), {})

    def test_429_without_retry_headers_fails_at_once(self) -> None:
        self.fake.script = [{"status": 429, "body": {"detail": "RateLimitError"}}]
        with self.assertLogs("llm_runner", level="WARNING"):
            result = self._runner().run_session_prompt("hello", providers=["exaone"])
        self.assertEqual(result.provider, BUILTIN_PROVIDER)
        self.assertEqual(len(self.fake.requests), 1)
        self.assertTrue(result.failures[0].startswith("exaone:"))
        self.assertIn("HTTP 429", result.failures[0])

    def test_429_with_a_long_retry_after_fails_at_once(self) -> None:
        self.fake.script = [{"status": 429, "headers": {"retry-after": "120"}, "body": {}}]
        with self.assertLogs("llm_runner", level="WARNING"):
            result = self._runner().run_session_prompt("hello", providers=["exaone"])
        self.assertEqual(result.provider, BUILTIN_PROVIDER)
        self.assertEqual(len(self.fake.requests), 1)
        self.assertIn("HTTP 429", result.failures[0])

    def test_429_every_time_gives_up_after_five_retries(self) -> None:
        self.fake.script = [{"status": 429, "headers": {"retry-after-ms": "10"}, "body": {}}]
        with self.assertLogs("llm_runner", level="WARNING"):
            result = self._runner().run_session_prompt("hello", providers=["exaone"])
        self.assertEqual(result.provider, BUILTIN_PROVIDER)
        self.assertEqual(len(self.fake.requests), 6)  # the first attempt plus five retries
        self.assertIn("HTTP 429", result.failures[0])


class HttpErrorTests(ExaoneTestCase):
    def _fails_at_once(self, status: int, request_id: str) -> LLMRunner:
        self.fake.script = [{"status": status, "headers": {"x-request-id": request_id}, "echo_auth": True}]
        runner = self._runner()
        with self.assertLogs("llm_runner", level="WARNING"):
            result = runner.run_session_prompt("hello", providers=["exaone"])
        self.assertEqual(result.provider, BUILTIN_PROVIDER)
        self.assertEqual(len(self.fake.requests), 1)
        failure = result.failures[0]
        self.assertTrue(failure.startswith("exaone:"))
        self.assertIn(f"HTTP {status}", failure)
        self.assertIn(request_id, failure)
        # The fake echoes the Authorization header back; the key must not survive into any message.
        self.assertIn("<redacted>", failure)
        self.assertNotIn(KEY, " ".join(result.failures))
        self.assertNotIn(KEY, runner.last_error or "")
        return runner

    def test_400_fails_at_once_without_the_key(self) -> None:
        self._fails_at_once(400, "req-400")

    def test_401_fails_at_once_without_the_key(self) -> None:
        self._fails_at_once(401, "req-401")

    def test_a_key_straddling_the_cut_is_redacted_not_truncated(self) -> None:
        # 190 filler bytes put the key 10 characters before the 200-char cut of the body text: the old
        # truncate-first order left "exaone-key" behind; redacting first cannot.
        failure = llm_runner._exaone_error_message(400, None, ("f" * 190 + KEY).encode("utf-8"), KEY)
        self.assertIn("HTTP 400", failure)
        self.assertNotIn(KEY, failure)
        for size in range(8, len(KEY) + 1):
            self.assertNotIn(KEY[:size], failure, KEY[:size])

    def test_a_request_id_echoing_the_key_is_redacted(self) -> None:
        failure = llm_runner._exaone_error_message(500, {"x-request-id": "id-" + KEY}, b"oops", KEY)
        self.assertNotIn(KEY, failure)
        self.assertIn("x-request-id id-<redacted>", failure)


class GuardTests(ExaoneTestCase):
    def test_an_oversized_prompt_never_reaches_the_api(self) -> None:
        self.fake.script = [{"status": 200, "body": _answer("PONG")}]
        runner = self._runner({"EXAONE_MAX_PROMPT_BYTES": "100", "EXAONE_STREAM": "0"})
        with self.assertLogs("llm_runner", level="WARNING"):
            result = runner.run_session_prompt("hello exaone, this is a long question", providers=["exaone"])
        self.assertEqual(self.fake.requests, [])
        self.assertEqual(result.provider, BUILTIN_PROVIDER)
        self.assertIn("EXAONE_MAX_PROMPT_BYTES", result.failures[0])
        self.assertIn("bytes", result.failures[0])

    def test_a_plain_http_base_url_is_refused_even_for_tests(self) -> None:
        runner = self._runner({"EXAONE_BASE_URL": "http://example.com/v1"})
        with self.assertRaises(RuntimeError) as ctx:
            runner._invoke_exaone("hi")
        self.assertIn("EXAONE_BASE_URL", str(ctx.exception))
        self.assertEqual(self.fake.requests, [])

    def test_a_missing_key_fails_the_call(self) -> None:
        with patch.dict(os.environ, {"EXAONE_BASE_URL": self.fake.base_url}, clear=False):
            os.environ.pop("EXAONE_API_KEY", None)
            with patch("llm_runner.shutil.which", return_value=None):
                runner = LLMRunner(providers=["exaone"])
        with self.assertRaises(RuntimeError) as ctx:
            runner._invoke_exaone("hi")
        self.assertIn("EXAONE_API_KEY is not set", str(ctx.exception))


class ReadinessTests(ExaoneTestCase):
    def test_ready_only_with_the_key_and_never_through_a_binary_lookup(self) -> None:
        with patch.dict(os.environ, {"EXAONE_BASE_URL": self.fake.base_url}, clear=False):
            os.environ.pop("EXAONE_API_KEY", None)
            with patch("llm_runner.shutil.which", return_value=None) as which:
                without_key = LLMRunner(providers=["exaone"])
        self.assertNotIn("exaone", without_key.installed_providers)
        self.assertEqual(without_key.available_providers, [])

        runner = self._runner()  # shutil.which is patched to None here too: no binary exists on the "host"
        self.assertEqual(runner.installed_providers, ["exaone"])
        self.assertNotIn("exaone", [call.args[0] for call in which.call_args_list])

    def test_exaone_is_known_but_opencode_took_its_default_place(self) -> None:
        # opencode (K-EXAONE through the opencode CLI) replaced exaone in the default chain; an explicit
        # ["exaone"] pick keeps working because exaone stays a known provider.
        self.assertIn("exaone", KNOWN_PROVIDERS)
        self.assertNotIn("exaone", DEFAULT_PROVIDER_ORDER)
        self.assertEqual(DEFAULT_PROVIDER_ORDER[:5], ["claude", "codex", "agy", "zai", "opencode"])


class EnvIsolationTests(unittest.TestCase):
    SOURCE = {"PATH": "/bin", "HOME": "/tmp", "EXAONE_API_KEY": KEY, "EXAONE_MODEL": "k-exaone_v2.5",
              "ZAI_API_KEY": "zai-key", "ANTHROPIC_API_KEY": "sk-ant", "OPENAI_API_KEY": "sk-o",
              "AGENT_TOKEN": "agent-token"}

    def test_exaone_env_holds_only_exaone_credentials(self) -> None:
        env = provider_env("exaone", self.SOURCE)
        self.assertEqual(env["EXAONE_API_KEY"], KEY)
        self.assertEqual(env["EXAONE_MODEL"], "k-exaone_v2.5")
        for leaked in ("ZAI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AGENT_TOKEN"):
            self.assertNotIn(leaked, env)

    def test_no_exaone_variable_reaches_another_provider(self) -> None:
        env = provider_env("claude", self.SOURCE)
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-ant")
        self.assertFalse([key for key in env if key.startswith("EXAONE_")])


class PacingTests(JsonAnswerTestCase):
    """Real short intervals (0.3 s); the fake records server-side arrival times of request starts."""

    def _patched_interval(self, seconds: float) -> None:
        patcher = patch.object(llm_runner, "EXAONE_MIN_INTERVAL", seconds)
        patcher.start()
        self.addCleanup(patcher.stop)
        llm_runner._EXAONE_NEXT_START = 0.0

    def _call_in_a_thread(self, runner: LLMRunner) -> threading.Thread:
        thread = threading.Thread(target=runner.run_session_prompt, args=("pace",), kwargs={"providers": ["exaone"]})
        thread.start()
        return thread

    def test_two_calls_start_at_least_the_interval_apart(self) -> None:
        self._patched_interval(0.3)
        self.fake.script = [{"status": 200, "body": _answer("PONG")}]
        runner = self._runner()
        first = self._call_in_a_thread(runner)
        self._wait_for_requests(1)
        second = self._call_in_a_thread(runner)
        first.join(5.0)
        second.join(5.0)

        self.assertEqual(len(self.fake.requests), 2)
        gap = self.fake.requests[1]["at"] - self.fake.requests[0]["at"]
        self.assertGreaterEqual(gap, 0.2)  # the interval, minus scheduling slack

    def test_a_slow_request_does_not_hold_the_pace_lock(self) -> None:
        self._patched_interval(0.3)
        self.fake.script = [{"status": 200, "body": _answer("PONG"), "delay": 1.0},
                            {"status": 200, "body": _answer("PONG")}]
        runner = self._runner()
        first = self._call_in_a_thread(runner)
        self._wait_for_requests(1)
        second = self._call_in_a_thread(runner)  # starts while the first request is still running
        first.join(5.0)
        second.join(5.0)

        self.assertEqual(len(self.fake.requests), 2)
        gap = self.fake.requests[1]["at"] - self.fake.requests[0]["at"]
        # The second start must be about one interval away, not delayed by the first request's full second.
        self.assertLessEqual(gap, 0.6)
        self.assertGreaterEqual(gap, 0.2)


if __name__ == "__main__":
    unittest.main()
