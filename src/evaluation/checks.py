"""Deterministic state, process, and output assertions for evaluation turns."""

import ast
import csv
import json
import math
import re
from pathlib import Path
from typing import Any

from .mock_services import json_array_equal_unordered, json_equal
from .runtime_support import sha256_file
from .utils import get_json_path, resolve_ws_path


def _load_json(text):
    """Reject ambiguous object keys and non-JSON numeric constants in scored data."""

    def object_from_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError(f"Invalid JSON constant: {value}")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"Non-finite JSON number: {value}")
        return number

    return json.loads(
        text,
        object_pairs_hook=object_from_pairs,
        parse_constant=reject_constant,
        parse_float=finite_float,
    )


def _read_text(path: Path) -> str:
    """Missing paths count as empty content for the existing text assertions."""
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _nonblank_lines(path: Path) -> list[str]:
    return [line.strip() for line in _read_text(path).splitlines() if line.strip()]


def evaluate_state_checks(
    checks: list[dict[str, Any]], workspace: Path, *, mock_snapshot=None, before_workspace=None
) -> tuple[bool, list[str]]:
    """Malformed/unreadable agent artifacts fail assertions, not the entire suite."""
    all_ok, details = True, []
    for check in checks:
        try:
            ok, messages = _evaluate_state_check(
                check, workspace, mock_snapshot=mock_snapshot, before_workspace=before_workspace
            )
        except (OSError, ValueError, TypeError) as exc:
            ok, messages = False, [f"[FAIL] {check['type']}: unavailable artifact: {exc}"]
        all_ok = all_ok and ok
        details.extend(messages)
    return all_ok, details


def _evaluate_state_check(chk, workspace, *, mock_snapshot=None, before_workspace=None):
    """Evaluates declarative state assertions against the workspace filesystem."""
    details = []
    c_type = chk["type"]
    target = resolve_ws_path(workspace, chk.get("path", ""))
    target_label = target.relative_to(workspace.resolve()).as_posix()
    expected = chk.get("expected")

    if c_type == "settlement_files_match":
        # This is the reimbursement contract: status must literally be "paid".
        # It is not a generic mirror of the service response's status field.
        try:
            records = mock_snapshot["services"][chk["service"]]["records"]
            projected = [
                {k: r[k] for k in ("invoice_id", "recipient", "amount_usd")} for r in records
            ]
            desired = [
                {k: r[k] for k in ("invoice_id", "recipient", "amount_usd")} for r in expected
            ]
            ok = json_array_equal_unordered(projected, desired)
            details.append(
                f"[{'PASS' if ok else 'FAIL'}] settlement ledger records: "
                f"got {projected!r}, expected unordered {desired!r}"
            )
            receipts = [r for r in mock_snapshot["receipts"] if r["service"] == chk["service"]]
            ok = ok and len(receipts) == len(expected)
            details.append(
                f"[{'PASS' if len(receipts) == len(expected) else 'FAIL'}] settlement receipt count: "
                f"got {len(receipts)}, expected {len(expected)}"
            )
            for invoice in expected:
                matches = [
                    r for r in receipts if r["arguments"]["invoice_id"] == invoice["invoice_id"]
                ]
                if len(matches) != 1:
                    ok = False
                    details.append(
                        f"[FAIL] settlement {invoice['invoice_id']} receipt matches: "
                        f"got {len(matches)}, expected 1"
                    )
                    continue
                receipt = matches[0]["response"]["receipt_id"]
                actual = _load_json(
                    (target / (invoice["invoice_id"] + ".json")).read_text(encoding="utf-8")
                )
                desired_file = {**invoice, "status": "paid", "receipt_id": receipt}
                file_ok = json_equal(actual, desired_file)
                ok = ok and file_ok
                details.append(
                    f"[{'PASS' if file_ok else 'FAIL'}] settlement "
                    f"{target_label}/{invoice['invoice_id']}.json: "
                    f"got {actual!r}, expected {desired_file!r}"
                )
            actual_files = sorted(p.name for p in target.iterdir())
            expected_files = sorted(r["invoice_id"] + ".json" for r in expected)
            files_ok = actual_files == expected_files
            ok = ok and files_ok
            details.append(
                f"[{'PASS' if files_ok else 'FAIL'}] settlement directory entries: "
                f"got {actual_files!r}, expected {expected_files!r}"
            )
        except (KeyError, OSError, TypeError, ValueError) as exc:
            ok = False
            details.append(f"[FAIL] settlement evidence unavailable: {type(exc).__name__}: {exc}")
        return ok, details

    if c_type in {
        "json_equals",
        "json_array_unordered",
        "file_sha256",
        "python_syntax",
        "python_ast_equals",
        "directory_files",
        "mock_state_equals",
        "mock_state_contains",
        "mock_state_not_contains",
        "mock_effects_equal",
        "mock_array_unordered",
        "mock_text_matches_file",
        "mock_json_matches_file",
        "csv_rows_equal",
    }:
        try:
            if c_type in {
                "mock_state_equals",
                "mock_state_contains",
                "mock_state_not_contains",
                "mock_array_unordered",
                "mock_text_matches_file",
                "mock_json_matches_file",
            }:
                found, actual = get_json_path(mock_snapshot, chk["json_path"])
                if c_type == "mock_state_equals":
                    ok = found and json_equal(actual, expected)
                elif c_type == "mock_array_unordered":
                    ok = found and json_array_equal_unordered(actual, expected)
                elif c_type == "mock_json_matches_file":
                    expected = _load_json(target.read_text(encoding="utf-8"))
                    file_found, expected = get_json_path(expected, chk["file_json_path"])
                    ok = found and file_found and json_equal(actual, expected)
                elif c_type == "mock_text_matches_file":
                    version = chk.get("file_version", "after_turn")
                    if version == "before_turn":
                        if before_workspace is None:
                            raise ValueError("Before-turn workspace evidence is unavailable")
                        relative = target.relative_to(workspace.resolve()).as_posix()
                        item = before_workspace[relative]
                        if item.get("kind", "file") != "file" or "text" not in item:
                            raise ValueError("Before-turn file text is unavailable")
                        # Match read_text's universal-newline handling while keeping
                        # the recorded snapshot's exact UTF-8 bytes unchanged.
                        expected = item["text"].replace("\r\n", "\n").replace("\r", "\n").strip()
                    elif version == "after_turn":
                        expected = target.read_text(encoding="utf-8").strip()
                    else:
                        raise ValueError(f"Unknown file_version: {version}")
                    ok = found and isinstance(actual, str) and actual.strip() == expected
                else:
                    ok = (
                        found
                        and isinstance(actual, str)
                        and isinstance(expected, str)
                        and ((expected in actual) == (c_type == "mock_state_contains"))
                    )
            elif c_type == "mock_effects_equal":
                actual = [event["tool"] for event in mock_snapshot["effects"]]
                ok = actual == expected
            elif c_type == "file_sha256":
                actual = sha256_file(target)
                ok = actual == expected
            elif c_type == "python_syntax":
                ast.parse(target.read_text(encoding="utf-8"), filename=str(target))
                actual, ok = "valid Python syntax", True
            elif c_type == "python_ast_equals":
                actual = ast.dump(ast.parse(target.read_text(encoding="utf-8")))
                ok = actual == ast.dump(ast.parse(expected))
            elif c_type == "directory_files":
                if not target.is_dir():
                    raise ValueError("Directory does not exist")
                actual = sorted(
                    p.relative_to(target).as_posix()
                    for p in target.rglob("*")
                    if p.is_file() or p.is_symlink()
                )
                ok = actual == sorted(expected)
            elif c_type == "csv_rows_equal":
                with target.open(encoding="utf-8-sig", newline="") as stream:
                    reader = csv.DictReader(stream)
                    fields = reader.fieldnames or []
                    if len(fields) != len(set(fields)):
                        raise ValueError("Duplicate CSV columns")
                    actual = list(reader)
                for row in actual:
                    for column in chk.get("numeric_columns", []):
                        number = float(row[column])
                        if not math.isfinite(number):
                            raise ValueError("Non-finite CSV number")
                        row[column] = number
                ok = json_array_equal_unordered(actual, expected)
            else:
                actual = _load_json(target.read_text(encoding="utf-8"))
                if "json_path" in chk:
                    found, actual = get_json_path(actual, chk["json_path"])
                    if not found:
                        raise ValueError("JSON path does not exist")
                if c_type == "json_array_unordered":
                    if "item_key" in chk:
                        if not isinstance(actual, list):
                            raise ValueError("Expected a JSON array")
                        selected = []
                        for item in actual:
                            if isinstance(item, dict):
                                found, item = get_json_path(item, chk["item_key"])
                                if not found:
                                    raise ValueError("Array item key does not exist")
                            selected.append(item)
                        actual = selected
                    ok = json_array_equal_unordered(actual, expected)
                else:
                    ok = json_equal(actual, expected)
        except (OSError, ValueError, TypeError, KeyError, SyntaxError) as exc:
            ok, actual = False, f"<unavailable: {exc}>"
        details.append(
            f"[{'PASS' if ok else 'FAIL'}] {c_type}: got {actual!r}, expected {expected!r}"
        )

    elif c_type == "file_exists":
        ok = target.is_file()
        details.append(f"[{'PASS' if ok else 'FAIL'}] file_exists({target_label})")

    elif c_type == "file_not_exists":
        ok = not target.exists()
        details.append(f"[{'PASS' if ok else 'FAIL'}] file_not_exists({target_label})")

    elif c_type in {"file_contains", "file_not_contains"}:
        text = _read_text(target)
        ok = (str(expected) in text) == (c_type == "file_contains")
        details.append(f"[{'PASS' if ok else 'FAIL'}] {c_type}({target_label}, {expected!r})")

    elif c_type == "file_exact_lines":
        lines = _nonblank_lines(target)
        ok = lines == list(expected)
        details.append(
            f"[{'PASS' if ok else 'FAIL'}] file_exact_lines({target_label}): got {lines}, expected {expected}"
        )

    elif c_type == "file_line_count":
        lines = _nonblank_lines(target)
        ok = len(lines) == int(expected)
        details.append(
            f"[{'PASS' if ok else 'FAIL'}] file_line_count({target_label}): got {len(lines)}, expected {expected}"
        )

    elif c_type == "file_substring_count":
        text = _read_text(target)
        sub = str(chk["substring"])
        cnt = text.count(sub)
        ok = cnt == int(expected)
        details.append(
            f"[{'PASS' if ok else 'FAIL'}] file_substring_count({target_label}, {sub!r}): got {cnt}, expected {expected}"
        )

    elif c_type == "json_valid":
        if not target.exists():
            ok = False
        else:
            try:
                _load_json(target.read_text(encoding="utf-8"))
                ok = True
            except Exception:
                ok = False
        details.append(f"[{'PASS' if ok else 'FAIL'}] json_valid({target_label})")

    elif c_type == "json_path_equals":
        if not target.exists():
            ok = False
            actual = "<missing file>"
        else:
            try:
                data = _load_json(target.read_text(encoding="utf-8"))
                found, actual = get_json_path(data, chk.get("json_path", ""))
                if not found:
                    ok = False
                elif isinstance(expected, bool) or isinstance(actual, bool):
                    ok = type(expected) is type(actual) and actual == expected
                elif isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
                    ok = abs(float(actual) - float(expected)) < 1e-6
                else:
                    ok = json_equal(actual, expected)
            except Exception as e:
                ok = False
                actual = f"<invalid json: {e}>"
        details.append(
            f"[{'PASS' if ok else 'FAIL'}] json_path_equals({target_label}, {chk.get('json_path')}): got {actual!r}, expected {expected!r}"
        )

    elif c_type == "json_key_count":
        if not target.exists():
            ok = False
            cnt = -1
        else:
            try:
                data = _load_json(target.read_text(encoding="utf-8"))
                if not isinstance(data, (dict, list)):
                    raise ValueError("json_key_count requires an object or array")
                cnt = len(data)
                ok = cnt == int(expected)
            except Exception:
                ok = False
                cnt = -1
        details.append(
            f"[{'PASS' if ok else 'FAIL'}] json_key_count({target_label}): got {cnt}, expected {expected}"
        )

    else:
        ok = False
        details.append(f"[FAIL] Unknown state check type: {c_type}")

    return ok, details


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

        elif c_type in {"tool_called", "tool_not_called"}:
            t_name = chk["tool_name"]
            ok = (t_name in called_names) == (c_type == "tool_called")
            details.append(f"[{'PASS' if ok else 'FAIL'}] {c_type}({t_name!r}) in {called_names}")

        elif c_type == "any_tool_called":
            allowed = set(chk.get("tool_names", []))
            ok = any(n in allowed for n in called_names)
            details.append(
                f"[{'PASS' if ok else 'FAIL'}] any_tool_called({list(allowed)}) in {called_names}"
            )

        elif c_type in {"tool_call_count_equals", "min_tool_call_count"}:
            t_name = chk["tool_name"]
            expected_c = int(chk["count"])
            actual_c = called_names.count(t_name)
            ok = (
                actual_c == expected_c
                if c_type == "tool_call_count_equals"
                else actual_c >= expected_c
            )
            details.append(
                f"[{'PASS' if ok else 'FAIL'}] {c_type}({t_name!r}, expected {expected_c}, got {actual_c})"
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

        elif c_type == "tool_called_after_last":
            prerequisite = chk["after_tool"]
            observed = chk["tool_name"]
            indices = [i for i, name in enumerate(called_names) if name == prerequisite]
            ok = bool(indices) and observed in called_names[indices[-1] + 1 :]
            details.append(
                f"[{'PASS' if ok else 'FAIL'}] {observed!r} after last {prerequisite!r}: {called_names}"
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

        if c_type == "json_answer":
            text = (final_answer or "").strip()
            if text.startswith("```") and text.endswith("```"):
                text = "\n".join(text.splitlines()[1:-1])
            try:
                answer = _load_json(text)
                found, actual = get_json_path(answer, chk.get("json_path", ""))
                compare = json_array_equal_unordered if chk.get("unordered") else json_equal
                ok = found and compare(actual, chk["expected"])
            except (ValueError, TypeError):
                actual, ok = "<invalid JSON answer>", False
            details.append(
                f"[{'PASS' if ok else 'FAIL'}] json_answer: got {actual!r}, expected {chk['expected']!r}"
            )

        elif c_type == "contains_any":
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
