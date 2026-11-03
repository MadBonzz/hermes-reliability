"""benchmark contract validation and controlled-condition construction."""

import copy
import json
import re

from src.evaluation.scenario_validation import validate_suite


def condition_spec(spec, condition):
    result = copy.deepcopy(spec)
    experiment = result.pop("experiment", None)
    if experiment and condition == "baseline":
        result["complexity"].update(copy.deepcopy(experiment.get("baseline_complexity", {})))
        for patch in experiment.get("baseline_turns", []):
            number = patch["turn_number"]
            result["turns"][number - 1].update(copy.deepcopy(patch))
        overrides = experiment.get("baseline_behaviors", {})
        for tool in result.get("mock_tools", []):
            tool["behavior"].update(overrides.get(tool["name"], {}))
    return result


def validate_benchmark(suite, schema, *, require_portable_prefix=True, runtime_kind="hermes"):
    validate_suite(suite, schema, runtime_kind=runtime_kind)
    for spec in suite["scenarios"]:
        complexity = spec["complexity"]
        if complexity["turns"] != len(spec["turns"]):
            raise ValueError(f"Incorrect turn dimension: {spec['id']}")
        experiment = spec.get("experiment")
        if not experiment:
            continue
        branch = experiment["branch_turn"]
        if not 1 <= branch <= len(spec["turns"]):
            raise ValueError("Branch turn outside scenario")
        known_tools = {tool["name"] for tool in spec.get("mock_tools", [])}
        if set(experiment.get("baseline_behaviors", {})) - known_tools:
            raise ValueError("Baseline overrides an unknown tool")
        patched_turns = [p["turn_number"] for p in experiment.get("baseline_turns", [])]
        if len(patched_turns) != len(set(patched_turns)):
            raise ValueError("Duplicate baseline turn override")
        if any(type(n) is not int or not branch <= n <= len(spec["turns"]) for n in patched_turns):
            raise ValueError("Baseline turn override is outside the branch suffix")
        base = condition_spec(spec, "baseline")
        if base == condition_spec(spec, "treatment"):
            raise ValueError("Controlled pair must define a distinct intervention")
        validate_suite(
            {"suite_name": "baseline", "scenarios": [base]}, schema, runtime_kind=runtime_kind
        )
        for original, variant in zip(spec["turns"], base["turns"], strict=True):
            if original["prompt"] != variant["prompt"]:
                raise ValueError("Controlled conditions must have identical user messages")
        if not require_portable_prefix:
            # Authoring can describe an extended adapter. Execution validators
            # retain their capability boundary until that adapter is implemented.
            continue
        if branch > 1 and experiment.get("baseline_behaviors"):
            raise ValueError("Behavior overrides require an empty prefix")
        for turn in spec["turns"][: branch - 1]:
            if turn.get("pre_turn_perturbations") or turn.get("post_turn_cleanup"):
                raise ValueError("Shared service prefixes must not mutate files or context")
            if "{WORKSPACE}" in turn["prompt"]:
                raise ValueError("Shared service prefixes must be independent of workspace paths")


def validate_prefix(report, spec, *, runtime_kind="hermes"):
    """Accept portable service-only prefixes, reject uncheckpointed runtime state."""
    if report["status"] != "prefix_ready":
        raise ValueError("The shared prefix did not pass its prerequisite checks")
    allowed = {
        t["name"] for t in spec.get("mock_tools", []) if t["behavior"]["type"] == "stateful_service"
    }
    native_directories = {"."}
    if runtime_kind == "deerflow":
        # These directories are initialized by LocalSandboxProvider. Todo state
        # lives in the serialized graph checkpoint, not an untracked executor.
        native_directories |= {"workspace", "uploads", "outputs"}
        allowed.add("write_todos")
    elif runtime_kind == "openhands":
        allowed.add("finish")
    elif runtime_kind != "hermes":
        raise ValueError("Unsupported native prefix checkpoint")
    if runtime_kind != "hermes":
        continuation = report.get("continuation", {})
        native = continuation.get("native_state", {})
        if native.get("runtime") != runtime_kind or native.get("history") != continuation.get(
            "history"
        ):
            raise ValueError("Native shared prefix lacks a matching runtime checkpoint and history")
    records = report.get("turns", []) + report.get("reference_prefix", [])
    for turn in records:
        if not turn["gate_passed"]:
            raise ValueError("Failed prerequisite cannot become a sampled shared prefix")
        for call in turn.get("execution_events", []):
            unavailable_memory = False
            if call["name"] == "memory" and isinstance(call.get("result"), str):
                try:
                    payload, _ = json.JSONDecoder().raw_decode(call["result"])
                    unavailable_memory = payload == {
                        "error": "Memory is not available. It may be disabled in config or this environment.",
                        "success": False,
                    }
                except ValueError:
                    pass
            if (call["name"] not in allowed and not unavailable_memory) or not call.get(
                "completed", True
            ):
                raise ValueError("Shared prefix used a nonportable tool or incomplete execution")
        files = turn.get("workspace_after_agent", {})
        if any(
            v.get("kind") != "directory"
            for v in turn.get("auxiliary_workspace_after_agent", {}).values()
        ):
            raise ValueError(
                "Shared prefix contains auxiliary filesystem state requiring checkpoint support"
            )
        if set(files) - native_directories or any(
            v.get("kind") != "directory" for v in files.values()
        ):
            raise ValueError(
                "Shared prefix contains filesystem state requiring native checkpoint support"
            )
    if records:
        history = report["continuation"]["history"]
        if not history or history[-1].get("role") != "assistant":
            raise ValueError("Shared prefix must end at a completed assistant boundary")


def visible_input_validator(spec, private_root):
    """Prevent known evaluator identity leaking in the next model-visible request."""
    forbidden = (spec["id"], "[EVAL run=", "eval_harness", str(private_root).replace("\\", "/"))

    def validate(body):
        visible = json.dumps(
            {"messages": body.get("messages"), "tools": body.get("tools")}, ensure_ascii=False
        )
        visible = visible.replace("\\\\", "/").replace("\\", "/")
        if any(value.casefold() in visible.casefold() for value in forbidden):
            raise ValueError("Model-visible input contains private evaluation identity")
        if re.search(r"scen_\d+_", visible):
            raise ValueError("Model-visible input contains a scenario directory identifier")

    return validate
