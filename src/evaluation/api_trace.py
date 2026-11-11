"""Capture HTTPX chat-completions exchanges, including SDK retries and streaming.

Hermes rebuilds clients and can use worker threads and async compression clients.
Intercept HTTPX's single-dispatch boundary for one scenario at a time instead of
wrapping one SDK client. Only chat-completions requests are recorded. The private
HTTPX boundary/decoder are covered by tests and the bounded httpx dependency.
"""

import asyncio
import codecs
import json
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from .runtime_support import json_safe, utc_now

_CAPTURE_LOCK = threading.Lock()
_SENSITIVE = re.compile(
    r"^(?:authorization|proxy[-_]authorization|cookie|set[-_]cookie|api[-_]?key|"
    r"access[-_]token|password|client[-_]secret|signature|subscription[-_]key)$"
    r"|[-_](?:api[-_]key|auth[-_]token)$",
    re.I,
)


class SecretRedactor:
    """Remove credentials while preserving task contents, including synthetic secrets."""

    def __init__(self, secrets=()):
        self.secrets = {value for value in secrets if value}
        self._lock = threading.RLock()

    def learn_headers(self, headers):
        with self._lock:
            for key, value in headers.items():
                if _SENSITIVE.search(key) and value:
                    self.secrets.add(value)
                    if key.lower().endswith("authorization"):
                        self.secrets.add(value.split(" ", 1)[-1])

    def __call__(self, value):
        value = json_safe(value)
        with self._lock:
            if isinstance(value, dict):
                return {
                    key: "[REDACTED]" if _SENSITIVE.search(key) else self(item)
                    for key, item in value.items()
                }
            if isinstance(value, list):
                return [self(item) for item in value]
            if isinstance(value, str):
                if value.startswith(("http://", "https://")):
                    url = urlsplit(value)
                    for credential in (url.username, url.password):
                        if credential:
                            self.secrets.add(credential)
                    self.secrets.update(
                        v for k, v in parse_qsl(url.query) if v and _SENSITIVE.search(k)
                    )
                    query = [
                        (k, "[REDACTED]" if _SENSITIVE.search(k) else v)
                        for k, v in parse_qsl(url.query, keep_blank_values=True)
                    ]
                    value = urlunsplit(
                        (
                            url.scheme,
                            url.netloc.rsplit("@", 1)[-1],
                            url.path,
                            urlencode(query),
                            url.fragment,
                        )
                    )
                for secret in sorted(self.secrets, key=len, reverse=True):
                    value = value.replace(secret, "[REDACTED]")
                    value = value.replace(json.dumps(secret)[1:-1], "[REDACTED]")
            return value


def _body(text):
    try:
        return json.loads(text)
    except ValueError:
        return text


class _ResponseCapture:
    def __init__(self, recorder, request_id, response, started, context):
        self.recorder = recorder
        self.request_id = request_id
        self.started = started
        self.context = context
        self.done = False
        self.received = 0
        self.buffer = ""
        self.sse = "text/event-stream" in response.headers.get("content-type", "").lower()
        # Use a separate decoder: never consume or alter the client's decoder.
        self.decoder = httpx.Response(200, headers=response.headers)._get_content_decoder()
        self.text_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def record(self, event, **fields):
        self.recorder.record(event, **self.context, request_id=self.request_id, **fields)

    def feed(self, raw, *, decoded=False):
        self.received += len(raw)
        data = raw if decoded else self.decoder.decode(raw)
        self._text(self.text_decoder.decode(data))

    def _text(self, text):
        self.buffer += text
        if self.sse:
            while separator := re.search(r"\r?\n\r?\n", self.buffer):
                frame, self.buffer = (
                    self.buffer[: separator.start()],
                    self.buffer[separator.end() :],
                )
                self.record("api_stream_event", frame=frame, partial=False)

    def finish(self, complete, error=None, *, decoded=False):
        if self.done:
            return
        self.done = True
        try:
            if not decoded:
                self._text(self.text_decoder.decode(self.decoder.flush()))
            self._text(self.text_decoder.decode(b"", final=True))
        except Exception as exc:
            complete = False
            error = f"Response decoding failed: {type(exc).__name__}: {exc}"
        if self.sse:
            if self.buffer:
                self.record("api_stream_event", frame=self.buffer, partial=True)
        else:
            self.record("api_response_body", body=_body(self.buffer), complete=complete)
        self.buffer = ""
        self.record(
            "api_response_end",
            complete=complete,
            error=error,
            bytes_received=self.received,
            elapsed_sec=round(time.monotonic() - self.started, 6),
        )


class _SyncStream(httpx.SyncByteStream):
    def __init__(self, inner, capture):
        self.inner, self.capture = inner, capture

    def __iter__(self):
        try:
            for chunk in self.inner:
                self.capture.feed(chunk)
                yield chunk
        except BaseException as exc:
            self.capture.finish(False, f"{type(exc).__name__}: {exc}")
            raise
        else:
            self.capture.finish(True)

    def close(self):
        try:
            self.inner.close()
        finally:
            self.capture.finish(False)


class _AsyncStream(httpx.AsyncByteStream):
    def __init__(self, inner, capture):
        self.inner, self.capture = inner, capture

    async def __aiter__(self):
        try:
            async for chunk in self.inner:
                self.capture.feed(chunk)
                yield chunk
        except BaseException as exc:
            self.capture.finish(False, f"{type(exc).__name__}: {exc}")
            raise
        else:
            self.capture.finish(True)

    async def aclose(self):
        try:
            await self.inner.aclose()
        finally:
            self.capture.finish(False)


class RequestPacer:
    """Shared per-run dispatch spacing, including retries and diagnostic branches."""

    def __init__(self, interval=0):
        self.interval = interval
        self.next_dispatch = 0.0
        self.lock = threading.Lock()

    def reserve(self):
        with self.lock:
            now = time.monotonic()
            delay = max(0, self.next_dispatch - now)
            self.next_dispatch = now + delay + self.interval
            return delay


class APITrace:
    """Append a scenario's API evidence immediately, outside its disposable workspace."""

    def __init__(
        self, path, *, run_id, scenario_id, redactor=None, pacer=None, input_validator=None
    ):
        self.path = Path(path)
        self.redact = redactor or SecretRedactor()
        self.context = {
            "run_id": run_id,
            "scenario_id": scenario_id,
            "turn_number": None,
            "phase": "setup",
        }
        self.requests = 0
        self.errors = []
        self.captures = []
        self._lock = threading.RLock()
        self.pacer = pacer
        self.input_validator = input_validator

    def pacing_delay(self):
        delay = self.pacer.reserve() if self.pacer else 0
        if delay:
            self.record("api_dispatch_wait", **self.context, seconds=delay)
        return delay

    def set_context(self, turn_number, phase):
        with self._lock:
            self.context = {**self.context, "turn_number": turn_number, "phase": phase}

    def record(self, event, **fields):
        with self._lock:
            item = self.redact({"timestamp_utc": utc_now(), "event": event, **fields})
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(item, ensure_ascii=False) + "\n")
            except OSError as exc:
                self.errors.append(self.redact(f"API trace write failed: {exc}"))
                raise

    def raise_if_failed(self):
        if self.errors:
            raise RuntimeError(self.errors[0])

    @staticmethod
    def matches(request):
        return request.method == "POST" and request.url.path.rstrip("/").endswith(
            "/chat/completions"
        )

    def begin(self, request, content):
        self.raise_if_failed()
        if self.input_validator:
            try:
                self.input_validator(_body(content.decode("utf-8")))
            except ValueError as exc:
                self.errors.append(str(exc))
                raise
        self.redact.learn_headers(request.headers)
        with self._lock:
            self.requests += 1
            context = dict(self.context)
            request_id = uuid.uuid4().hex
            self.record(
                "api_request",
                **context,
                request_id=request_id,
                sequence=self.requests,
                method=request.method,
                url=str(request.url),
                headers=dict(request.headers),
                body=_body(content.decode("utf-8")),
            )
        return request_id, time.monotonic(), context

    def response(self, state, response, asynchronous=False):
        request_id, started, context = state
        self.redact.learn_headers(response.headers)
        self.record(
            "api_response_headers",
            **context,
            request_id=request_id,
            status_code=response.status_code,
            headers=dict(response.headers),
            elapsed_sec=round(time.monotonic() - started, 6),
        )
        capture = _ResponseCapture(self, request_id, response, started, context)
        self.captures.append(capture)
        if response.is_stream_consumed:
            capture.feed(response.content, decoded=True)
            capture.finish(True, decoded=True)
        elif asynchronous:
            response.stream = _AsyncStream(response.stream, capture)
        else:
            response.stream = _SyncStream(response.stream, capture)
        return response

    def failed(self, state, exc):
        request_id, started, context = state
        self.record(
            "api_request_error",
            **context,
            request_id=request_id,
            error=f"{type(exc).__name__}: {exc}",
            elapsed_sec=round(time.monotonic() - started, 6),
        )

    def __enter__(self):
        if not _CAPTURE_LOCK.acquire(blocking=False):
            raise RuntimeError(
                "Concurrent API trace scopes are unsupported; run scenarios sequentially"
            )
        try:
            self._sync = httpx.Client._send_single_request
            self._async = httpx.AsyncClient._send_single_request

            def send(client, request):
                if not self.matches(request):
                    return self._sync(client, request)
                time.sleep(self.pacing_delay())
                state = self.begin(request, request.read())
                try:
                    return self.response(state, self._sync(client, request))
                except BaseException as exc:
                    self.failed(state, exc)
                    raise

            async def asend(client, request):
                if not self.matches(request):
                    return await self._async(client, request)
                await asyncio.sleep(self.pacing_delay())
                state = self.begin(request, await request.aread())
                try:
                    return self.response(
                        state, await self._async(client, request), asynchronous=True
                    )
                except BaseException as exc:
                    self.failed(state, exc)
                    raise

            self.record("api_capture_start", **self.context, format_version=1)
            httpx.Client._send_single_request = send
            httpx.AsyncClient._send_single_request = asend
            return self
        except BaseException:
            _CAPTURE_LOCK.release()
            raise

    def __exit__(self, *exc):
        try:
            httpx.Client._send_single_request = self._sync
            httpx.AsyncClient._send_single_request = self._async
            for capture in self.captures:
                capture.finish(False, "Capture scope ended before the response was fully consumed")
            self.record("api_capture_end", **self.context, requests=self.requests)
            self.raise_if_failed()
        finally:
            _CAPTURE_LOCK.release()
