"""Tools, slash command, CLI and an approval hook on supported Hermes surfaces."""

import json

DESCRIPTION = "Run source-grounded reliability campaigns: discover datasets, generate scenarios, independently review, optionally execute, and retain evidence."
SCHEMAS = {
    "reliability_start": {
        "description": DESCRIPTION,
        "properties": {
            "source": {
                "type": "string",
                "description": "HTTPS Git URL or absolute local source directory",
            },
            "runtime": {
                "type": "string",
                "enum": ["custom", "hermes", "openhands", "deerflow"],
                "default": "custom",
            },
            "scope": {"type": "string"},
            "revision": {"type": "string"},
            "mode": {"type": "string", "enum": ["explore", "regression"], "default": "explore"},
            "benchmark_id": {
                "type": "string",
                "description": "Frozen benchmark ID from reliability_freeze/history",
            },
            "baseline_run_id": {
                "type": "string",
                "description": "Audited trusted run to compare against",
            },
            "execute": {
                "type": "boolean",
                "default": False,
                "description": "Explicitly authorize generated evaluator and target execution using operator-configured runtimes",
            },
            "prior_campaign": {
                "type": "string",
                "description": "Reuse this saved campaign's generated tests",
            },
        },
        "required": ["source"],
    },
    "reliability_status": {
        "description": "List jobs/campaigns, or read campaign evidence and findings.",
        "properties": {"campaign_id": {"type": "string"}},
    },
    "reliability_stop": {
        "description": "Request a persistent campaign stop at the next safe boundary.",
        "properties": {"campaign_id": {"type": "string"}, "job_id": {"type": "string"}},
    },
    "reliability_evidence": {
        "description": "Read a bounded page of a campaign's saved evidence file.",
        "properties": {
            "campaign_id": {"type": "string"},
            "path": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0},
        },
        "required": ["campaign_id", "path"],
    },
    "reliability_history": {
        "description": "Retrieve this profile's frozen benchmarks and execution history. Resolve memory references here before regression testing. Refresh imports finished campaigns from before history indexing was installed.",
        "properties": {"refresh": {"type": "boolean", "default": False}},
    },
    "reliability_freeze": {
        "description": "Freeze the trusted evaluator and scenarios of a finished audited campaign as a versioned regression benchmark. Does not run the target. Optional remember returns a compact note for the native memory tool; it does not write memory itself.",
        "properties": {
            "campaign_id": {"type": "string"},
            "round_number": {"type": "integer", "minimum": 1},
            "remember": {"type": "boolean", "default": False},
        },
        "required": ["campaign_id"],
    },
    "reliability_compare": {
        "description": "Compare two saved runs only when benchmark, runtime settings and audited evidence are compatible. Otherwise report invalid or incomparable, never a regression verdict.",
        "properties": {"baseline_run_id": {"type": "string"}, "current_run_id": {"type": "string"}},
        "required": ["baseline_run_id", "current_run_id"],
    },
    "reliability_memory": {
        "description": "Prepare a verified baseline reference for Hermes native memory. Call the native memory tool with the returned arguments when the user asks to remember it; this operation alone does not save memory.",
        "properties": {"benchmark_id": {"type": "string"}, "baseline_run_id": {"type": "string"}},
        "required": ["benchmark_id", "baseline_run_id"],
    },
}


def register(ctx):
    def service():
        from .service import Service, settings

        return Service(options=settings(ctx))

    def handler_for(name):
        def handler(args, **kwargs):
            try:
                obj = service()
                if name == "reliability_start":
                    result = obj.start(args)
                elif name == "reliability_status":
                    result = (
                        obj.catalog.detail(args["campaign_id"])
                        if args.get("campaign_id")
                        else obj.list()
                    )
                elif name == "reliability_stop":
                    result = obj.stop(**args)
                elif name == "reliability_history":
                    result = obj.history.list(refresh=args.get("refresh", False))
                elif name == "reliability_freeze":
                    result = obj.promote(**args)
                elif name == "reliability_compare":
                    result = obj.history.comparison(args["baseline_run_id"], args["current_run_id"])
                elif name == "reliability_memory":
                    result = obj.history.memory_note(args["benchmark_id"], args["baseline_run_id"])
                else:
                    result = obj.evidence(args["campaign_id"], args["path"], args.get("offset", 0))
                return json.dumps(result, ensure_ascii=False)
            except (ValueError, OSError, KeyError) as exc:
                return json.dumps({"error": str(exc)})

        return handler

    for name, item in SCHEMAS.items():
        ctx.register_tool(
            name=name,
            toolset="reliability",
            schema={
                "name": name,
                "description": item["description"],
                "parameters": {
                    "type": "object",
                    "properties": item["properties"],
                    "required": item.get("required", []),
                    "additionalProperties": False,
                },
            },
            handler=handler_for(name),
        )

    def approval(tool_name, args, **kwargs):
        if tool_name == "reliability_start" and args.get("execute") is True:
            return {
                "action": "approve",
                "message": "Run the configured target and generated evaluator code? This uses the target model account and may execute code on this machine.",
                "rule_key": "hermes-reliability.execute",
            }
        return None

    ctx.register_hook("pre_tool_call", approval)

    ctx.register_system_prompt_section(
        "reliability.baselines",
        "Reliability Lab stores exact benchmarks and evidence in profile-local plugin storage. "
        "Use reliability_history to resolve benchmark/baseline IDs recalled from memory; stale or "
        "foreign-profile IDs must not be guessed. For a regression request, use mode=regression "
        "with those IDs and explicit execution authorization. This preserves scenarios and assertions. "
        "When the user asks to remember a baseline, call reliability_memory (or reliability_freeze "
        "with remember=true), then call the native memory tool with its memory_tool_arguments. "
        "Only claim it was saved when that native tool confirms an applied write. If staged, full "
        "or disabled, explain the result; never edit MEMORY.md directly or remove unrelated memories. "
        "Stored references do not authorize execution. Do not put test answers or raw logs in memory.",
        position="after_memory",
        max_chars=1500,
    )

    def command(raw="", **kwargs):
        # Natural-language initiation goes through reliability_start and the
        # ordinary Hermes tool pipeline, including its approval hook.
        if raw.strip() in {"", "help"}:
            return "Reliability Lab: ask Hermes to test a repository using reliability_start. /reliability status lists campaigns. Dashboard: Reliability tab. CLI: hermes reliability --help."
        if raw.strip() == "status":
            return handler_for("reliability_status")({})
        return "Use /reliability status, or ask Hermes to start/stop a campaign."

    ctx.register_command(
        "reliability",
        command,
        description="Reliability Lab campaigns and evidence",
        args_hint="status | help",
    )

    def setup(parser):
        sub = parser.add_subparsers(dest="reliability_action", required=True)
        start = sub.add_parser("start", help="Launch a persistent campaign")
        start.add_argument("source")
        start.add_argument(
            "--runtime", choices=["custom", "hermes", "openhands", "deerflow"], default="custom"
        )
        start.add_argument("--scope", default="")
        start.add_argument("--revision", default="")
        start.add_argument(
            "--execute",
            action="store_true",
            help="Authorize target and generated evaluator execution (requires plugin configuration)",
        )
        start.add_argument("--prior-campaign")
        start.add_argument("--mode", choices=["explore", "regression"], default="explore")
        start.add_argument("--benchmark-id")
        start.add_argument("--baseline-run-id")
        sub.add_parser("status", help="List jobs and campaigns")
        history = sub.add_parser("history", help="List benchmark versions and run history")
        history.add_argument("--refresh", action="store_true")
        freeze = sub.add_parser("freeze", help="Freeze a finished trusted execution as a benchmark")
        freeze.add_argument("campaign_id")
        freeze.add_argument("--round-number", type=int)
        freeze.add_argument(
            "--remember", action="store_true", help="Return a note for Hermes's native memory tool"
        )
        compare = sub.add_parser("compare", help="Compare compatible audited executions")
        compare.add_argument("baseline_run_id")
        compare.add_argument("current_run_id")
        memory = sub.add_parser(
            "memory-note", help="Prepare native-memory arguments; does not write memory"
        )
        memory.add_argument("benchmark_id")
        memory.add_argument("baseline_run_id")
        stop = sub.add_parser("stop", help="Stop at the next safe boundary")
        stop.add_argument("job_id")
        evidence = sub.add_parser("evidence", help="Read a campaign evidence file")
        evidence.add_argument("campaign_id")
        evidence.add_argument("path")
        worker = sub.add_parser("worker", help="Run a previously queued job (used by launchers)")
        worker.add_argument("job_id")

    def cli(args):
        obj = service()
        try:
            action = args.reliability_action
            if action == "start":
                result = obj.start(
                    {
                        name: getattr(args, name)
                        for name in (
                            "source",
                            "runtime",
                            "scope",
                            "revision",
                            "execute",
                            "prior_campaign",
                            "mode",
                            "benchmark_id",
                            "baseline_run_id",
                        )
                    }
                )
            elif action == "worker":
                result = obj.worker(args.job_id, ctx)
            elif action == "stop":
                result = obj.stop(job_id=args.job_id)
            elif action == "evidence":
                result = obj.evidence(args.campaign_id, args.path)
            elif action == "history":
                result = obj.history.list(refresh=args.refresh)
            elif action == "freeze":
                result = obj.promote(args.campaign_id, args.round_number, args.remember)
            elif action == "compare":
                result = obj.history.comparison(args.baseline_run_id, args.current_run_id)
            elif action == "memory-note":
                result = obj.history.memory_note(args.benchmark_id, args.baseline_run_id)
            else:
                result = obj.list()
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 1 if result.get("status") == "error" else 0
        except (ValueError, OSError) as exc:
            print(json.dumps({"error": str(exc)}))
            return 1

    ctx.register_cli_command("reliability", DESCRIPTION, setup, cli)
