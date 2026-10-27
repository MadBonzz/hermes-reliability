import asyncio
import gzip
import json
from unittest.mock import patch

import httpx

from src.api_trace import APITrace, SecretRedactor
from tests.helpers import TemporaryDirectoryTestCase


class Chunks(httpx.SyncByteStream):
    def __init__(self, parts, fail=False):
        self.parts, self.fail = parts, fail

    def __iter__(self):
        yield from self.parts
        if self.fail:
            raise httpx.ReadError("connection lost")


class AsyncChunks(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"choices": [{"delta": {"content": "hello"}}]}\n\n'
        yield b"data: [DONE]\n\n"


class APITraceTests(TemporaryDirectoryTestCase):
    def recorder(self):
        return APITrace(
            self.root / "api.jsonl",
            run_id="run",
            scenario_id="scenario",
            redactor=SecretRedactor(["private-key"]),
        )

    def events(self):
        return [json.loads(line) for line in (self.root / "api.jsonl").read_text().splitlines()]

    def test_full_request_and_response_are_written_before_return_and_secrets_redacted(self):
        body = {
            "model": "meta-llama/example",
            "messages": [{"role": "user", "content": "x" * 60000}],
            "tools": [{"type": "function", "function": {"name": "test_tool"}}],
        }

        def respond(request):
            logged = self.events()[-1]
            self.assertEqual("api_request", logged["event"])
            self.assertEqual(body, logged["body"])
            return httpx.Response(
                200,
                json={
                    "id": "provider-id",
                    "usage": {"total_tokens": 12},
                    "choices": [{"message": {"tool_calls": [{"function": {"name": "test_tool"}}]}}],
                },
                headers={"x-request-id": "server-request", "set-cookie": "session=private"},
            )

        with (
            self.recorder() as trace,
            httpx.Client(transport=httpx.MockTransport(respond)) as client,
        ):
            trace.set_context(2, "agent")
            response = client.post(
                "https://provider.example/v1/chat/completions",
                headers={"Authorization": "Bearer private-key"},
                json=body,
            )
            self.assertEqual(12, response.json()["usage"]["total_tokens"])
        events = self.events()
        request = next(e for e in events if e["event"] == "api_request")
        response = next(e for e in events if e["event"] == "api_response_body")
        self.assertEqual(request["request_id"], response["request_id"])
        self.assertEqual(
            ("run", "scenario", 2, "agent"),
            tuple(request[k] for k in ("run_id", "scenario_id", "turn_number", "phase")),
        )
        self.assertEqual("[REDACTED]", request["headers"]["authorization"])
        text = (self.root / "api.jsonl").read_text()
        self.assertNotIn("private-key", text)
        self.assertNotIn("session=private", text)
        self.assertIn("server-request", text)

    def test_each_retry_attempt_and_error_body_survives(self):
        attempts = []

        def respond(request):
            attempts.append(request)
            return httpx.Response(
                429 if len(attempts) == 1 else 200,
                json={"error": "limited"} if len(attempts) == 1 else {"ok": True},
            )

        with self.recorder(), httpx.Client(transport=httpx.MockTransport(respond)) as client:
            for attempt in range(2):
                client.post(
                    "https://provider.example/api/v1/chat/completions",
                    headers={"x-stainless-retry-count": str(attempt)},
                    json={"messages": []},
                )
        events = self.events()
        requests = [e for e in events if e["event"] == "api_request"]
        self.assertEqual([1, 2], [e["sequence"] for e in requests])
        self.assertNotEqual(requests[0]["request_id"], requests[1]["request_id"])
        self.assertEqual(
            [429, 200], [e["status_code"] for e in events if e["event"] == "api_response_headers"]
        )
        self.assertEqual(
            {"error": "limited"},
            next(e["body"] for e in events if e["event"] == "api_response_body"),
        )

    def test_stream_events_are_saved_incrementally_across_byte_boundaries(self):
        frame = 'data: {"choices":[{"delta":{"content":"private-key café"}}]}\n\n'.encode()
        usage = b'data: {"usage":{"total_tokens":7},"choices":[]}\n\ndata: [DONE]\n\n'
        parts = [frame[:40], frame[40:-2], frame[-2:], usage]

        def respond(request):
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=Chunks(parts)
            )

        with self.recorder(), httpx.Client(transport=httpx.MockTransport(respond)) as client:
            with client.stream(
                "POST", "https://provider/v1/chat/completions", json={"stream": True}
            ) as response:
                iterator = response.iter_bytes()
                received = b"".join(next(iterator) for _ in range(3))
                self.assertEqual(frame, received)
                self.assertTrue(any(e["event"] == "api_stream_event" for e in self.events()))
                self.assertEqual(usage, b"".join(iterator))
        text = (self.root / "api.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("private-key", text)
        self.assertIn("café", text)
        self.assertIn("total_tokens", text)
        end = next(e for e in self.events() if e["event"] == "api_response_end")
        self.assertTrue(end["complete"])

    def test_compressed_response_is_decoded_without_changing_client_data(self):
        payload = {"choices": [{"message": {"content": "compressed text"}}]}
        compressed = gzip.compress(json.dumps(payload).encode())

        def respond(request):
            return httpx.Response(
                200,
                headers={"content-encoding": "gzip"},
                stream=Chunks([compressed[:10], compressed[10:]]),
            )

        with self.recorder(), httpx.Client(transport=httpx.MockTransport(respond)) as client:
            self.assertEqual(
                payload, client.post("https://provider/v1/chat/completions", json={}).json()
            )
        self.assertEqual(
            payload, next(e["body"] for e in self.events() if e["event"] == "api_response_body")
        )

    def test_transport_failure_is_recorded_and_other_requests_are_not(self):
        original = httpx.Client._send_single_request

        def respond(request):
            raise httpx.ConnectError("cannot connect private-key")

        with self.recorder(), httpx.Client(transport=httpx.MockTransport(respond)) as client:
            with self.assertRaises(httpx.ConnectError):
                client.post("https://provider/v1/chat/completions", json={})
            with self.assertRaises(httpx.ConnectError):
                client.get("https://unrelated.example/private-document")
        self.assertIs(original, httpx.Client._send_single_request)
        self.assertEqual(1, sum(e["event"] == "api_request_error" for e in self.events()))
        self.assertNotIn("private-key", (self.root / "api.jsonl").read_text())
        self.assertNotIn("private-document", (self.root / "api.jsonl").read_text())

    def test_partial_and_broken_streams_are_not_marked_complete(self):
        def respond(request):
            return httpx.Response(200, stream=Chunks([b'{"partial":'], fail=True))

        with self.recorder(), httpx.Client(transport=httpx.MockTransport(respond)) as client:
            with self.assertRaises(httpx.ReadError):
                client.post("https://provider/v1/chat/completions", json={})
            with client.stream("POST", "https://provider/v1/chat/completions", json={}) as response:
                self.assertEqual(b'{"partial":', next(response.iter_bytes()))
        endings = [e for e in self.events() if e["event"] == "api_response_end"]
        self.assertEqual([False, False], [e["complete"] for e in endings])

    def test_async_compression_requests_and_streams_are_captured(self):
        async def run():
            async def respond(request):
                return httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, stream=AsyncChunks()
                )

            with self.recorder() as trace:
                trace.set_context(3, "pre_turn_perturbations")
                async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                    response = await client.post(
                        "https://auxiliary.example/v1/chat/completions", json={}
                    )
                    self.assertIn("hello", response.text)

        asyncio.run(run())
        request = next(e for e in self.events() if e["event"] == "api_request")
        self.assertEqual((3, "pre_turn_perturbations"), (request["turn_number"], request["phase"]))
        self.assertTrue(
            next(e["complete"] for e in self.events() if e["event"] == "api_response_end")
        )

    def test_write_failure_prevents_dispatch_and_is_not_silently_swallowed(self):
        calls = []
        trace = self.recorder()
        with self.assertRaisesRegex(RuntimeError, "API trace write failed"):
            with (
                trace,
                httpx.Client(
                    transport=httpx.MockTransport(lambda req: calls.append(req))
                ) as client,
            ):
                with patch.object(type(trace.path), "open", side_effect=OSError("disk full")):
                    with self.assertRaises(OSError):
                        client.post("https://provider/v1/chat/completions", json={})
        self.assertEqual([], calls)

    def test_redaction_preserves_scenario_secrets_but_removes_connection_credentials(self):
        redact = SecretRedactor(["provider-key"])
        result = redact(
            {
                "base_url": "https://user:pass@provider/v1?api_key=query-key&api-version=1",
                "secret.txt": {"text": "TOP_SECRET_TOKEN_99"},
                "error": "provider-key",
            }
        )
        self.assertEqual({"text": "TOP_SECRET_TOKEN_99"}, result["secret.txt"])
        self.assertNotIn("query-key", result["base_url"])
        self.assertNotIn("user:pass", result["base_url"])
        self.assertEqual("[REDACTED]", result["error"])

    def test_capture_scope_is_restored_on_exception_and_rejects_overlap(self):
        original = httpx.Client._send_single_request
        with self.assertRaisesRegex(ValueError, "abort"):
            with self.recorder():
                with self.assertRaisesRegex(RuntimeError, "Concurrent"):
                    with self.recorder():
                        pass
                raise ValueError("abort")
        self.assertIs(original, httpx.Client._send_single_request)
