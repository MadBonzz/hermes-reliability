"""Profile-local jobs. CLI workers own inference; web/tool handlers never own keys."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from ..evaluation.api_trace import SecretRedactor
from ..evaluation.runtime_support import write_json_atomic
from ..orchestrator.dashboard import Catalog, identity, read_json
from ..orchestrator.runner import CampaignPreempted, run
from .client import HermesClient
from .snapshot import ROOT, freeze, service_class, verify
from .source import REPOSITORIES, resolve_source

PLUGIN_ID = "hermes-reliability"
ALLOWED_REQUEST = {"source", "runtime", "scope", "revision", "execute", "limits", "prior_campaign"}


def data_root():
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir(PLUGIN_ID)


def settings(ctx=None):
    keys = (
        "execution_enabled",
        "runtimes",
        "target",
        "requests",
        "timeout_seconds",
        "dataset_discovery",
    )
    if ctx is not None:
        return {key: ctx.get_config(key, None) for key in keys}
    from hermes_cli.config import load_config_readonly

    config = load_config_readonly() or {}
    return config.get("plugins", {}).get("entries", {}).get(PLUGIN_ID, {}).get("settings", {})


def alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class Service:
    def __init__(self, root=None, options=None):
        self.root = Path(root) if root is not None else data_root()
        self.options = options if options is not None else settings()
        self.catalog = Catalog(self.root / "campaigns", bundled_root=self.root / "no-bundles")

    def job_path(self, job_id):
        if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise ValueError("Invalid job ID")
        return self.root / "jobs" / job_id

    def validate(self, request):
        if not isinstance(request, dict) or set(request) - ALLOWED_REQUEST:
            raise ValueError(
                "Unsupported campaign settings; configure runtimes in Hermes plugin settings"
            )
        source = str(request.get("source", "")).strip()
        url = urlsplit(source)
        local = Path(source).expanduser().is_dir() if source else False
        if (
            not source
            or source.startswith("-")
            or (not local and (url.scheme != "https" or not url.hostname))
        ):
            raise ValueError("Provide an existing local directory or HTTPS Git repository")
        if not local and (url.username or url.password or url.query or url.fragment):
            raise ValueError(
                "Repository URLs must not contain credentials, query strings, or fragments"
            )
        kind = request.get("runtime", "custom")
        if kind not in {*REPOSITORIES, "custom"}:
            raise ValueError("Runtime must be hermes, openhands, deerflow, or custom")
        execute = request.get("execute", False)
        if type(execute) is not bool:
            raise ValueError("execute must be a boolean")
        if execute and self.options.get("execution_enabled") is not True:
            raise ValueError(
                "Enable execution_enabled and configure a target runtime in Hermes plugin settings first"
            )
        limits = {"rounds": 3, "generations": 3, "initial_tests": 5, "active_tests": 12}
        supplied = request.get("limits", {})
        if not isinstance(supplied, dict) or set(supplied) - set(limits):
            raise ValueError("Unknown campaign budget")
        limits.update(supplied)
        for key, maximum in (
            ("rounds", 5),
            ("generations", 3),
            ("initial_tests", 5),
            ("active_tests", 12),
        ):
            if type(limits[key]) is not int or not 1 <= limits[key] <= maximum:
                raise ValueError(f"{key} must be between 1 and {maximum}")
        if limits["initial_tests"] > limits["active_tests"]:
            raise ValueError("Initial tests exceed the active test cap")
        requests = self.options.get("requests")
        timeout = self.options.get("timeout_seconds")
        requests = 100 if requests is None else requests
        timeout = 180 if timeout is None else timeout
        if type(requests) is not int or not 1 <= requests <= 500:
            raise ValueError("requests must be between 1 and 500")
        if type(timeout) is not int or not 10 <= timeout <= 600:
            raise ValueError("timeout_seconds must be between 10 and 600")
        configured = (self.options.get("runtimes") or {}).get(kind, {}) if execute else {}
        adapter = {
            "kind": kind,
            "python": sys.executable,
            "max_iterations": 15,
            "timeout_seconds": 1800,
            **configured,
        }
        adapter["kind"] = kind
        # Approval policy is never chosen by model output or silently set to auto.
        adapter["hermes_approval"] = "gate"
        target = (
            dict(self.options.get("target") or {})
            if execute
            else {"model": "design only", "base_url": ""}
        )
        if set(target) - {"base_url", "model"}:
            raise ValueError(
                "Target settings accept only base_url and model; configure secrets through Hermes"
            )
        target["api_key_env"] = "RELIABILITY_TARGET_API_KEY"
        if execute:
            target_url = urlsplit(target.get("base_url", ""))
            if (
                not target.get("model")
                or target_url.scheme not in {"http", "https"}
                or not target_url.hostname
            ):
                raise ValueError("Configure target.model and target.base_url in plugin settings")
            if (
                target_url.username
                or target_url.password
                or target_url.query
                or target_url.fragment
            ):
                raise ValueError(
                    "Target API URLs must not contain credentials, query strings, or fragments"
                )
            if kind == "custom" and not adapter.get("factory"):
                raise ValueError(
                    "Custom execution needs a trusted runtimes.custom.factory integration"
                )
            if kind in {"openhands", "deerflow"} and not adapter.get("runtime_image"):
                raise ValueError("Configure the pinned native runtime image before executing")
        revision = str(request.get("revision", "")).strip()
        if revision.startswith("-") or len(revision) > 200:
            raise ValueError("Invalid revision")
        scope = str(
            request.get("scope")
            or "Explore reliability of realistic workflows, using meaningful controls and code-grounded failure hypotheses."
        ).strip()
        if not scope or len(scope) > 12000:
            raise ValueError("Scope must be between 1 and 12000 characters")
        config = {
            "source_input": str(Path(source).expanduser().resolve()) if local else source,
            "requested_revision": revision,
            "scope": scope,
            "execute": execute,
            "adapter": adapter,
            "target": target,
            "limits": {**limits, "requests": requests},
            "orchestrator": {
                "model": "Hermes active model",
                "max_output_tokens": 8192,
                "max_request_characters": 350000,
                "timeout_seconds": timeout,
            },
            "prompt_variant": "architecture_first_v3",
            "dataset_discovery": self.options.get("dataset_discovery") or {"enabled": True},
            "artifacts_root": str(self.catalog.root),
            "regression_index": str(self.root / "regression_index.json"),
        }
        prior = request.get("prior_campaign")
        if prior:
            previous = read_json(self.catalog.resolve(prior) / "config.json", {})
            if previous.get("adapter", {}).get("kind") != kind:
                raise ValueError("Saved tests must use the same runtime")
            config["seed_scenarios"] = self.catalog.seed_selection(prior)
            config["previous_source"] = previous["source"]
        return config

    def start(self, request, *, spawn=True):
        config = self.validate(request)
        job_id = uuid.uuid4().hex
        folder = self.job_path(job_id)
        folder.mkdir(parents=True, mode=0o700)
        freeze(folder)
        write_json_atomic(folder / "config.json", config)
        job = {
            "id": job_id,
            "status": "queued",
            "created_at": time.time(),
            "execute": config["execute"],
        }
        write_json_atomic(folder / "job.json", job)
        if spawn:
            # Supported Hermes CLI entry point obtains a real PluginContext and its
            # host-owned model facade. No private manager access or token export.
            try:
                with (folder / "worker.log").open("wb") as log:
                    process = subprocess.Popen(
                        [sys.executable, "-m", "hermes_cli.main", "reliability", "worker", job_id],
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=log,
                        start_new_session=True,
                        env={
                            **{
                                k: v
                                for k, v in os.environ.items()
                                if not k.startswith("HERMES_PLUGIN_HOST_")
                            },
                            "HERMES_HOME": str(self.root.parent.parent),
                        },
                        cwd=self.root,
                    )
                write_json_atomic(folder / "process.json", {"pid": process.pid})
            except OSError:
                job.update(status="error", error="Hermes CLI worker could not start")
                write_json_atomic(folder / "job.json", job)
                raise
        return job

    def jobs(self):
        result = []
        for path in sorted((self.root / "jobs").glob("*/job.json")):
            job = read_json(path, {})
            process = read_json(path.with_name("process.json"), {})
            if (
                job.get("status") in {"queued", "running"}
                and process
                and not alive(process.get("pid"))
            ):
                job = {
                    **job,
                    "status": "interrupted",
                    "error": "Worker exited; saved evidence is retained. Start a new campaign using its saved tests.",
                }
            result.append(job)
        return result

    def list(self):
        return {"campaigns": self.catalog.cards(), "jobs": self.jobs()}

    def stop(self, campaign_id=None, job_id=None):
        if job_id:
            folder = self.job_path(job_id)
            job = read_json(folder / "job.json")
            if not job:
                raise ValueError("Unknown job")
            write_json_atomic(folder / "stop.json", {"reason": "Operator requested a stop"})
            campaign_id = job.get("campaign_id")
        if campaign_id:
            write_json_atomic(
                self.catalog.resolve(campaign_id) / "stop.json",
                {"reason": "Operator requested a stop"},
            )
        elif not job_id:
            raise ValueError("Supply a campaign or job ID")
        return {"status": "stop_requested"}

    def evidence(self, campaign_id, name, offset=0):
        root = self.catalog.resolve(campaign_id).resolve()
        candidate = root / name
        path = candidate.resolve()
        linked = any(p.is_symlink() for p in (candidate, *candidate.parents) if p != root.parent)
        if (
            not path.is_relative_to(root)
            or linked
            or path.suffix not in {".json", ".jsonl", ".md", ".txt", ".log", ".diff"}
        ):
            raise ValueError("Choose an evidence file inside this campaign")
        if type(offset) is not int or offset < 0:
            raise ValueError("Invalid offset")
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            stream.seek(offset)
            content = stream.read(24000)
            next_offset = stream.tell()
            more = bool(stream.read(1))
        return {
            "path": name,
            "text": SecretRedactor([])(content),
            "next_offset": next_offset,
            "more": more,
        }

    def worker(self, job_id, ctx):
        folder = self.job_path(job_id)
        if ROOT != (folder / "controller").resolve():
            return service_class(folder)(self.root, self.options).worker(job_id, ctx)
        verify(folder)
        # Atomic claim prevents duplicate CLI invocations from replaying a job.
        with (folder / "claimed").open("x"):
            pass
        job = read_json(folder / "job.json")
        if not job:
            raise ValueError("Unknown job")
        job.update(status="running", started_at=time.time())
        write_json_atomic(folder / "job.json", job)
        write_json_atomic(folder / "process.json", {"pid": os.getpid()})
        values = {}
        try:
            if (folder / "stop.json").exists():
                raise CampaignPreempted("Stopped before source preparation")
            config = read_json(folder / "config.json")
            if config["execute"] and ctx.get_config("execution_enabled", False) is not True:
                raise ValueError("Execution permission was revoked in plugin settings")
            resolve_source(config, folder)
            from agent.secret_scope import get_secret

            values = {}
            for name in ("HF_TOKEN", "RELIABILITY_TARGET_API_KEY"):
                value = get_secret(name)
                if value:
                    values[name] = value

            def created(path):
                job.update(campaign_id=identity(path), campaign_path=str(path))
                write_json_atomic(folder / "job.json", job)
                if (folder / "stop.json").exists():
                    write_json_atomic(path / "stop.json", {"reason": "Operator requested a stop"})

            def client(*args):
                return HermesClient(*args, llm=ctx.llm)

            result = run(
                config,
                client_factory=client,
                execute=config["execute"],
                on_created=created,
                credential_values=values,
            )
            verify(folder)
            job.update(
                status="stopped"
                if read_json(result / "campaign.json", {}).get("stop_reason")
                == "operator_preempted"
                else "complete",
                result=str(result),
            )
        except CampaignPreempted:
            job.update(status="stopped")
        except Exception as exc:
            # The host client sanitizes provider exceptions; redact any separately
            # configured target/dataset credentials from source/runtime errors too.
            job.update(
                status="error",
                error=SecretRedactor(values.values())(f"{type(exc).__name__}: {exc}"),
            )
        finally:
            job["finished_at"] = time.time()
            write_json_atomic(folder / "job.json", job)
        return job
