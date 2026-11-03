"""Validate all authored prefixes with installed Hermes tools, without inference."""

import argparse
import copy
import logging
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.evaluation import hermes_runner, utils
from src.evaluation.runtime_support import write_json_atomic
from src.evaluation.scenario_validation import load_scenarios


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario-id", action="append")
    parser.add_argument("--scenarios", type=Path)
    parser.add_argument("--schema", type=Path)
    args = parser.parse_args()
    root = utils.PROJECT_ROOT
    target = root / "eval_harness/reference_validation" / uuid.uuid4().hex
    target.mkdir(parents=True)
    reports = []
    suite, _ = load_scenarios(
        args.scenarios or root / "scenarios", args.schema or root / "scenario.schema.json"
    )
    with utils.hermes_run_environment(target / "home"):
        utils.load_hermes(utils.load_eval_config(root / ".env").get("HERMES_AGENT_ROOT"))

        def agent(*args):
            tools = utils.model_tools.get_tool_definitions(
                enabled_toolsets=["terminal", "file"], quiet_mode=True
            )
            return SimpleNamespace(
                tools=tools, valid_tool_names={t["function"]["name"] for t in tools}
            )

        with patch.object(hermes_runner, "instantiate_hermes_agent", agent):
            for source in suite["scenarios"]:
                if args.scenario_id and source["id"] not in args.scenario_id:
                    continue
                if len(source["turns"]) == 1:
                    continue
                spec = copy.deepcopy(source)
                spec["turns"] = spec["turns"][:-1]
                result = hermes_runner.run_single_scenario(
                    spec,
                    "http://127.0.0.1:9/v1",
                    "reference-only",
                    "unused",
                    target / "workspaces",
                    run_id=target.name,
                    trace_root=target / "traces",
                    guard_file_paths=True,
                    observe_native_controls=True,
                    reference_until=len(spec["turns"]),
                )
                reports.append(result)
                write_json_atomic(target / "report.json", reports)
                print(spec["id"], result["status"], result["harness_errors"], flush=True)
        logging.shutdown()
    print("Reference validation:", target / "report.json")
    return int(not reports or any(r["status"] != "passed" for r in reports))


if __name__ == "__main__":
    sys.exit(main())
