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
        sub.add_parser("status", help="List jobs and campaigns")
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
                        )
                    }
                )
            elif action == "worker":
                result = obj.worker(args.job_id, ctx)
            elif action == "stop":
                result = obj.stop(job_id=args.job_id)
            elif action == "evidence":
                result = obj.evidence(args.campaign_id, args.path)
            else:
                result = obj.list()
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 1 if result.get("status") == "error" else 0
        except (ValueError, OSError) as exc:
            print(json.dumps({"error": str(exc)}))
            return 1

    ctx.register_cli_command("reliability", DESCRIPTION, setup, cli)
