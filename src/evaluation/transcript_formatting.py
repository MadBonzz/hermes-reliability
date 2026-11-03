"""Read-only presentation of observed Hermes controls in transcript exports."""

import html
import json

CHECKS = {
    "whole_file_overwrite": (
        "Replacement of an existing file using the task's recorded file knowledge.",
        "null/empty: no blocker returned. Text: an overwrite blocker reason was returned.",
    ),
    "file_state_staleness": (
        "Whether recorded file knowledge is stale or incomplete.",
        "null/empty: no stale-state message. Text: stale/unread/partial-state message; may be advisory on patch.",
    ),
    "full_write_baseline": (
        "Whether a recorded complete-file baseline matches the current version.",
        "true: matching baseline. false: no matching baseline established; does not by itself prove an incomplete read.",
    ),
    "file_version_snapshot": (
        "File-version metadata used by read tracking.",
        "Snapshot: metadata obtained (raw values in evidence link). null: no usable snapshot. Neither is a safety verdict.",
    ),
    "read_mtime_drift": (
        "Whether the modification timestamp differs from the recorded read.",
        "true: timestamp drift detected. false: no drift detected, or no usable prior timestamp/file measurement.",
    ),
    "file_state_guard_disabled": (
        "Configuration switch for the file-state protection.",
        "true: guard disabled. false: guard not disabled. This does not prove that a later write was checked or blocked.",
    ),
    "display_text_write": (
        "Detection of internal display text, such as read_file line-number prefixes, in proposed content.",
        "true: display-text refusal signal. false: heuristic did not flag the content; not proof of valid file contents.",
    ),
    "sensitive_path_write": (
        "Protection for sensitive system/configuration destinations.",
        "null/empty: no refusal from this check. Text: a sensitive-path refusal reason.",
    ),
    "binary_document_write": (
        "Protection against plain-text writes that would damage a binary document.",
        "null/empty: no refusal from this check. Text: a binary-document refusal reason.",
    ),
    "protected_instruction_write": (
        "Configured protection/approval for instruction-file writes.",
        "null/empty: no refusal (may be inapplicable or disabled). Text: a protection/approval refusal reason.",
    ),
    "configured_write_approval": (
        "Approval requirements for configured sensitive destinations.",
        "null/empty: no refusal (may be inapplicable or approved). Text: an approval-related refusal reason.",
    ),
    "backend_path_routing": (
        "Detection of problematic host/container/sandbox mirror destinations.",
        "null/empty: no routing message. Text: a routing warning/reason. Not a general workspace isolation check.",
    ),
}


def cell(value):
    """Escape literal data for a Markdown table without interpreting it as markup."""
    text = html.escape(str(value), quote=False)
    for char, entity in (
        ("|", "&#124;"),
        ("`", "&#96;"),
        ("*", "&#42;"),
        ("_", "&#95;"),
        ("[", "&#91;"),
        ("]", "&#93;"),
    ):
        text = text.replace(char, entity)
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


def check_glossary():
    rows = [
        "## How to read Hermes checks",
        "These are passive observations of Hermes internals, not extra model calls or evaluator assertions. "
        "Not every file operation invokes every check. A returned refusal/warning is not proof that the attempted "
        "operation was wrong or actually blocked; the subsequent tool result shows what happened. "
        "Auto-approval and non-interactive evaluation settings also affect approval checks.",
        "| Check | What it examines | Meaning of the recorded result |",
        "|---|---|---|",
    ]
    rows.extend(
        f"| `{name}` | {cell(description)} | {cell(meaning)} |"
        for name, (description, meaning) in CHECKS.items()
    )
    table = "\n".join(rows[2:])
    return "\n\n".join(
        [
            rows[0],
            rows[1],
            table,
            "**Common event outcomes:** `no_refusal_returned` means no refusal signal from that invocation; "
            "`refusal_returned` means a refusal/warning signal; `value_observed` means a measurement or Boolean, "
            "not a pass/fail judgment. An exception means the function raised; a missing result means the start "
            "was recorded without a matching completion. Unknown future check types retain their raw result "
            "without an inferred meaning. Snapshot measurements and explanatory messages are not categorical; "
            "their full source records are available through each row's evidence link.",
        ]
    )


def interpret_check(event):
    """Return a readable result and a deliberately bounded interpretation."""
    kind = event.get("event", "native_control_result")
    if kind in {"native_control_exception", "native_control_error"}:
        return event.get(
            "exception_type", event.get("error", "exception")
        ), "Function raised; not an allow/refuse verdict."
    if kind == "native_control_start":
        return "No recorded result", "Invocation started; its outcome is unavailable."
    name = event.get("control", "unknown")
    value = event.get("result")
    raw = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    if name == "file_version_snapshot":
        if value is None:
            return "null", "No usable file-version snapshot obtained."
        return (
            "Snapshot available",
            "Metadata captured; open the evidence link for its exact values.",
        )
    meanings = {
        "full_write_baseline": (
            "Matching complete-file baseline established.",
            "No matching complete-file baseline established.",
        ),
        "read_mtime_drift": (
            "Modification-time drift detected.",
            "No drift detected, or no usable timestamp comparison.",
        ),
        "file_state_guard_disabled": (
            "File-state guard is disabled.",
            "File-state guard is not disabled.",
        ),
        "display_text_write": (
            "Display-text refusal signal returned.",
            "Display-text heuristic did not flag this content.",
        ),
    }
    if name in meanings and isinstance(value, bool):
        return raw, meanings[name][0 if value else 1]
    if name in CHECKS and event.get("result_semantics") == "refusal_reason":
        if value:
            return (
                raw,
                "Refusal/warning reason returned; inspect the tool result for the actual effect.",
            )
        return raw, "No refusal from this check; may be inapplicable, disabled, or allowed."
    return raw, "Recorded value; no additional interpretation inferred."


def check_row(event, evidence):
    result, meaning = interpret_check(event)
    return f"| {cell(event.get('control', 'unknown'))} | {cell(result)} | {cell(meaning)} | {evidence} |"


def check_table(rows):
    return "\n".join(
        [
            "| Check | Recorded result | Interpretation | Evidence |",
            "|---|---|---|---|",
            *rows,
        ]
    )
