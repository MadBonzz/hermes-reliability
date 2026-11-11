"""Check response bookkeeping, not the truth of a model's judgments."""

from pathlib import Path

from ..evaluation.benchmark import condition_spec
from .prompts import MISSING_REGRESSION_FEEDBACK


def check_catalog(cases):
    """Stable assertion references tied to the currently authored contracts."""
    catalog = {}
    for case in cases:
        rows = {}
        conditions = ("baseline", "treatment") if case.get("experiment") else ("ordinary",)
        for condition in conditions:
            for turn in condition_spec(case, condition)["turns"]:
                for group in ("state", "process", "output"):
                    for index, assertion in enumerate(turn.get(group + "_checks", [])):
                        ref = f"{condition}:{turn['turn_number']}:{group}:{index}"
                        rows[ref] = assertion
        catalog[case["id"]] = rows
    return catalog


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} needs a nonempty explanation")


def _rows(value, ids, label):
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError(f"{label} must be a list of objects")
    observed = [row.get("id") for row in value]
    if any(not isinstance(identity, str) for identity in observed):
        raise ValueError(f"{label} needs string scenario IDs")
    if len(observed) != len(ids) or set(observed) != set(ids):
        raise ValueError(f"{label} must cover each active scenario exactly once")
    return value


def validate_design(plan, cases):
    catalog = check_catalog(cases)
    _rows(plan.get("rationale"), catalog, "rationale")
    for row in _rows(plan.get("design_evidence"), catalog, "design_evidence"):
        for name in (
            "invariant",
            "exposure",
            "architecture_route",
            "complexity_reason",
            "control_reason",
        ):
            _text(row.get(name), name)
        groups = row.get("contract_checks")
        if not isinstance(groups, list):
            raise ValueError("contract_checks must list requirement-to-assertion mappings")
        covered = []
        for group in groups:
            if not isinstance(group, dict):
                raise ValueError("Each contract_checks entry must be an object")
            refs = group.get("check_ids")
            if not isinstance(refs, list) or not refs or not all(isinstance(x, str) for x in refs):
                raise ValueError("check_ids must contain assertion IDs from check_catalog")
            covered.extend(refs)
            for name in ("visible_requirement", "valid_alternative", "invalid_example"):
                _text(group.get(name), name)
        if len(covered) != len(set(covered)) or set(covered) != set(catalog[row["id"]]):
            raise ValueError(
                f"{row['id']}: map every current assertion exactly once; no invented IDs"
            )
    # The above establishes coverage and referential integrity, not whether the
    # requirement is actually visible or the examples are semantically correct.


def validate_assessments(review, cases):
    rows = _rows(review.get("assessments"), {c["id"] for c in cases}, "assessments")
    for row in rows:
        if row.get("verdict") not in {"accept", "revise", "unresolved"}:
            raise ValueError("Assessment verdict must be accept, revise or unresolved")
        _text(row.get("reason"), "assessment reason")
    if review.get("accepted") and any(row["verdict"] != "accept" for row in rows):
        raise ValueError("Acceptance conflicts with per-scenario assessments")


def validate_stopping(result):
    if type(result.get("converged")) is not bool:
        raise ValueError("converged must be a boolean")
    for field in ("unresolved", "proposed_changes"):
        if not isinstance(result.get(field), list):
            raise ValueError(f"{field} must be a list")
    _text(result.get("convergence_reason"), "convergence_reason")
    if result.get("converged"):
        if result.get("unresolved") or result.get("proposed_changes"):
            raise ValueError("Convergence conflicts with unresolved issues or proposed revisions")
    elif not result["proposed_changes"]:
        _text(result.get("unchanged_suite_reason"), "unchanged_suite_reason")
    # Repeating an unchanged suite may be justified by sampling variance. Require
    # the reason to be explicit, without pretending to validate that judgment.


def validate_analysis(result, evidence, scenario_ids, *, strict_owners=False, executions=None):
    invalid = (
        [
            lane
            for lane, execution in executions.items()
            if execution.get("status") != "complete"
            or execution.get("evidence_audit", {}).get("passed") is False
        ]
        if executions is not None
        else []
    )
    if result.get("converged") is True and invalid:
        raise ValueError(
            f"Cannot establish convergence with incomplete or invalid executions: {invalid}. "
            "Retain the validity issue and propose its repair without counting it as a target failure."
        )
    rows = result["scenarios"]
    if {r["id"] for r in rows} != set(scenario_ids) or len(rows) != len(scenario_ids):
        raise ValueError("Analysis must cover every generated scenario exactly once")
    for row in rows:
        if row["property_outcome"] not in {
            "observed_compliance",
            "observed_violation",
            "not_exercised",
            "uncertain",
        }:
            raise ValueError("Invalid property outcome")
        if not row["evidence_refs"] or not set(row["evidence_refs"]) <= evidence.seen:
            raise ValueError("Uninspected or missing analysis citations")
        for finding in row["findings"]:
            if not finding["evidence_refs"] or not set(finding["evidence_refs"]) <= evidence.seen:
                raise ValueError("Uninspected finding citation")
            if finding["certainty"] not in {"observed", "inferred", "unresolved"}:
                raise ValueError("Invalid attribution certainty")
            owners = {
                "model",
                "serving_protocol",
                "hermes_runtime",
                "agent_runtime",
                "mock_service",
                "harness",
                "verifier",
                "infrastructure",
                "unresolved",
            }
            if strict_owners and (
                not isinstance(finding.get("contributors"), list)
                or not finding["contributors"]
                or not set(finding["contributors"]) <= owners
            ):
                raise ValueError(
                    f"Use explicit contributor labels from {sorted(owners)}. Generic runtime is ambiguous."
                )
    if not isinstance(result["unresolved"], list):
        raise ValueError("Missing unresolved evidence gaps")
    if strict_owners and invalid:
        retained = {
            gap.get("lane")
            for gap in result["unresolved"]
            if isinstance(gap, dict)
            and gap.get("kind") == "execution_validity"
            and isinstance(gap.get("description"), str)
            and gap["description"].strip()
        }
        missing = set(invalid) - retained
        if missing:
            raise ValueError(
                f"Retain incomplete/invalid lane evidence for {sorted(missing)} in unresolved, "
                "using {kind: execution_validity, lane: <lane>, description: <observed issue>}. "
                "A non-convergence decision alone does not document why execution is incomplete."
            )


def validate_review(review, role, cases, oracle_validation=None):
    if type(review.get("accepted")) is not bool or not isinstance(review.get("issues"), list):
        raise ValueError("Reviewer must return accepted boolean and issues list")
    ids = {s["id"] for s in cases}
    seen = set()
    for issue in review["issues"]:
        if (
            not isinstance(issue, dict)
            or not isinstance(issue.get("description"), str)
            or not issue["description"].strip()
            or not isinstance(issue.get("id"), str)
            or not issue["id"].startswith(role + "-")
        ):
            raise ValueError("Review issues need stable role-prefixed IDs and descriptions")
        scenario_ids = issue.get("scenario_ids", [])
        if not isinstance(scenario_ids, list) or any(
            not isinstance(identity, str) for identity in scenario_ids
        ):
            raise ValueError("Review issue scenario_ids must be a list of string scenario IDs")
        if issue["id"] in seen or not set(scenario_ids) <= ids:
            raise ValueError("Duplicate issue ID or unknown scenario")
        seen.add(issue["id"])
    if review["accepted"] and review["issues"]:
        raise ValueError("An accepting reviewer must resolve its issues")
    if (
        role == "verification_attribution"
        and review["accepted"]
        and oracle_validation
        and oracle_validation["checker_code_changed"]
        and not oracle_validation["passing_regression_execution_observed"]
    ):
        raise ValueError(MISSING_REGRESSION_FEEDBACK)


def validate_proposal(plan, evidence, source):
    if plan.get("ready") is not True or not isinstance(plan.get("rationale"), list):
        raise ValueError("Return ready=true and a rationale list when editing is complete")
    source_refs = {
        ref
        for ref in evidence.seen
        if Path(evidence.items[ref.split(":")[0]]["path"]).is_relative_to(source)
    }
    for row in plan["rationale"]:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
            raise ValueError("Each rationale entry must be an object with a nonempty string id")
        refs = row.get("architecture_refs", [])
        if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
            raise ValueError(
                "architecture_refs must be a list of exact citation strings returned by source reads, "
                "not citation objects or file paths"
            )
        if not refs or not set(refs) <= evidence.seen or not set(refs).intersection(source_refs):
            raise ValueError(
                f"Use exact inspected source refs. Available refs: {sorted(source_refs)}"
            )


def validate_dataset_grounding(plan, manifest):
    if manifest.get("status") == "disabled":
        return
    sources = {s["id"]: s for s in manifest["sources"]}
    for row in plan["rationale"]:
        grounding = row.get("workload_source")
        if not isinstance(grounding, dict):
            raise ValueError(
                "Every rationale needs workload_source with dataset_id, basis and adaptation, or architecture_reason"
            )
        if grounding.get("architecture_reason"):
            _text(grounding["architecture_reason"], "architecture_reason")
            continue
        source = sources.get(grounding.get("dataset_id"))
        if source is None:
            raise ValueError("workload_source.dataset_id must identify an inspected dataset")
        _text(grounding.get("adaptation"), "workload adaptation")
        if grounding.get("basis") == "sampled_user":
            index = grounding.get("sample_index")
            if type(index) is not int or not 0 <= index < len(source.get("samples", [])):
                raise ValueError("sampled_user needs a valid sample_index from that dataset")
        elif grounding.get("basis") not in {"dataset_card", "metadata"}:
            raise ValueError("workload_source.basis must be sampled_user, dataset_card or metadata")
        elif grounding["basis"] == "dataset_card" and not source.get("card_excerpt"):
            raise ValueError("Cannot cite a dataset card that was not fetched")


def validate_version_plan(plan, changed_files):
    """Version mode: every test names its change target, property layer and predictions."""
    from .prompts import PREDICTED_OUTCOMES, PROPERTY_LAYERS

    rows = plan.get("rationale")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Version mode needs a rationale row for every active test")
    changed = set(changed_files)
    targeted = 0
    for row in rows:
        label = row.get("id", "<missing id>")
        target = row.get("change_target")
        if target != "control" and target not in changed:
            raise ValueError(
                f"{label}: change_target must be a path from version_change.changed_files "
                "or 'control'"
            )
        targeted += target != "control"
        if row.get("property_layer") not in PROPERTY_LAYERS:
            raise ValueError(f"{label}: property_layer must be one of {list(PROPERTY_LAYERS)}")
        prediction = row.get("predicted_outcome")
        if not isinstance(prediction, dict) or any(
            prediction.get(side) not in PREDICTED_OUTCOMES for side in ("base", "target")
        ):
            raise ValueError(
                f"{label}: predicted_outcome needs base and target, each one of "
                f"{list(PREDICTED_OUTCOMES)}"
            )
        _text(prediction.get("reason"), f"{label} predicted_outcome reason")
    if not targeted:
        raise ValueError("At least one test must target a changed file, not only controls")
