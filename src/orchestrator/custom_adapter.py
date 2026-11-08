"""Trusted, user-configured integrations for arbitrary agent frameworks.

The factory is never supplied by model output. It implements the existing
preflight/execute_revision interface and must retain the benchmark evidence contract.
"""

import importlib.util
import json
import sys
from pathlib import Path

from src.evaluation.benchmark import validate_benchmark
from src.evaluation.runtime_support import sha256_file, write_json_atomic
from src.evaluation.scenario_validation import load_scenarios
from src.orchestrator.adapters import ROOT, revision_hashes


class CustomAdapter:
    def __init__(self, config):
        self.config = config
        self.schema_path = ROOT / "scenario.schema.json"
        self.schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        self.implementation = None
        self.reference_tools = None
        factory = config.get("factory")
        if not factory:
            return
        filename, separator, name = factory.rpartition(":")
        path = Path(filename)
        if (
            not separator
            or not name.isidentifier()
            or not path.is_absolute()
            or not path.is_file()
            or path.suffix != ".py"
        ):
            raise ValueError(
                "Custom adapter factory must be /absolute/path/adapter.py:create_adapter"
            )
        self.factory_sha256 = sha256_file(path)
        spec = importlib.util.spec_from_file_location(
            "reliability_custom_adapter_" + self.factory_sha256, path
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        self.implementation = getattr(module, name)(dict(config))
        for method in ("describe", "preflight", "execute_revision"):
            if not callable(getattr(self.implementation, method, None)):
                raise ValueError(f"Custom adapter must implement {method}")
        description = self.implementation.describe()
        if not isinstance(description, dict) or not description.get("framework"):
            raise ValueError("Custom adapter describe() must identify the framework")
        names = description.get("reference_tools")
        if not isinstance(names, list) or any(not isinstance(n, str) or not n for n in names):
            raise ValueError(
                "Custom adapter must declare reference_tools (an empty list is allowed)"
            )
        self.reference_tools = names

    def describe(self):
        if self.implementation is None:
            return {
                "framework": self.config.get("name", "custom"),
                "execution_available": False,
                "reference_tools": None,
                "scope": "Source-grounded scenario design. Native tool names and execution capabilities must be established from source; execution requires a configured adapter factory.",
            }
        return {
            **self.implementation.describe(),
            "execution_available": True,
            "adapter_factory_sha256": self.factory_sha256,
        }

    def _cases(self, evaluator):
        suite, _ = load_scenarios(
            Path(evaluator) / "scenarios",
            self.schema_path,
            runtime_kind="custom",
            reference_tools=self.reference_tools,
        )
        validate_benchmark(
            suite,
            self.schema_path,
            runtime_kind="custom",
            reference_tools=self.reference_tools,
            require_portable_prefix=False,
        )
        return suite["scenarios"]

    def preflight(self, evaluator, output):
        output = Path(output)
        output.mkdir(parents=True, exist_ok=True)
        result = {"passed": False}
        try:
            if self.implementation is None:
                raise ValueError("Execution requires adapter.factory for this custom agent")
            self._cases(evaluator)
            before = revision_hashes(evaluator)
            result = self.implementation.preflight(Path(evaluator), output)
            if not isinstance(result, dict) or type(result.get("passed")) is not bool:
                raise ValueError(
                    "Custom preflight must return a passed boolean and validation evidence"
                )
            if revision_hashes(evaluator) != before:
                raise ValueError("Validation modified evaluator source or scenario definitions")
        except (ValueError, OSError, TypeError) as exc:
            result = {"passed": False, "error": str(exc)}
        write_json_atomic(output / "report.json", result)
        return result

    def execute_revision(self, evaluator, output, target, key):
        evaluator, output = Path(evaluator), Path(output)
        validation = self.preflight(evaluator, output.parent / "preflight")
        if not validation["passed"]:
            return {"status": "invalid_evaluator", "validation": validation}
        before = revision_hashes(evaluator)
        result = self.implementation.execute_revision(evaluator, output, target, key)
        try:
            if not isinstance(result, dict):
                raise ValueError("Custom execution must return an execution-result object")
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            if not isinstance(report.get("cases"), list):
                raise ValueError(
                    "Custom execution must write the benchmark report and raw phase evidence"
                )
            if result.get("status") != report.get("status"):
                raise ValueError("Custom result and saved report status disagree")
            if result.get("status") == "complete":
                definitions = {c["id"]: c for c in self._cases(evaluator)}
                expected = set(definitions)
                observed = [c["id"] for c in report["cases"]]
                if set(observed) != expected or len(observed) != len(expected):
                    raise ValueError(
                        "Custom execution did not report every active scenario exactly once"
                    )
                for case in report["cases"]:
                    if not isinstance(case.get("branches"), dict):
                        raise ValueError("Custom case must include a branches object")
                    if not case.get("branches") and not case.get("shared_prefix"):
                        raise ValueError("Custom execution lacks branch or failed-prefix evidence")
                    conditions = (
                        {"baseline", "treatment"}
                        if definitions[case["id"]].get("experiment")
                        else {"ordinary"}
                    )
                    if case["branches"] and set(case["branches"]) != conditions:
                        raise ValueError("Custom execution must report every controlled condition")
                    phases = (
                        list(case["branches"].values())
                        + [
                            case[name]
                            for name in ("shared_prefix", "diagnostic_prefix")
                            if case.get(name)
                        ]
                        + case.get("diagnostic_continuations", [])
                    )
                    if any(
                        not isinstance(phase, dict)
                        or not phase.get("api_trace_path")
                        or not isinstance(phase.get("turns"), list)
                        or (phase.get("status") != "error" and not phase["turns"])
                        for phase in phases
                    ):
                        raise ValueError("Every custom phase needs API trace and turn evidence")
            if revision_hashes(evaluator) != before:
                raise ValueError("Execution modified evaluator source or scenario definitions")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return {"status": "invalid_evaluator", "error": str(exc)}
        return {**result, "report": str(output / "report.json"), "revision_unchanged": True}
