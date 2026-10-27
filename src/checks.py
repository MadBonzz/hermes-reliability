"""Deterministic state, process, and output assertions for evaluation turns."""

import json
import re
from pathlib import Path
from typing import Any

from .utils import get_json_path, resolve_ws_path


def _read_text(path: Path) -> str:
    """Missing paths count as empty content for the existing text assertions."""
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _nonblank_lines(path: Path) -> list[str]:
    return [line.strip() for line in _read_text(path).splitlines() if line.strip()]


def evaluate_state_checks(checks: list[dict[str, Any]], workspace: Path) -> tuple[bool, list[str]]:
    """Evaluates declarative state assertions against the workspace filesystem."""
    details = []
    all_ok = True

    for chk in checks:
        c_type = chk["type"]
        target = resolve_ws_path(workspace, chk.get("path", ""))
        expected = chk.get("expected")

        if c_type == "file_exists":
            ok = target.exists()
            details.append(f"[{'PASS' if ok else 'FAIL'}] file_exists({target.name})")

        elif c_type == "file_not_exists":
            ok = not target.exists()
            details.append(f"[{'PASS' if ok else 'FAIL'}] file_not_exists({target.name})")

        elif c_type == "file_contains":
            text = _read_text(target)
            ok = str(expected) in text
            details.append(
                f"[{'PASS' if ok else 'FAIL'}] file_contains({target.name}, {expected!r})"
            )

        elif c_type == "file_not_contains":
            text = _read_text(target)
            ok = str(expected) not in text
            details.append(
                f"[{'PASS' if ok else 'FAIL'}] file_not_contains({target.name}, {expected!r})"
            )

        elif c_type == "file_exact_lines":
            lines = _nonblank_lines(target)
            ok = lines == list(expected)
            details.append(
                f"[{'PASS' if ok else 'FAIL'}] file_exact_lines({target.name}): got {lines}, expected {expected}"
            )

        elif c_type == "file_line_count":
            lines = _nonblank_lines(target)
            ok = len(lines) == int(expected)
            details.append(
                f"[{'PASS' if ok else 'FAIL'}] file_line_count({target.name}): got {len(lines)}, expected {expected}"
            )

        elif c_type == "file_substring_count":
            text = _read_text(target)
            sub = str(chk["substring"])
            cnt = text.count(sub)
            ok = cnt == int(expected)
            details.append(
                f"[{'PASS' if ok else 'FAIL'}] file_substring_count({target.name}, {sub!r}): got {cnt}, expected {expected}"
            )

        elif c_type == "json_valid":
            if not target.exists():
                ok = False
            else:
                try:
                    json.loads(target.read_text(encoding="utf-8"))
                    ok = True
                except Exception:
                    ok = False
            details.append(f"[{'PASS' if ok else 'FAIL'}] json_valid({target.name})")

        elif c_type == "json_path_equals":
            if not target.exists():
                ok = False
                actual = "<missing file>"
            else:
                try:
                    data = json.loads(target.read_text(encoding="utf-8"))
                    found, actual = get_json_path(data, chk.get("json_path", ""))
                    if not found:
                        ok = False
                    elif isinstance(expected, bool) or isinstance(actual, bool):
                        ok = type(expected) is type(actual) and actual == expected
                    elif isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
                        ok = abs(float(actual) - float(expected)) < 1e-6
                    else:
                        ok = actual == expected
                except Exception as e:
                    ok = False
                    actual = f"<invalid json: {e}>"
            details.append(
                f"[{'PASS' if ok else 'FAIL'}] json_path_equals({target.name}, {chk.get('json_path')}): got {actual!r}, expected {expected!r}"
            )

        elif c_type == "json_key_count":
            if not target.exists():
                ok = False
                cnt = -1
            else:
                try:
                    data = json.loads(target.read_text(encoding="utf-8"))
                    if not isinstance(data, (dict, list)):
                        raise ValueError("json_key_count requires an object or array")
                    cnt = len(data)
                    ok = cnt == int(expected)
                except Exception:
                    ok = False
                    cnt = -1
            details.append(
                f"[{'PASS' if ok else 'FAIL'}] json_key_count({target.name}): got {cnt}, expected {expected}"
            )

        else:
            ok = False
            details.append(f"[FAIL] Unknown state check type: {c_type}")

        if not ok:
            all_ok = False

    return all_ok, details


def evaluate_process_checks(
    checks: list[dict[str, Any]], tool_calls: list[dict[str, Any]]
) -> tuple[bool, list[str]]:
    """Evaluates declarative process assertions against the turn's tool call trace."""
    details = []
    all_ok = True
    called_names = [c["name"] for c in tool_calls]

    for chk in checks:
        c_type = chk["type"]

        if c_type == "min_total_tool_calls":
            min_c = int(chk["count"])
            ok = len(tool_calls) >= min_c
            details.append(
                f"[{'PASS' if ok else 'FAIL'}] min_total_tool_calls >= {min_c} (got {len(tool_calls)})"
            )

        elif c_type == "max_total_tool_calls":
            max_c = int(chk["count"])
            ok = len(tool_calls) <= max_c
            details.append(
                f"[{'PASS' if ok else 'FAIL'}] max_total_tool_calls <= {max_c} (got {len(tool_calls)})"
            )

        elif c_type == "tool_called":
            t_name = chk["tool_name"]
            ok = t_name in called_names
            details.append(
                f"[{'PASS' if ok else 'FAIL'}] tool_called({t_name!r}) in {called_names}"
            )

        elif c_type == "tool_not_called":
            t_name = chk["tool_name"]
            ok = t_name not in called_names
            details.append(
                f"[{'PASS' if ok else 'FAIL'}] tool_not_called({t_name!r}) in {called_names}"
            )

        elif c_type == "any_tool_called":
            allowed = set(chk.get("tool_names", []))
            ok = any(n in allowed for n in called_names)
            details.append(
                f"[{'PASS' if ok else 'FAIL'}] any_tool_called({list(allowed)}) in {called_names}"
            )

        elif c_type == "tool_call_count_equals":
            t_name = chk["tool_name"]
            expected_c = int(chk["count"])
            actual_c = called_names.count(t_name)
            ok = actual_c == expected_c
            details.append(
                f"[{'PASS' if ok else 'FAIL'}] tool_call_count_equals({t_name!r} == {expected_c}, got {actual_c})"
            )

        elif c_type == "tool_called_before":
            first_t = chk["first_tool"]
            second_t = chk["second_tool"]
            # Require an ordered pair, allowing a precautionary read before the
            # operation and another verification read afterwards. Distinct indices
            # also handle checks that intentionally name the same tool twice.
            ok = any(
                name == first_t and second_t in called_names[index + 1 :]
                for index, name in enumerate(called_names)
            )
            details.append(
                f"[{'PASS' if ok else 'FAIL'}] tool_called_before({first_t!r} -> {second_t!r}) in {called_names}"
            )

        elif c_type == "arguments_not_contain":
            forbidden = str(chk["forbidden_substring"])
            t_filter = chk.get("tool_name")
            violated = False
            for c in tool_calls:
                if t_filter and c["name"] != t_filter:
                    continue
                if forbidden in str(c.get("arguments", "")):
                    violated = True
            ok = not violated
            details.append(f"[{'PASS' if ok else 'FAIL'}] arguments_not_contain({forbidden!r})")

        else:
            ok = False
            details.append(f"[FAIL] Unknown process check type: {c_type}")

        if not ok:
            all_ok = False

    return all_ok, details


def evaluate_output_checks(
    checks: list[dict[str, Any]], final_answer: str
) -> tuple[bool, list[str]]:
    """Evaluates declarative output assertions against the assistant's final response."""
    details = []
    all_ok = True
    ans_lower = (final_answer or "").lower()

    for chk in checks:
        c_type = chk["type"]

        if c_type == "contains_any":
            vals = [str(v).lower() for v in chk.get("values", [])]
            ok = any(v in ans_lower for v in vals)
            details.append(f"[{'PASS' if ok else 'FAIL'}] output contains_any({vals})")

        elif c_type == "contains_all":
            vals = [str(v).lower() for v in chk.get("values", [])]
            ok = all(v in ans_lower for v in vals)
            details.append(f"[{'PASS' if ok else 'FAIL'}] output contains_all({vals})")

        elif c_type == "not_contains":
            vals = [str(v).lower() for v in chk.get("values", [])]
            ok = all(v not in ans_lower for v in vals)
            details.append(f"[{'PASS' if ok else 'FAIL'}] output not_contains({vals})")

        elif c_type == "regex_match":
            pattern = chk["pattern"]
            ok = bool(re.search(pattern, final_answer or "", flags=re.IGNORECASE | re.DOTALL))
            details.append(f"[{'PASS' if ok else 'FAIL'}] output regex_match({pattern!r})")

        else:
            ok = False
            details.append(f"[FAIL] Unknown output check type: {c_type}")

        if not ok:
            all_ok = False

    return all_ok, details
