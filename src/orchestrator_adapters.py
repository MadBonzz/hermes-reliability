"""Execution adapters for the separate exploratory orchestrator.

Adapters are trusted local configuration, never model-authored executable code.
"""

import json
import os
import subprocess
from pathlib import Path

import jsonschema

from src.scenario_validation import external_schema_ref, validate_suite

ROOT = Path(__file__).resolve().parent.parent


def reject_external_refs(value):
    if external_schema_ref(value):
        raise ValueError("External JSON Schema references are unsupported")


class Adapter:
    """Generic adapter: schema plus a trusted command producing a result manifest."""

    def __init__(self, config):
        self.config = config
        self.schema_path = Path(config["schema"]).resolve()
        self.schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        reject_external_refs(self.schema)
        jsonschema.Draft7Validator.check_schema(self.schema)

    def validate(self, scenarios):
        seen = set()
        for scenario in scenarios:
            reject_external_refs(scenario)
            jsonschema.validate(scenario, self.schema)
            sid = scenario["id"]
            if (
                not sid
                or any(
                    c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
                    for c in sid
                )
                or sid in seen
            ):
                raise ValueError("Unsafe or duplicate scenario id")
            seen.add(sid)

    def command(self, request_path, result_path, target):
        return [
            *self.config["command"],
            "--orchestrator-request",
            str(request_path),
            "--orchestrator-result",
            str(result_path),
        ]

    def run(self, directory, target, key):
        request = directory / "execution_request.json"
        result = directory / "execution_result.json"
        payload = {
            "scenarios": str(directory / "scenarios"),
            "target": target,
            "output_directory": str(directory / "execution"),
        }
        request.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        env = os.environ.copy()
        env["EVAL_API_KEY"] = key
        env["EVAL_MODEL"] = target["model"]
        env["EVAL_BASE_URL"] = target["base_url"]
        with (directory / "execution.log").open("wb") as log:
            process = subprocess.run(
                self.command(request, result, target),
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=self.config.get("timeout_seconds", 3600),
                check=False,
            )
        if process.returncode:
            raise RuntimeError(f"Adapter exited {process.returncode}; inspect execution.log")
        manifest = json.loads(result.read_text(encoding="utf-8"))
        if manifest.get("status") != "complete":
            raise RuntimeError("Adapter did not report complete evidence")
        evidence = []
        for name in manifest["evidence_files"]:
            path = (directory / name).resolve()
            if not path.is_relative_to(directory.resolve()) or not path.is_file():
                raise ValueError("Adapter evidence must be files inside the cycle directory")
            evidence.append(path)
        if not evidence:
            raise ValueError("Adapter returned no evidence")
        return evidence


class HermesAdapter(Adapter):
    def __init__(self, config):
        super().__init__({**config, "schema": str(ROOT / "scenario.schema.json")})

    def validate(self, scenarios):
        # The Hermes validator already checks schema, IDs and semantic contracts.
        # Keep the network-reference guard without repeating generic validation.
        for scenario in scenarios:
            reject_external_refs(scenario)
        validate_suite({"suite_name": "orchestrator", "scenarios": scenarios}, self.schema_path)

    def command(self, request_path, result_path, target):
        return [
            self.config["python"],
            "-m",
            "src.orchestrator_hermes_worker",
            "--orchestrator-request",
            str(request_path),
            "--orchestrator-result",
            str(result_path),
            "--hermes-root",
            self.config["hermes_root"],
            "--max-iterations",
            str(self.config.get("max_iterations", 15)),
            "--request-interval",
            str(self.config.get("request_interval", 0)),
        ]


def make_adapter(config):
    if config["kind"] == "hermes":
        return HermesAdapter(config)
    if config["kind"] == "command":
        return Adapter(config)
    raise ValueError("Unknown adapter kind")
