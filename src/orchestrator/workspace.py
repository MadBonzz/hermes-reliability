"""Campaign-local editing and immutable revision snapshots."""

import ast
import difflib
import json
import shutil
import tempfile
import uuid
from pathlib import Path

from src.evaluation.artifact_checks import evaluate_artifact_check
from src.evaluation.benchmark import condition_spec, validate_benchmark
from src.evaluation.checks import (
    evaluate_output_checks,
    evaluate_process_checks,
    evaluate_state_checks,
)
from src.evaluation.runtime_support import sha256_file
from src.evaluation.scenario_validation import external_schema_ref
from src.evaluation.utils import resolve_ws_path
from src.orchestrator.quality import check_catalog


class Workspace:
    # Protocol-level edit protection, not an OS sandbox. Candidate Python remains
    # executable; external frozen audits and revision hashes are still required.
    PROTECTED = {
        "api_trace.py",
        "audit.py",
        "audit_api_evidence.py",
        "runtime_support.py",
        "control_observer.py",
        "execution_isolation.py",
    }

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
        if operation == "validate":
            cases = self.validate(getattr(self, "active_limit", 12))
            return {
                "valid": True,
                "scenario_count": len(cases),
                "check_catalog": check_catalog(cases),
                "scope": "Schema, source syntax and contract references only; no model, native reference execution or semantic oracle validation.",
            }
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
        if path.parent == self.root / "src/evaluation" and path.name in self.PROTECTED:
            raise ValueError(
                "Measurement and evidence-policy code is read-only; propose a separately reviewed change"
            )
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
        """Test examples with trusted oracles; executable checks stay in Docker."""
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
        available = {
            ref: check
            for ref, check in check_catalog([original])[original["id"]].items()
            if ref.startswith(f"{condition}:{number}:")
        }
        selected = request.get("check_ids", list(available))
        if (
            not isinstance(selected, list)
            or not selected
            or not all(isinstance(ref, str) for ref in selected)
            or len(selected) != len(set(selected))
            or not set(selected).issubset(available)
        ):
            raise ValueError(
                "check_ids must select unique current assertions for this condition and turn"
            )
        selected_set = set(selected)
        # Preserve contract order and indexes, even if the selection is reordered.
        selected = [ref for ref in available if ref in selected_set]
        turn = {
            **turn,
            **{
                group + "_checks": [
                    check
                    for index, check in enumerate(turn.get(group + "_checks", []))
                    if f"{condition}:{number}:{group}:{index}" in selected_set
                ]
                for group in ("state", "process", "output")
            },
        }
        artifact_checks = [
            c for c in turn.get("state_checks", []) if c["type"] == "artifact_python"
        ]
        artifact_runtime = getattr(self, "artifact_runtime", None)
        if artifact_checks and not artifact_runtime:
            raise ValueError(
                "artifact_python requires isolated Docker verification; this host-only probe "
                "cannot execute it. Use native reference validation and isolated counterexamples."
            )
        files = request.get("files", {})
        if not isinstance(files, dict) or not all(isinstance(v, str) for v in files.values()):
            raise ValueError("Probe files must map workspace-relative paths to text")
        output_files = request.get("output_files", {})
        if not isinstance(output_files, dict) or not all(
            isinstance(v, str) for v in output_files.values()
        ):
            raise ValueError("Probe output_files must map outputs-relative paths to text")
        output_scope = any(
            c.get("artifact_scope") == "outputs" for c in turn.get("state_checks", [])
        )
        if output_files or output_scope:
            if getattr(self, "runtime_kind", "hermes") != "deerflow":
                raise ValueError("Native output probes require the DeerFlow profile")
            if any(c.get("artifact_scope") == "outputs" for c in artifact_checks):
                raise ValueError("artifact_python currently uses the task workspace")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "workspace"
            root.mkdir()
            outputs = root.parent / "outputs"
            outputs.mkdir()
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
            for directory, contents in ((root, files), (outputs, output_files)):
                for name, content in contents.items():
                    target = resolve_ws_path(directory, name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8", newline="")
            results = {
                "state": evaluate_state_checks(
                    [c for c in turn.get("state_checks", []) if c["type"] != "artifact_python"],
                    root,
                    mock_snapshot=request.get("mock_snapshot"),
                    before_workspace=request.get("before_workspace"),
                    artifact_roots={"outputs": outputs} if output_scope else {},
                    before_artifacts=request.get("before_artifacts"),
                ),
                "process": evaluate_process_checks(
                    turn.get("process_checks", []), request.get("calls", [])
                ),
                "output": evaluate_output_checks(
                    turn.get("output_checks", []), request.get("final_response", "")
                ),
            }
        artifact_results = []
        if artifact_checks:
            snapshot = {name: {"kind": "file", "text": content} for name, content in files.items()}
            probe_root = self.root.parent / "oracle_probes" / uuid.uuid4().hex
            for index, check in enumerate(artifact_checks):
                observed = evaluate_artifact_check(
                    check, snapshot, **artifact_runtime, output=probe_root / str(index)
                )
                artifact_results.append(observed)
            results["artifact"] = (
                all(item["passed"] for item in artifact_results),
                [
                    {k: item[k] for k in ("name", "passed", "exit_code", "timed_out")}
                    for item in artifact_results
                ],
            )
        passed = all(ok for ok, _ in results.values())
        checker_paths = ["src/evaluation/checks.py"]
        if artifact_checks:
            checker_paths.append("src/evaluation/artifact_checks.py")
        checker_evidence = {}
        for relative in checker_paths:
            trusted_path = Path(__file__).parents[2] / relative
            candidate_path = self.root / relative
            trusted_hash = sha256_file(trusted_path)
            candidate_hash = sha256_file(candidate_path) if candidate_path.exists() else None
            checker_evidence[relative] = {
                "trusted_sha256": trusted_hash,
                "candidate_sha256": candidate_hash,
                "matches": trusted_hash == candidate_hash,
            }
        return {
            "scenario_sha256": sha256_file(path),
            "condition": condition,
            "turn_number": number,
            "selected_check_ids": selected,
            "unselected_check_ids": [ref for ref in available if ref not in selected_set],
            "scope": "Trusted checkers on supplied synthetic artifacts, calls and response. No model or agent trajectory. If artifact_checks is nonempty, authored checks and supplied code run only in isolated copies in the pinned Docker image; never on the host. This does not establish end-to-end feasibility or semantic validity of the supplied example.",
            "artifact_checks": artifact_results,
            "checker_sha256": checker_evidence["src/evaluation/checks.py"]["trusted_sha256"],
            "checker_evidence": checker_evidence,
            "candidate_checker_matches": all(item["matches"] for item in checker_evidence.values()),
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
            reference_tools=getattr(self, "reference_tools", None),
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

    def seed(self, selection, limit):
        """Import a verified earlier scenario revision into a fresh campaign."""
        source = Path(selection["directory"]).resolve()
        expected = selection["sha256"]
        files = sorted(source.glob("*.json"))
        if not expected or {p.name for p in files} != set(expected):
            raise ValueError("Saved scenario selection changed")
        if not 1 <= len(files) <= limit:
            raise ValueError(f"Saved suite must contain 1 to {limit} tests")
        if any(p.is_symlink() or sha256_file(p) != expected[p.name] for p in files):
            raise ValueError("Saved scenario content changed")
        for path in files:
            shutil.copy2(path, self.root / "scenarios" / path.name)
        self.validate(limit)
        return {"directory": str(source), "sha256": expected, "count": len(files)}
