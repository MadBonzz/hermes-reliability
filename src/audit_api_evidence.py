"""Read-only completeness checks for captured Chat Completions exchanges."""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.runtime_support import read_jsonl
from src.utils import PROJECT_ROOT, load_eval_config, unwrap_tool_call


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


def audit_run(run_directory, secrets=()):
    """Cross-check independent wire, callback, history and checkpoint evidence.

    Differences introduced by Hermes are review observations, not automatically
    logging defects. The checks cannot establish what a provider never sent.
    """
    root = Path(run_directory)
    report_raw = (root / "eval_report.json").read_text(encoding="utf-8")
    if any(secret and secret in report_raw for secret in secrets):
        raise ValueError("Credential value found in saved report")
    report = json.loads(report_raw)
    errors, observations, api_results = [], [], []
    counts = Counter()
    scenarios = report.get("reports", []) + report.get("diagnostic_reports", [])
    for scenario in scenarios:
        sid = scenario["id"]
        path = root / "traces" / sid / "api.jsonl"
        if not path.is_file():
            errors.append(f"{sid}: API trace missing")
            continue
        api_result = audit(path, secrets)
        api_results.append(api_result)
        errors.extend(f"{sid}: {error}" for error in api_result["errors"])
        events = read_jsonl(path)
        requests = [e for e in events if e["event"] == "api_request"]
        if scenario.get("api_requests") != len(requests):
            errors.append(f"{sid}: report/API request count differs")
        if [e["sequence"] for e in requests] != list(range(1, len(requests) + 1)):
            errors.append(f"{sid}: dispatch sequence gap")
        by_id = {e["request_id"]: e for e in requests}
        for event in events:
            if event.get("request_id") not in by_id:
                continue
            request = by_id[event["request_id"]]
            if any(
                event.get(k) != request.get(k)
                for k in ("run_id", "scenario_id", "turn_number", "phase")
            ):
                errors.append(f"{sid}: request/response correlation changed")
            if event["event"] == "api_response_end" and event.get("error"):
                observations.append(f"{sid}: recorded transport error: {event['error']}")
        responses = reconstruct_responses(events)
        for request in requests:
            rid = request["request_id"]
            headers = [
                e
                for e in events
                if e.get("request_id") == rid and e["event"] == "api_response_headers"
            ]
            if not headers or not 200 <= headers[0]["status_code"] < 300:
                continue
            response = responses.get(rid, {})
            choices = response.get("choices", {})
            if not choices or any(c["finish_reason"] is None for c in choices.values()):
                errors.append(f"{sid}/{rid}: successful response missing choice finish reason")
            counts["successful_responses"] += 1
            counts["responses_with_usage"] += bool(response.get("usage"))
            counts["responses_with_reasoning"] += any(c["reasoning"] for c in choices.values())
        for turn in scenario.get("reference_prefix", []) + scenario.get("turns", []):
            number = turn["turn_number"]
            label = f"{sid}/turn_{number}"
            trace_path = path.parent / f"turn_{number}.jsonl"
            if not trace_path.is_file():
                errors.append(f"{label}: turn trace missing")
                continue
            raw = trace_path.read_text(encoding="utf-8")
            if any(secret and secret in raw for secret in secrets):
                raise ValueError("Credential value found in saved turn evidence")
            trace = [json.loads(line) for line in raw.splitlines()]
            counts["turns"] += 1
            for required in ("turn_start", "before_agent", "agent_result", "turn_end"):
                if sum(e["event"] == required for e in trace) != 1:
                    errors.append(f"{label}: missing/duplicate {required}")
            end = next((e["record"] for e in trace if e["event"] == "turn_end"), {})
            for key in (
                "tool_calls",
                "execution_events",
                "final_answer",
                "state_check",
                "process_check",
                "output_check",
                "workspace_after_agent",
                "mock_snapshot",
            ):
                if key not in end or end[key] != turn.get(key):
                    errors.append(f"{label}: checkpoint/trace mismatch for {key}")
            starts = [e for e in trace if e["event"] == "tool_start"]
            counts["tool_executions"] += len(starts)
            native = [e for e in trace if e["event"].startswith("native_control_")]
            if "native_control_events" in turn and turn["native_control_events"] != native:
                errors.append(f"{label}: native control trace/checkpoint mismatch")
            native_ids = {e.get("invocation_id") for e in native}
            counts["native_control_invocations"] += len(native_ids)
            for invocation in native_ids:
                group = [e for e in native if e.get("invocation_id") == invocation]
                if (
                    sum(e["event"] == "native_control_start" for e in group) != 1
                    or sum(
                        e["event"] in {"native_control_result", "native_control_exception"}
                        for e in group
                    )
                    != 1
                ):
                    errors.append(f"{label}: unpaired native control invocation")
                if len({e.get("call_id") for e in group}) != 1 or not all(
                    e.get("call_id") in {s["id"] for s in starts} for e in group
                ):
                    errors.append(f"{label}: native control call correlation missing/changed")
            for start in starts:
                cid = start["id"]
                for required in (
                    "tool_start",
                    "tool_complete",
                    "tool_state_before",
                    "tool_state_after",
                ):
                    if sum(e["event"] == required and e.get("id") == cid for e in trace) != 1:
                        errors.append(f"{label}/{cid}: missing/duplicate {required}")
                completed = next(
                    (e for e in trace if e["event"] == "tool_complete" and e["id"] == cid), {}
                )
                if "result" not in completed:
                    errors.append(f"{label}/{cid}: result missing")
                for snapshot in (
                    e
                    for e in trace
                    if e.get("id") == cid
                    and e["event"] in {"tool_state_before", "tool_state_after"}
                ):
                    if not all(key in snapshot for key in ("workspace", "mock_snapshot")):
                        errors.append(f"{label}/{cid}: state snapshot payload missing")
            if turn.get("origin") == "authored_reference":
                counts["reference_turns"] += 1
                continue
            counts["model_turns"] += 1
            turn_requests = [
                e for e in requests if e.get("turn_number") == number and e.get("phase") == "agent"
            ]
            if len(turn_requests) < turn.get("agent_termination", {}).get("api_calls", 0):
                errors.append(f"{label}: fewer dispatch records than reported API calls")
            wire_calls = {}
            wire_texts = []
            for request in turn_requests:
                for choice in responses.get(request["request_id"], {}).get("choices", {}).values():
                    wire_calls.update({c["id"]: c for c in choice["tool_calls"].values()})
                    if choice["content"]:
                        wire_texts.append(choice["content"])
            counts["wire_tool_calls"] += len(wire_calls)
            for start in starts:
                call = wire_calls.get(start["id"])
                if call is not None:
                    name, arguments = unwrap_tool_call(call["name"], call["arguments"])
                    if name != call["name"]:
                        counts["resolved_bridge_calls"] += 1
                    call = {**call, "name": name, "arguments": arguments}
                if call is None:
                    errors.append(f"{label}/{start['id']}: execution lacks provider tool call")
                elif call["name"] != start["name"]:
                    errors.append(f"{label}/{start['id']}: tool name changed")
                else:
                    try:
                        arguments = (
                            json.loads(call["arguments"])
                            if isinstance(call["arguments"], str)
                            else call["arguments"]
                        )
                    except ValueError:
                        arguments = call["arguments"]
                    if arguments != start["arguments"]:
                        observations.append(
                            f"{label}/{start['id']}: runtime changed provider arguments; both retained"
                        )
            unexecuted = set(wire_calls) - {e["id"] for e in starts}
            if unexecuted:
                observations.append(
                    f"{label}: {len(unexecuted)} provider tool calls without execution; review termination/dispatch"
                )
            if turn.get("final_answer") and turn["final_answer"] not in wire_texts:
                observations.append(
                    f"{label}: final answer differs from wire text; review runtime transformation"
                )
    return {
        "passed": not errors,
        "errors": errors,
        "observations": observations,
        "counts": dict(counts),
        "api_traces": api_results,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run_directory", type=Path)
    args = parser.parse_args()
    config = load_eval_config(PROJECT_ROOT / ".env")
    secrets = [
        config.get(k)
        for k in (
            "COHERE_API_KEY",
            "COHERE_KEY",
            "EVAL_API_KEY",
            "BASETEN_API_KEY",
            "BASETEN_KEY",
            "BAESTEN_KEY",
        )
    ]
    results = audit_run(args.run_directory, secrets)
    print(json.dumps(results, indent=2))
    return int(not results["passed"] or not results["api_traces"])


if __name__ == "__main__":
    sys.exit(main())
