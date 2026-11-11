"""Read-only completeness checks for captured Chat Completions exchanges."""

import json
from collections import Counter
from pathlib import Path


def reconstruct_responses(events):
    """Reassemble provider output without relying on Hermes's parsed history."""
    responses = {}
    for event in events:
        if event["event"] not in {"api_stream_event", "api_response_body"}:
            continue
        rid = event["request_id"]
        response = responses.setdefault(rid, {"choices": {}, "usage": None})
        if event["event"] == "api_response_body":
            body = event["body"]
        else:
            # SSE data may span multiple data: lines in one frame.
            data = "\n".join(
                line[5:].lstrip()
                for line in event["frame"].splitlines()
                if line.startswith("data:")
            )
            if not data or data == "[DONE]":
                continue
            body = json.loads(data)
        if not isinstance(body, dict):
            continue
        if body.get("usage"):
            response["usage"] = body["usage"]
        for chunk in body.get("choices", []):
            choice = response["choices"].setdefault(
                chunk.get("index", 0),
                {"content": "", "reasoning": "", "tool_calls": {}, "finish_reason": None},
            )
            message = chunk.get("delta", chunk.get("message", {})) or {}
            choice["content"] += message.get("content") or ""
            choice["reasoning"] += (
                message.get("reasoning_content") or message.get("reasoning") or ""
            )
            if chunk.get("finish_reason") is not None:
                choice["finish_reason"] = chunk["finish_reason"]
            for index, fragment in enumerate(message.get("tool_calls") or []):
                call = choice["tool_calls"].setdefault(
                    fragment.get("index", index), {"id": "", "name": "", "arguments": ""}
                )
                call["id"] += fragment.get("id") or ""
                function = fragment.get("function", {})
                call["name"] += function.get("name") or ""
                call["arguments"] += function.get("arguments") or ""
    return responses


def audit(path, secrets=()):
    raw = Path(path).read_text(encoding="utf-8")
    if any(secret and secret in raw for secret in secrets):
        raise ValueError("Credential value found in saved evidence")
    events = [json.loads(line) for line in raw.splitlines()]
    requests = [e for e in events if e["event"] == "api_request"]
    ids = {e["request_id"] for e in requests}
    errors = []
    if (
        not events
        or events[0]["event"] != "api_capture_start"
        or events[-1]["event"] != "api_capture_end"
    ):
        errors.append("capture scope did not finish")
    if len(ids) != len(requests):
        errors.append("duplicate request IDs")
    for event in events:
        if event.get("request_id") and event["request_id"] not in ids:
            errors.append("unpaired response")
    counters = Counter()
    statuses = Counter()
    for request in requests:
        counters["requests"] += 1
        exchange = [e for e in events if e.get("request_id") == request["request_id"]]
        headers = [e for e in exchange if e["event"] == "api_response_headers"]
        terminal = [e for e in exchange if e["event"] in {"api_response_end", "api_request_error"}]
        if len(terminal) != 1:
            errors.append("missing/duplicate terminal event")
        if not headers and not any(e["event"] == "api_request_error" for e in exchange):
            errors.append("missing response headers")
        for header in headers:
            statuses[str(header["status_code"])] += 1
        frames = [e["frame"] for e in exchange if e["event"] == "api_stream_event"]
        counters["stream_frames"] += len(frames)
        done = any("data: [DONE]" in frame for frame in frames)
        counters["streams_with_done"] += int(done)
        if frames and not done:
            errors.append("stream lacks provider DONE marker")
        bodies = [e["body"] for e in exchange if e["event"] == "api_response_body"]
        if headers and not frames and not bodies:
            errors.append("response payload missing")
        for frame in frames:
            for line in frame.splitlines():
                if line.startswith("data:") and line[5:].strip() != "[DONE]":
                    bodies.append(json.loads(line[5:]))
        for body in bodies:
            if not isinstance(body, dict):
                continue
            if body.get("usage"):
                counters["usage_records"] += 1
            for choice in body.get("choices", []):
                message = choice.get("delta", choice.get("message", {}))
                counters["tool_call_fragments"] += len(message.get("tool_calls") or [])
                counters["reasoning_fragments"] += int(
                    bool(message.get("reasoning_content") or message.get("reasoning"))
                )
        body = request.get("body")
        if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
            errors.append("request messages missing")
    return {
        "path": str(path),
        "passed": not errors,
        "errors": errors,
        "status_codes": dict(statuses),
        **counters,
    }
