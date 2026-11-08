"""Execution adapters for the separate exploratory orchestrator.

Adapters are trusted local configuration, never model-authored executable code.
"""

import json
import os
import subprocess
from pathlib import Path

import jsonschema

from ..evaluation.runtime_support import sha256_file
from ..evaluation.scenario_validation import external_schema_ref, reference_tool_names

ROOT = Path(__file__).resolve().parents[2]


def revision_hashes(directory):
    root = Path(directory)
    files = [*root.glob("*.schema.json")]
    for name, suffix in (("src", "*.py"), ("tests", "*.py"), ("scenarios", "*.json")):
        files.extend((root / name).rglob(suffix))
    return {str(p.relative_to(root)): sha256_file(p) for p in sorted(files)}


class HermesAdapter:
    """benchmark preflight and execution using a separately installed Hermes runtime."""

    def __init__(self, config):
        self.config = config
        self.schema_path = ROOT / "scenario.schema.json"
        self.schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        if external_schema_ref(self.schema):
            raise ValueError("External JSON Schema references are unsupported")
        jsonschema.Draft7Validator.check_schema(self.schema)

    def validation_command(self, evaluator, output):
        return [
            self.config["python"],
            str(ROOT / "src/orchestrator/hermes_worker.py"),
            "--validate-revision",
            str(evaluator),
            "--hermes-root",
            self.config["hermes_root"],
            "--validation-output",
            str(output / "report.json"),
        ]

    def validation_environment(self):
        return os.environ.copy()

    def preflight(self, evaluator, output):
        """Run copied validation, authored prerequisites and local evaluator tests."""
        output = Path(output)
        output.mkdir(parents=True)
        before = revision_hashes(evaluator)
        command = self.validation_command(evaluator, output)
        with (output / "worker.log").open("w", encoding="utf-8") as log:
            try:
                process = subprocess.run(
                    command,
                    cwd=evaluator,
                    env=self.validation_environment(),
                    stdout=log,
                    stderr=log,
                    timeout=self.config.get("timeout_seconds"),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except subprocess.TimeoutExpired:
                return {
                    "passed": False,
                    "error": "Preflight timed out",
                    "log": str(output / "worker.log"),
                }
        report = output / "report.json"
        result = (
            json.loads(report.read_text(encoding="utf-8"))
            if report.exists()
            else {
                "passed": False,
                "error": f"Validation exited {process.returncode} without report",
            }
        )
        result["log"] = str(output / "worker.log")
        if process.returncode:
            result["passed"] = False
        if revision_hashes(evaluator) != before:
            result.update(
                passed=False, error="Validation modified evaluator source or scenario definitions"
            )
        return result

    def execution_command(self, evaluator, output, target):
        return [
            self.config["python"],
            "-m",
            "src.evaluation.evaluate",
            "--scenarios",
            str(evaluator / "scenarios"),
            "--output",
            str(output),
            "--base-url",
            target["base_url"],
            "--model",
            target["model"],
            "--hermes-root",
            self.config["hermes_root"],
            "--max-iterations",
            str(self.config.get("max_iterations", 15)),
            "--request-interval",
            str(self.config.get("request_interval", 0)),
            "--hermes-approval",
            self.config.get("hermes_approval", "auto"),
        ]

    def execute_revision(self, evaluator, output, target, key):
        evaluator, output = Path(evaluator).resolve(), Path(output).resolve()
        validation = self.preflight(evaluator, output.parent / "preflight")
        if not validation["passed"]:
            return {"status": "invalid_evaluator", "validation": validation}
        before = revision_hashes(evaluator)
        command = self.execution_command(evaluator, output, target)
        # Provider credentials are delivered only to target execution, never to editing/review tools.
        env = {
            k: v
            for k, v in os.environ.items()
            if not any(s in k.upper() for s in ("API_KEY", "META_KEY", "COHERE_KEY", "TOKEN"))
        }
        env.update(EVAL_API_KEY=key, EVAL_MODEL=target["model"], EVAL_BASE_URL=target["base_url"])
        with (output.parent / "execution.log").open("w", encoding="utf-8") as log:
            try:
                process = subprocess.run(
                    command,
                    cwd=evaluator,
                    env=env,
                    stdout=log,
                    stderr=log,
                    timeout=self.config.get("timeout_seconds"),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except subprocess.TimeoutExpired:
                return {"status": "execution_timeout", "report": str(output / "report.json")}
        report_path = output / "report.json"
        if not report_path.exists():
            return {
                "status": "error",
                "error": f"Evaluator exited {process.returncode} without report",
            }
        report = json.loads(report_path.read_text(encoding="utf-8"))
        unchanged = revision_hashes(evaluator) == before
        return {
            "status": report.get("status", "error") if unchanged else "invalid_evaluator",
            "revision_unchanged": unchanged,
            "exit_code": process.returncode,
            "report": str(report_path),
            "cases": [
                {
                    "id": c["id"],
                    "status": c["status"],
                    "branches": {k: v["status"] for k, v in c.get("branches", {}).items()},
                }
                for c in report.get("cases", [])
            ],
        }


class ContainerAdapter(HermesAdapter):
    """benchmark contract plus native Docker execution; no Hermes runtime is imported."""

    def __init__(self, config):
        super().__init__(config)
        if config["kind"] not in {"openhands", "deerflow"}:
            raise ValueError("Unsupported native framework")
        if config.get("design_only"):
            self.config["runtime_image"] = config.get("runtime_image")
            return
        image = config["runtime_image"]
        if not image or image.startswith("-"):
            raise ValueError("Invalid runtime image")
        info = json.loads(
            subprocess.check_output(["docker", "image", "inspect", image], text=True)
        )[0]
        labels = info["Config"].get("Labels") or {}
        if labels.get("org.agent-evaluation.runtime") != config["kind"]:
            raise ValueError("Container image does not identify the selected native framework")
        if labels.get("org.opencontainers.image.revision") != config["source_revision"]:
            raise ValueError(
                "Container image source revision differs from the inspected architecture"
            )
        self.config["runtime_image"] = info["Id"]

    def inspect_tool_catalog(self, directory, evaluator):
        """Inspect an empty native instance, without inference or task execution."""
        from ..evaluation.agent_container import AgentContainer

        bridges = ("agent_worker.py", "native_agent_bridges.py")
        hashes = {name: sha256_file(ROOT / "src/evaluation" / name) for name in bridges}
        if any(
            sha256_file(Path(evaluator) / "src/evaluation" / name) != hashes[name]
            for name in bridges
        ):
            return {
                "available": False,
                "reason": "Frozen evaluator bridge differs from the current introspection bridge; inspect its source and saved tool catalogs instead.",
            }

        def reject_dispatch(payload):
            raise RuntimeError("Catalog inspection must not invoke inference or mock services")

        runtime = AgentContainer(
            self.config["runtime_image"],
            directory,
            reject_dispatch,
            reject_dispatch,
            lambda event: None,
        )
        try:
            runtime.start()
            catalog = runtime.call(
                "init",
                config={
                    "agent_kind": self.config["kind"],
                    "model": self.config["target"]["model"],
                    "max_iterations": self.config.get("max_iterations", 15),
                    "recursion_limit": self.config.get("recursion_limit", 100),
                    "memory_flush_timeout": self.config.get("memory_flush_timeout", 30),
                    "tools": [],
                },
            )
        finally:
            runtime.close()
        return {
            "available": True,
            "source_revision": self.config["source_revision"],
            "runtime_image": self.config["runtime_image"],
            "bridge_sha256": hashes,
            "scope": "Native tool schemas from an empty instance; no model inference, task turns or scenario mocks. This is interface evidence, not a capability test.",
            **catalog,
        }

    def describe(self):
        kind = self.config["kind"]
        return {
            "framework": kind,
            "execution_available": not self.config.get("design_only", False),
            "contract_scope": "Static adapter contract; source-only design does not verify a runtime image or execute references"
            if self.config.get("design_only")
            else "Pinned image verified against the inspected source revision",
            "source_revision": self.config["source_revision"],
            "runtime_image": self.config["runtime_image"],
            "native_entry_point": "SDK Conversation, CLI preset"
            if kind == "openhands"
            else "DeerFlowClient embedded, subagents and planning enabled",
            "visible_workspace": "/workspace"
            if kind == "openhands"
            else "/mnt/user-data/workspace",
            "filesystem": "Linux container, nonroot, no host mounts, no Docker socket, no direct network",
            "path_contract": "Fixture and assertion paths are relative to visible_workspace. For the same artifact, use path='brief.md' in fixtures/checks and '{WORKSPACE}/brief.md' in absolute native-tool arguments. A path='workspace/brief.md' means a real nested workspace directory, not an alias for the root; its native path is '{WORKSPACE}/workspace/brief.md'. Match prompts, references and checks to the same intended location. Do not strip directory components in the verifier to compensate for inconsistent authored paths.",
            "native_tools": sorted(reference_tool_names(kind)),
            "execution_budget": {
                "kind": "LangGraph supersteps" if kind == "deerflow" else "SDK iterations",
                "limit": self.config.get("recursion_limit", 100)
                if kind == "deerflow"
                else self.config.get("max_iterations", 15),
                "interpretation": "DeerFlow's pinned embedded-client default is 100 graph supersteps, including middleware; this is not 100 model calls. A final answer can be emitted before the graph reaches its end. Record recursion_limit termination separately from assertion failures. Native SDK/model defaults are not provider output-token caps; changing execution budgets requires an explicit recorded configuration change, not weakening completion checks.",
            },
            "task_sources": "Use scenario mock services for external APIs, search and retrieval; native tools handle local files/code. Do not require arbitrary internet access or package installation.",
            "mock_tool_route": "Scenario mocks are registered extension tools backed by a host RPC handler, not native web-provider or MCP traffic. DeerFlow uses StructuredTool without MCP metadata; its web group alone does not establish that remote-content sanitization applies. Inspect the pinned source's name/metadata-based middleware routing before claiming native retrieval defenses were exercised. Do not retag mocks or alter native policy merely to manufacture guard coverage.",
            "entry_point": "src.evaluation.evaluate --agent-kind "
            + kind
            + " --runtime-image <pinned-image>",
            "phase_implementation": "src/evaluation/eval_container_worker.py; src/evaluation/native_agent_bridges.py; src/evaluation/agent_worker.py",
            "unsupported": [
                "unverified_race_deduct cross-process file mock",
                "nonzero history truncation",
                "forced native compression",
                "symbolic-link artifacts in host assertion mirrors",
            ],
            "prefixes": "Shared sampled prefixes allow only service calls and serialized native planning state; files require branching at turn 1. Native graph/event-tree state is restored. DeerFlow memory is flushed, captured and restored before forking.",
            "observability": "Native tool events plus before/after task files and mock-service snapshots; internal native control checks are not instrumented. No Hermes guard conclusions apply.",
            "reference_validation": "Authored actions execute through native tools; DeerFlow auxiliary memory inference remains real and is captured/costed separately from task inference.",
            "memory_flush_timeout_seconds": self.config.get("memory_flush_timeout", 30)
            if kind == "deerflow"
            else None,
            "artifact_verification": "benchmark state check artifact_python runs private Python assertions on a fresh, network-disabled Docker copy of final artifact contents, using the same image and physical workspace. WORKSPACE gives that root. No model call, agent tool event or native-state mutation occurs. It does not clone permissions, memory or live services, and is not tamper-proof against intentionally hostile code inside its verifier. Use assertions for behavior, including valid and invalid examples; stdout, stderr, exit, timeout and verification time are retained separately.",
            "transport": "Exact native JSON/SSE payloads through buffered relay; host captures provider arrival times. No adapter output cap; native framework defaults are retained and visible in wire evidence.",
        }

    def validation_command(self, evaluator, output):
        target = self.config["target"]
        return [
            self.config["python"],
            str(ROOT / "src/orchestrator/container_worker.py"),
            "--validate-revision",
            str(evaluator),
            "--validation-output",
            str(output / "report.json"),
            "--agent-kind",
            self.config["kind"],
            "--runtime-image",
            self.config["runtime_image"],
            "--model",
            target["model"],
            "--base-url",
            target["base_url"],
            "--request-interval",
            str(self.config.get("request_interval", 0)),
            *(
                ["--memory-flush-timeout", str(self.config["memory_flush_timeout"])]
                if "memory_flush_timeout" in self.config
                else []
            ),
        ]

    def validation_environment(self):
        from ..evaluation.utils import load_eval_config

        environment = {
            k: v for k, v in os.environ.items() if not any(s in k.upper() for s in ("KEY", "TOKEN"))
        }
        key_name = self.config["target"]["api_key_env"]
        environment["TASK_API_KEY"] = load_eval_config(ROOT / ".env")[key_name]
        return environment

    def execution_command(self, evaluator, output, target):
        return [
            self.config["python"],
            "-m",
            "src.evaluation.evaluate",
            "--scenarios",
            str(evaluator / "scenarios"),
            "--output",
            str(output),
            "--base-url",
            target["base_url"],
            "--model",
            target["model"],
            "--agent-kind",
            self.config["kind"],
            "--runtime-image",
            self.config["runtime_image"],
            "--max-iterations",
            str(self.config.get("max_iterations", 15)),
            "--recursion-limit",
            str(self.config.get("recursion_limit", 100)),
            *(
                ["--memory-flush-timeout", str(self.config["memory_flush_timeout"])]
                if "memory_flush_timeout" in self.config
                else []
            ),
            "--request-interval",
            str(self.config.get("request_interval", 0)),
        ]


def make_adapter(config):
    if config["kind"] == "custom":
        from .custom_adapter import CustomAdapter

        return CustomAdapter(config)
    if config["kind"] == "hermes":
        return HermesAdapter(config)
    if config["kind"] in {"openhands", "deerflow"}:
        return ContainerAdapter(config)
    raise ValueError("Unknown adapter kind")
