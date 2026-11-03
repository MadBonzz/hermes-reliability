"""Compact execution evidence for the campaign analyst, with raw trace links."""

import json
from pathlib import Path

from src.evaluation.audit_api_evidence import reconstruct_responses
from src.evaluation.evaluation_metrics import canonical_arguments
from src.evaluation.runtime_support import read_jsonl
from src.evaluation.transcript_formatting import cell, check_glossary, check_row, check_table


def block(value):
    text = value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)
    if not text:
        return ""
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}\n{text}\n{fence}"


def delta(before, after):
    return {
        key: {"before": before.get(key), "after": after.get(key)}
        for key in sorted(set(before) | set(after))
        if before.get(key) != after.get(key)
    }


def provider_messages(events, number):
    """Retain responses the native runtime may have removed from its history."""
    selected = [e for e in events if e.get("turn_number") == number and e.get("phase") == "agent"]
    responses = reconstruct_responses(selected)
    complete_messages = {}
    for event in selected:
        if event.get("event") == "api_response_body" and isinstance(event.get("body"), dict):
            for choice in event["body"].get("choices", []):
                if choice.get("index", 0) == 0 and choice.get("message"):
                    complete_messages[event["request_id"]] = choice["message"]
    messages = []
    for request in selected:
        if request.get("event") != "api_request":
            continue
        response = responses.get(request["request_id"], {})
        choice = response.get("choices", {}).get(0)
        if choice is None:
            continue
        message = complete_messages.get(
            request["request_id"],
            {
                "role": "assistant",
                "content": choice["content"],
                "reasoning": choice["reasoning"],
                "tool_calls": list(choice["tool_calls"].values()),
            },
        )
        messages.append(message)
    return messages


def render_phase(report_path, *, include_glossary=True, include_tools=True, include_profile=True):
    report_path = Path(report_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    spec = report.get("scenario_contract", {})
    api_events = read_jsonl(report["api_trace_path"]) if report.get("api_trace_path") else []
    parts = [
        f"# {report.get('id', 'Execution')}",
        f"Status `{report['status']}`. Raw report [{report_path.name}]({report_path.as_posix()}).",
    ]
    native = report.get("native_runtime")
    if include_glossary and not native:
        parts.append(check_glossary())
    if native and include_profile:
        parts.append(
            "Native runtime profile:\n\n" + block(native) + "\n\n"
            "Internal native guards are not instrumented in this adapter. Tool events "
            "record execution and state changes; missing guard events do not establish "
            "that protection was absent."
        )
    tools = [
        {k: t[k] for k in ("name", "description", "parameters") if k in t}
        for t in spec.get("mock_tools", [])
    ]
    if tools and include_tools:
        parts += ["## Scenario tools", block(tools)]
    turns = report.get("reference_prefix", []) + report.get("turns", [])
    for turn in turns:
        number = turn["turn_number"]
        parts += [
            f"## Turn {number}",
            block(
                {
                    "origin": turn.get("origin"),
                    "gate_passed": turn.get("gate_passed"),
                    "termination": turn.get("agent_termination"),
                }
            ),
            "### User request",
            block(turn.get("sent_prompt", turn.get("prompt", ""))),
        ]
        if turn.get("perturbations"):
            parts += ["### Perturbations", block(turn["perturbations"])]
        events = read_jsonl(turn["trace_path"]) if turn.get("trace_path") else []
        start = next((e for e in events if e.get("event") == "before_agent"), {})
        if start.get("workspace"):
            parts += ["### Starting files", block(start["workspace"])]
        if start.get("auxiliary_workspace"):
            parts += ["### Other native task directories", block(start["auxiliary_workspace"])]
        if start.get("mock_snapshot", {}).get("services"):
            parts += ["### Starting services", block(start["mock_snapshot"])]
        result = next(
            (e.get("result") or {} for e in reversed(events) if e.get("event") == "agent_result"),
            {},
        )
        messages = result.get("messages", [])
        boundary = result.get("current_turn_user_idx")
        if type(boundary) is int:
            messages = messages[boundary + 1 :]
        else:
            # Most recent matching request avoids repeating history from earlier turns.
            positions = [
                i
                for i, m in enumerate(messages)
                if m.get("role") == "user"
                and m.get("content") == turn.get("sent_prompt", turn.get("prompt"))
            ]
            messages = messages[positions[-1] + 1 :] if positions else []
        model_visible_results = [m for m in messages if m.get("role") == "tool"]
        wire = provider_messages(api_events, number)
        if wire:
            messages = wire
        transport_errors = [
            e
            for e in api_events
            if e.get("turn_number") == number and e.get("event") == "api_request_error"
        ]
        if transport_errors:
            parts.extend(["### Transport errors", block(transport_errors)])
        calls = {e["id"]: e for e in turn.get("execution_events", [])}
        rendered = set()
        displayed_content = set()
        displayed_tool_results = set()

        def render_tool_message(message):
            cid = message.get("tool_call_id")
            content = message.get("content")
            key = (cid, canonical_arguments(content))
            if not content or key in displayed_tool_results:
                return
            if cid in calls and key[1] == canonical_arguments(calls[cid].get("result")):
                return
            parts.extend([f"### Model-visible tool result · {cid}", block(content)])
            displayed_tool_results.add(key)

        def render_call(call):
            cid = call["id"]
            parts.extend([f"### Tool {call['name']} · {cid}", block(call.get("arguments"))])
            controls = [
                e
                for e in turn.get("native_control_events", [])
                if e.get("tool_call_id", e.get("call_id")) == cid
                and e.get("event") != "native_control_start"
            ]
            if controls:
                parts.append(
                    check_table(
                        [
                            check_row(e, f"[trace]({Path(turn['trace_path']).as_posix()})")
                            for e in controls
                        ]
                    )
                )
            parts.append(block(call.get("result")))
            before = next(
                (e for e in events if e.get("event") == "tool_state_before" and e.get("id") == cid),
                {},
            )
            after = next(
                (e for e in events if e.get("event") == "tool_state_after" and e.get("id") == cid),
                {},
            )
            if not before or not after:
                parts.extend(
                    [
                        "State snapshot coverage",
                        block({"before_available": bool(before), "after_available": bool(after)}),
                    ]
                )
            if call.get("completed") is False:
                parts.append("Execution did not complete.")
            changes = {
                key: delta(before.get(key, {}), after.get(key, {}))
                for key in ("workspace", "auxiliary_workspace", "mock_snapshot", "fault_flags")
            }
            changes = {k: v for k, v in changes.items() if v}
            if changes:
                parts.extend(["Observed changes", block(changes)])
            rendered.add(cid)

        for message in messages:
            if message.get("role") == "tool":
                render_tool_message(message)
                continue
            if message.get("role") != "assistant":
                continue
            reasoning = []
            for name in ("reasoning", "reasoning_content"):
                value = message.get(name)
                if value and value not in reasoning and value != message.get("content"):
                    reasoning.append(value)
            for value in reasoning:
                parts.extend(["### Recorded reasoning", block(value)])
            content = message.get("content")
            if content:
                parts.extend(["### Assistant", block(content)])
                if isinstance(content, str):
                    displayed_content.add(content)
            for call in message.get("tool_calls", []):
                cid = call.get("id", call.get("call_id"))
                if cid in calls and cid not in rendered:
                    provider = call.get("function", call)
                    if provider.get("name") and (
                        provider.get("name") != calls[cid]["name"]
                        or canonical_arguments(provider.get("arguments"))
                        != canonical_arguments(calls[cid].get("arguments"))
                    ):
                        parts.extend(["### Provider-emitted call", block(call)])
                    render_call(calls[cid])
                elif cid not in calls:
                    parts.extend(
                        ["### Provider-emitted call with no direct callback ID match", block(call)]
                    )
                if wire:
                    for tool_message in model_visible_results:
                        if tool_message.get("tool_call_id") == cid:
                            render_tool_message(tool_message)
        for cid, call in calls.items():
            if cid not in rendered:
                render_call(call)
        if wire:
            for message in model_visible_results:
                render_tool_message(message)
        if turn.get("tool_call_coverage", {}).get("attempts_without_execution_callback"):
            parts.extend(["### Tool observation coverage", block(turn["tool_call_coverage"])])
        final = turn.get("final_answer")
        if final and final not in displayed_content:
            parts.extend(["### Final response", block(final)])
        contract = next((t for t in spec.get("turns", []) if t["turn_number"] == number), {})
        parts.append("### Verifier results")
        rows = ["| Group | Assertion | Recorded result |", "|---|---|---|"]
        for group in ("state", "process", "output"):
            checks = contract.get(group + "_checks", [])
            results = turn.get(group + "_check", {})
            details = results.get("details", [])
            for index in range(max(len(checks), len(details))):
                assertion = checks[index] if index < len(checks) else "Unmatched result"
                detail = details[index] if index < len(details) else "No result recorded"
                rows.append(
                    f"| {group} | {cell(json.dumps(assertion, ensure_ascii=False))} | {cell(detail)} |"
                )
        parts.append("\n".join(rows))
        if turn.get("artifact_checks"):
            parts.extend(["### Isolated artifact verification", block(turn["artifact_checks"])])
        for name in ("causal_evidence", "harness_errors", "agent_error", "cleanup"):
            if turn.get(name):
                parts.extend([name.replace("_", " ").capitalize(), block(turn[name])])
    phase_errors = {
        key: report[key]
        for key in ("harness_errors", "runtime_errors", "cleanup_error")
        if report.get(key)
    }
    if phase_errors:
        if "status_before_cleanup" in report:
            phase_errors["status_before_cleanup"] = report["status_before_cleanup"]
        parts += ["## Phase-level errors", block(phase_errors)]
    if report.get("efficiency"):
        parts += ["## Cost and trajectory measurements", block(report["efficiency"])]
    return "\n\n".join(p for p in parts if p) + "\n"


def export_execution(directory):
    """Render every ordinary, controlled and reference phase exactly once."""
    directory = Path(directory)
    sections = []
    glossary_added = False
    defined = set()
    profiles = set()
    for path in sorted(directory.rglob("report.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if "scenario_contract" not in data:
            continue
        if not data.get("native_runtime") and not glossary_added:
            sections.insert(0, check_glossary())
            glossary_added = True
        signature = json.dumps(
            [
                {k: t.get(k) for k in ("name", "description", "parameters")}
                for t in data["scenario_contract"].get("mock_tools", [])
            ],
            sort_keys=True,
        )
        profile = json.dumps(data.get("native_runtime"), sort_keys=True)
        sections.append(f"# Execution phase {path.parent.relative_to(directory).as_posix()}")
        sections.append(
            render_phase(
                path,
                include_glossary=False,
                include_tools=signature not in defined,
                include_profile=profile not in profiles,
            )
        )
        defined.add(signature)
        profiles.add(profile)
    if not sections:
        return []
    target = directory / "transcripts.md"
    target.write_text("\n\n".join(sections), encoding="utf-8")
    return [target]
