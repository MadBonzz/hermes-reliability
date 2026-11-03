"""Campaign-local editing and immutable revision snapshots."""

import ast
import difflib
import json
import shutil
import tempfile
from pathlib import Path

from src.evaluation.benchmark import condition_spec, validate_benchmark
from src.evaluation.checks import (
    evaluate_output_checks,
    evaluate_process_checks,
    evaluate_state_checks,
)
from src.evaluation.runtime_support import sha256_file
from src.evaluation.scenario_validation import external_schema_ref
from src.evaluation.utils import resolve_ws_path


class Workspace:
    @classmethod
    def clone(cls, root, template):
        """Copy a saved campaign including authored scenarios and regression tests."""
        shutil.copytree(template, root, ignore=shutil.ignore_patterns("__pycache__"))
        result = cls.__new__(cls)
        result.root = Path(root).resolve()
        result.schema = result.root / "scenario.schema.json"
        return result

    def __init__(self, root, template):
        self.root = Path(root).resolve()
        self.schema = Path(template) / "scenario.schema.json"
        self.root.mkdir()
        for directory in ("src", "scenarios", "tests"):
            (self.root / directory).mkdir()
        shutil.copy2(Path(template) / "src/__init__.py", self.root / "src/__init__.py")
        shutil.copytree(
            Path(template) / "src/evaluation",
            self.root / "src/evaluation",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        shutil.copy2(self.schema, self.root / "scenario.schema.json")

    def path(self, name):
        path = self.root / name
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root) or resolved == self.root:
            raise ValueError("Path must be inside the campaign workspace")
        if any(
            p.is_symlink() or getattr(p, "is_junction", lambda: False)()
            for p in (path, *path.parents)
            if p != self.root.parent
        ):
            raise ValueError("Linked workspace paths are unsupported")
        relative = resolved.relative_to(self.root)
        allowed = (
            (
                len(relative.parts) == 2
                and (
                    (relative.parts[0] == "tests" and resolved.suffix == ".py")
                    or (relative.parts[0] == "scenarios" and resolved.suffix == ".json")
                )
            )
            or (
                len(relative.parts) == 3
                and relative.parts[:2] == ("src", "evaluation")
                and resolved.suffix == ".py"
            )
            or relative.as_posix() in {"scenario.schema.json"}
        )
        if not allowed:
            raise ValueError(
                "Editable files are src/evaluation/*.py, tests/*.py, scenarios/*.json and schemas"
            )
        return resolved

    def inventory(self):
        return {
            p.relative_to(self.root).as_posix(): sha256_file(p)
            for p in sorted(self.root.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts
        }

    def evaluator_changes(self, original):
        changes = {}
        for name, sha in self.inventory().items():
            if not name.startswith(("src/", "tests/")):
                continue
            old = Path(original) / name
            if old.exists() and sha256_file(old) == sha:
                continue
            before = old.read_text(encoding="utf-8") if old.exists() else ""
            after = (self.root / name).read_text(encoding="utf-8")
            changes[name] = "".join(
                difflib.unified_diff(
                    before.splitlines(True),
                    after.splitlines(True),
                    fromfile="original/" + name,
                    tofile="candidate/" + name,
                )
            )
        for old in (Path(original) / "src/evaluation").rglob("*.py"):
            name = old.relative_to(original).as_posix()
            if not (self.root / name).exists():
                changes[name] = "Deleted"
        return changes

    def oracle_validation(self, original, validation):
        """Summarize observed regression evidence, not oracle correctness."""
        before = Path(original) / "src/evaluation/checks.py"
        after = self.root / "src/evaluation/checks.py"
        try:
            changed = (
                False
                if not before.exists() and not after.exists()
                else (
                    ast.dump(ast.parse(before.read_text(encoding="utf-8")))
                    != ast.dump(ast.parse(after.read_text(encoding="utf-8")))
                )
            )
            comparison_error = None
        except (OSError, SyntaxError) as exc:
            changed, comparison_error = True, str(exc)
        tests = validation.get("test_files", [])
        return {
            "checker_code_changed": changed,
            "comparison_error": comparison_error,
            "regression_test_files": tests,
            "regression_exit_code": validation.get("tests_exit_code"),
            "passing_regression_execution_observed": bool(tests)
            and validation.get("tests_exit_code") == 0,
            "interpretation": "Changed checker code requires executable regression evidence before verification review can accept. AST comparison ignores formatting/comments; it does not prove behavioral equivalence or change. File presence or a zero exit alone does not establish meaningful coverage: inspect positive/negative examples and reject token/comment-only impostors. Other evaluator mechanisms and unchanged checks still require semantic review.",
        }

    def action(self, request, readonly=False):
        if any(key in request for key in ("source", "read", "actions")):
            raise ValueError(
                "Use source/read/actions as separate top-level envelopes, not nested workspace actions"
            )
        operation = request["op"]
        if operation == "list":
            return self.inventory()
        if operation == "probe":
            return self.probe(request)
        path = self.path(request["path"])
        if operation == "read":
            text = path.read_text(encoding="utf-8")
            offset, length = request.get("offset", 0), request.get("length", 16000)
            if type(offset) is not int or type(length) is not int or offset < 0 or length < 1:
                raise ValueError("Invalid read range")
            return {
                "text": text[offset : offset + length],
                "characters": len(text),
                "sha256": sha256_file(path),
                "offset": offset,
            }
        if readonly:
            raise ValueError("Reviewers have read-only access")
        if path.name in {"scenario.schema.json"}:
            raise ValueError("The benchmark schema is fixed for both evaluator lanes")
        if operation == "write":
            if path.exists():
                raise ValueError("Use replace with expected_sha256 for existing files")
            if path.suffix == ".json":
                json.loads(request["content"])
            path.write_text(request["content"], encoding="utf-8")
        elif operation in {"replace", "delete"}:
            if request.get("expected_sha256") != sha256_file(path):
                raise ValueError("File changed or expected_sha256 missing; read it again")
            if operation == "delete":
                path.unlink()
                return {"deleted": request["path"]}
            text = path.read_text(encoding="utf-8")
            old = request["old"]
            if not old or text.count(old) != 1:
                raise ValueError("Replacement must match exactly once")
            updated = text.replace(old, request["new"], 1)
            if path.suffix == ".json":
                json.loads(updated)
            path.write_text(updated, encoding="utf-8")
        else:
            raise ValueError("Unknown workspace operation")
        return {"path": request["path"], "sha256": sha256_file(path)}

    def probe(self, request):
        """Test supplied examples with trusted built-in oracles; never execute task code."""
        path = self.path(request["path"])
        if path.parent != self.root / "scenarios" or path.suffix != ".json":
            raise ValueError("Probe requires a scenario JSON path")
        condition = request.get("condition", "ordinary")
        original = json.loads(path.read_text(encoding="utf-8"))
        allowed = {"baseline", "treatment"} if original.get("experiment") else {"ordinary"}
        if condition not in allowed:
            raise ValueError(f"Probe condition must be one of {sorted(allowed)}")
        spec = condition_spec(original, condition)
        number = request["turn_number"]
        if type(number) is not int or not 1 <= number <= len(spec["turns"]):
            raise ValueError("Probe turn outside scenario")
        if type(request.get("expect_pass")) is not bool or not request.get("reason"):
            raise ValueError(
                "Describe why this example should pass or fail, with expect_pass boolean"
            )
        turn = spec["turns"][number - 1]
        if any(c["type"] == "artifact_python" for c in turn.get("state_checks", [])):
            raise ValueError(
                "artifact_python requires isolated Docker verification; this host-only probe "
                "cannot execute it. Use native reference validation and isolated counterexamples."
            )
        files = request.get("files", {})
        if not isinstance(files, dict) or not all(isinstance(v, str) for v in files.values()):
            raise ValueError("Probe files must map workspace-relative paths to text")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            # Checks never follow author-supplied paths outside this fresh fixture tree.
            for check in turn.get("state_checks", []):
                if "path" in check:
                    resolve_ws_path(root, check["path"])
                if check.get("type") == "settlement_files_match":
                    for invoice in check.get("expected", []):
                        identity = invoice["invoice_id"]
                        if not isinstance(identity, str) or any(
                            c in identity for c in ("/", "\\", ":")
                        ):
                            raise ValueError("Probe settlement invoice IDs must be plain filenames")
            for name, content in files.items():
                target = resolve_ws_path(root, name)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8", newline="")
            results = {
                "state": evaluate_state_checks(
                    turn.get("state_checks", []),
                    root,
                    mock_snapshot=request.get("mock_snapshot"),
                    before_workspace=request.get("before_workspace"),
                ),
                "process": evaluate_process_checks(
                    turn.get("process_checks", []), request.get("calls", [])
                ),
                "output": evaluate_output_checks(
                    turn.get("output_checks", []), request.get("final_response", "")
                ),
            }
        passed = all(ok for ok, _ in results.values())
        checker = Path(__file__).parents[1] / "evaluation/checks.py"
        candidate_checker = self.root / "src/evaluation/checks.py"
        return {
            "scenario_sha256": sha256_file(path),
            "condition": condition,
            "turn_number": number,
            "scope": "Trusted built-in oracles on supplied synthetic artifacts, calls and response. No tools, model, native runtime, candidate code or task code executed. This does not establish end-to-end feasibility or semantic validity of the supplied example.",
            "checker_sha256": sha256_file(checker),
            "candidate_checker_matches": candidate_checker.exists()
            and sha256_file(candidate_checker) == sha256_file(checker),
            "passed": passed,
            "matches_expectation": passed == request["expect_pass"],
            "checks": {
                group: {"passed": ok, "details": details}
                for group, (ok, details) in results.items()
            },
        }

    def review_scenarios(self):
        """Keep invalid-but-readable drafts available for reviewer diagnosis."""
        cases, errors = [], []
        for path in sorted((self.root / "scenarios").glob("*.json")):
            try:
                case = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(case, dict) or not isinstance(case.get("id"), str):
                    raise ValueError("Scenario needs an object with a string id")
                cases.append(case)
            except (ValueError, OSError) as exc:
                errors.append({"path": path.relative_to(self.root).as_posix(), "error": str(exc)})
        return {"scenarios": cases, "errors": errors}

    def validate(self, limit, *, check_code=True):
        cases = []
        for path in sorted((self.root / "scenarios").glob("*.json")):
            try:
                case = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path.name}: invalid JSON: {exc}") from exc
            if external_schema_ref(case):
                raise ValueError("External schema references are unsupported")
            if path.stem != case["id"]:
                raise ValueError("Scenario filename must equal its id")
            cases.append(case)
        if not 1 <= len(cases) <= limit:
            raise ValueError(f"Active suite must contain 1 to {limit} scenarios")
        if check_code:
            for path in [
                *(self.root / "src/evaluation").rglob("*.py"),
                *(self.root / "tests").glob("*.py"),
            ]:
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        # Keep the benchmark format and basic path contracts fixed. Each evaluator's
        # preflight validates its own implementation capabilities independently.
        validate_benchmark(
            {"suite_name": "campaign", "scenarios": cases},
            self.schema,
            require_portable_prefix=False,
            runtime_kind=getattr(self, "runtime_kind", "hermes"),
        )
        for case in cases:
            experiment = case.get("experiment", {})
            baseline_faults = experiment.get("baseline_complexity", {}).get("fault_count", 0)
            if baseline_faults > case["complexity"].get("fault_count", 0):
                raise ValueError(
                    "Put the tested perturbation in treatment; baseline must not increase the fault count"
                )
            for turn in case["turns"][:-1]:
                if "reference" not in turn:
                    raise ValueError(
                        f"{case['id']} needs a validated reference for prerequisite turn {turn['turn_number']}"
                    )
        return cases

    def snapshot(self, destination):
        shutil.copytree(
            self.root, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )
        return self.inventory()
