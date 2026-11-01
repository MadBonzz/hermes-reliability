"""Preflight scenario loading and validation; no agent or model execution."""

import hashlib
import json
import re
from pathlib import Path

from .mock_services import json_equal

DEFAULT_SUITE_NAME = "Hermes Agent Software & Distributed Systems Suite"


def external_schema_ref(value):
    """Detect non-local references without resolving or fetching schema content."""
    if isinstance(value, list):
        return any(external_schema_ref(v) for v in value)
    if isinstance(value, dict):
        ref = value.get("$ref")
        return (isinstance(ref, str) and not ref.startswith("#")) or any(
            external_schema_ref(v) for v in value.values()
        )
    return False


def load_scenarios(source, schema_path, suite_name=DEFAULT_SUITE_NAME):
    """Load a directory in filename order, or one standalone scenario JSON."""
    source = Path(source).resolve()
    if source.is_dir():
        paths = sorted(source.glob("*.json"), key=lambda p: p.name)
        if not paths:
            raise ValueError(f"No scenario JSON files found in {source}")
    elif source.is_file() and source.suffix.lower() == ".json":
        paths = [source]
    else:
        raise ValueError(f"Scenario source must be a directory or a JSON file: {source}")
    _validate_suite_name(suite_name)
    validator = _load_validator(schema_path)
    scenarios, sources = [], []
    seen = set()
    for path in paths:
        raw = path.read_bytes()
        try:
            scenario = json.loads(raw.decode("utf-8-sig"))
            _validate_scenario(scenario, validator, seen)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Invalid scenario file {path}: {exc}") from exc
        scenarios.append(scenario)
        sources.append(
            {"id": scenario["id"], "path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}
        )
    suite = {"suite_name": suite_name, "scenarios": scenarios}
    return suite, sources


def suite_sha256(suite):
    """Hash loaded content, independent of source paths and JSON formatting."""
    canonical = json.dumps(suite, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_validator(schema_path):
    """Read and compile the schema once per load/validation operation."""
    try:
        import jsonschema
    except ImportError as exc:
        raise RuntimeError(
            "Run uv sync at the repository root; for an activated Hermes environment, use uv sync --active --inexact"
        ) from exc
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    jsonschema.Draft7Validator.check_schema(schema)
    return jsonschema.Draft7Validator(schema)


def _validate_suite_name(name):
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Scenario suite must have a nonempty suite_name")


def _validate_suite_header(suite):
    if not isinstance(suite, dict):
        raise ValueError("Scenario suite must be an object")
    _validate_suite_name(suite.get("suite_name"))
    if not isinstance(suite.get("scenarios"), list) or not suite["scenarios"]:
        raise ValueError("Scenario suite must contain at least one scenario")


def _validate_scenario(scenario, validator, seen):
    """Check one definition, including IDs already encountered in this suite."""
    error = next(validator.iter_errors(scenario), None)
    if error is not None:
        location = "/".join(map(str, error.absolute_path)) or "<root>"
        raise ValueError(f"Invalid scenario at {location}: {error.message}")
    sid = scenario["id"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", sid) or sid in seen:
        raise ValueError(f"Unsafe or duplicate scenario ID: {sid!r}")
    seen.add(sid)
    turns = scenario["turns"]
    if [t["turn_number"] for t in turns] != list(range(1, len(turns) + 1)):
        raise ValueError(f"{sid}: turn numbers must be consecutive starting at 1")
    names = [m["name"] for m in scenario.get("mock_tools", [])]
    if len(names) != len(set(names)):
        raise ValueError(f"{sid}: duplicate mock names")
    services = {}
    import jsonschema

    for mock in scenario.get("mock_tools", []):
        behavior = mock["behavior"]
        parameters = mock["parameters"]
        try:
            jsonschema.Draft7Validator.check_schema(parameters)
        except jsonschema.SchemaError as exc:
            raise ValueError(f"{sid}: invalid tool parameter schema: {exc.message}") from exc
        if parameters.get("type") != "object":
            raise ValueError(f"{sid}: tool arguments must be an object")
        if behavior["type"] != "stateful_service":
            continue
        service = behavior["service"]
        services.setdefault(service, None)
        if "initial_state" in behavior:
            initial = behavior["initial_state"]
            if services[service] is not None and not json_equal(services[service], initial):
                raise ValueError(f"{sid}: conflicting initial state for {service}")
            services[service] = initial
        field = behavior.get("idempotency_field") or behavior.get("lookup_arg")
        if field and (
            field not in parameters.get("required", [])
            or parameters.get("properties", {}).get(field, {}).get("type") != "string"
        ):
            raise ValueError(f"{sid}: {field} must be a required string argument")
        if behavior["operation"] == "compare_and_swap":
            if (
                "expected_version" not in parameters.get("required", [])
                or parameters.get("properties", {}).get("expected_version", {}).get("type")
                != "integer"
            ):
                raise ValueError(f"{sid}: compare_and_swap requires expected_version")
        for field_name in ("return_payload", "failure_response", "rejection"):
            if field_name in behavior and not isinstance(behavior[field_name], dict):
                raise ValueError(f"{sid}: service {field_name} must be an object")
    for mock in scenario.get("mock_tools", []):
        behavior = mock["behavior"]
        if behavior.get("operation") == "compare_and_swap":
            initial = services.get(behavior["service"]) or {}
            if type(initial.get("version")) is not int:
                raise ValueError(f"{sid}: compare_and_swap needs an initial integer version")
    paths = [
        m["behavior"]["target_file"]
        for m in scenario.get("mock_tools", [])
        if "target_file" in m["behavior"]
    ]
    for turn in turns:
        reference = turn.get("reference")
        if reference:
            allowed_reference = {
                "read_file",
                "write_file",
                "patch",
                "search_files",
                "terminal",
                *names,
            }
            for action in reference["actions"]:
                if action["tool"] not in allowed_reference:
                    raise ValueError(f"{sid}: unknown reference tool {action['tool']}")
        for group in ("state_checks", "pre_turn_perturbations", "post_turn_cleanup"):
            paths += [c["path"] for c in turn.get(group, []) if "path" in c]
            for entry in turn.get(group, []):
                if entry["type"] == "set_mock_state" and entry["service"] not in services:
                    raise ValueError(f"{sid}: unknown mock service {entry['service']}")
    for path in paths:
        clean = path.replace("{WORKSPACE}/", "").replace("{WORKSPACE}\\", "").replace("\\", "/")
        if not clean or clean.startswith("/") or ":" in clean or ".." in clean.split("/"):
            raise ValueError(f"{sid}: expected workspace-relative path, got {path!r}")


def validate_suite(suite, schema_path):
    """Validate an in-memory collection against the standalone scenario schema."""
    _validate_suite_header(suite)
    validator = _load_validator(schema_path)
    seen = set()
    for scenario in suite["scenarios"]:
        _validate_scenario(scenario, validator, seen)
