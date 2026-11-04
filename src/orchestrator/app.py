"""Local campaign dashboard. Run with python -m src.orchestrator.app."""

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from src.evaluation.api_trace import SecretRedactor
from src.evaluation.runtime_support import write_json_atomic
from src.evaluation.utils import load_eval_config
from src.orchestrator.dashboard import Catalog, identity, read_json
from src.orchestrator.findings import FindingsWorker, exclusive, inputs

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "eval_harness/orchestrator"
META_URL = "https://api.meta.ai/v1"
REPOSITORIES = {
    "hermes": "https://github.com/NousResearch/hermes-agent.git",
    "openhands": "https://github.com/OpenHands/software-agent-sdk.git",
    "deerflow": "https://github.com/bytedance/deer-flow.git",
}


def git(*args):
    return subprocess.check_output(
        ["git", *args], text=True, encoding="utf-8", stderr=subprocess.PIPE, timeout=600
    ).strip()


def connection(value, role):
    if not isinstance(value, dict):
        raise ValueError(f"Configure the {role} model")
    result = {key: str(value.get(key, "")).strip() for key in ("base_url", "model", "api_key_env")}
    url = urlparse(result["base_url"])
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
        raise ValueError(f"{role} needs an HTTP(S) API root without embedded credentials")
    if not result["model"] or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", result["api_key_env"]):
        raise ValueError(f"{role} needs a model ID and credential environment-variable name")
    return result


def validate_request(value, catalog):
    kind = value.get("runtime", "hermes")
    if kind not in REPOSITORIES:
        raise ValueError("Choose Hermes, OpenHands or DeerFlow")
    source = str(value.get("source", "")).strip()
    if not source or source.startswith("-"):
        raise ValueError("Provide a repository URL or local Git checkout")
    if not Path(source).is_dir() and urlparse(source).scheme not in {"http", "https"}:
        raise ValueError(
            "Source must be an HTTP(S) repository URL or an existing local Git checkout"
        )
    limits = value.get("limits", {})
    for key, default, maximum in (
        ("rounds", 3, 5),
        ("generations", 3, 3),
        ("initial_tests", 5, 5),
        ("active_tests", 12, 12),
    ):
        limits[key] = limits.get(key, default)
        if type(limits[key]) is not int or not 1 <= limits[key] <= maximum:
            raise ValueError(f"{key} must be between 1 and {maximum}")
    if limits["initial_tests"] > limits["active_tests"]:
        raise ValueError("Initial test count exceeds the active-test limit")
    orchestrator = connection(value.get("orchestrator"), "orchestrator")
    target = connection(value.get("target"), "target")
    environment = load_eval_config(ROOT / ".env")
    for spec in (orchestrator, target):
        if not environment.get(spec["api_key_env"]):
            raise ValueError(
                f"Set {spec['api_key_env']} in .env or the shell. Local APIs may use a placeholder value."
            )
    adapter = {
        "kind": kind,
        "python": str(value.get("python", sys.executable)).strip(),
        "max_iterations": 15,
        "request_interval": 0,
    }
    if not Path(adapter["python"]).is_file() and not shutil.which(adapter["python"]):
        raise ValueError("Select an installed Python executable for the runtime")
    if kind != "hermes":
        adapter.update(
            runtime_image=str(value.get("runtime_image", "")).strip(),
            recursion_limit=100,
            memory_flush_timeout=600,
        )
        if not adapter["runtime_image"] or adapter["runtime_image"].startswith("-"):
            raise ValueError("Provide the built native Docker image tag or digest")
    else:
        approval = value.get("hermes_approval", "auto")
        if approval not in {"auto", "gate"}:
            raise ValueError("Unknown approval mode")
        adapter["hermes_approval"] = approval
    config = {
        "scope": str(
            value.get(
                "scope",
                "Explore software and distributed-system properties using realistic workflows, meaningful controls and varied complexity.",
            )
        ).strip(),
        "source_input": source,
        "requested_revision": str(value.get("revision", "")).strip(),
        "orchestrator": {**orchestrator, "timeout_seconds": 180, "read_timeout_seconds": None},
        "target": target,
        "adapter": adapter,
        "limits": limits,
        "prompt_variant": "architecture_first_v3",
        "execute": value.get("execute", True),
    }
    if type(config["execute"]) is not bool or not config["scope"]:
        raise ValueError("Provide a scope and boolean execution setting")
    prior = value.get("prior_campaign")
    if prior:
        previous = read_json(catalog.resolve(prior) / "config.json", {})
        if previous.get("adapter", {}).get("kind", "hermes") != kind:
            raise ValueError("Saved tests must come from the same runtime")
        config["seed_scenarios"] = catalog.seed_selection(prior)
        config["previous_source"] = previous["source"]
    base = str(value.get("base_revision", "")).strip()
    if base:
        if kind != "hermes":
            raise ValueError("Framework-version comparisons currently support Hermes")
        config["requested_base"] = base
    return config


def resolve_source(config, folder):
    """Clone into owned run data, pin a commit, and leave the user's checkout alone."""
    location = config.pop("source_input")
    requested = config.pop("requested_revision", "")
    destination = folder / "agent_source"
    local = Path(location).is_dir()
    if local:
        location = str(Path(location).resolve())
        if git("-C", location, "status", "--porcelain", "--untracked-files=no"):
            raise ValueError("Commit or stash tracked changes before comparing a local checkout")
        revision = requested or git("-C", location, "rev-parse", "HEAD")
        try:
            repository = git("-C", location, "remote", "get-url", "origin")
        except subprocess.CalledProcessError:
            repository = Path(location).as_uri()
    else:
        repository, revision = location, requested
    if config["adapter"]["kind"] != "hermes":
        image = json.loads(
            subprocess.check_output(
                ["docker", "image", "inspect", config["adapter"]["runtime_image"]], text=True
            )
        )[0]
        labels = image["Config"].get("Labels") or {}
        if labels.get("org.agent-evaluation.runtime") != config["adapter"]["kind"]:
            raise ValueError("Docker image runtime does not match the selected agent")
        image_revision = labels.get("org.opencontainers.image.revision")
        if not image_revision:
            raise ValueError("Docker image has no source revision label")
        revision = revision or image_revision
        config["adapter"]["runtime_image"] = image["Id"]
    git("clone", "--no-hardlinks", location, str(destination))
    if revision:
        if revision.startswith("-"):
            raise ValueError("Invalid revision")
        git("-C", str(destination), "checkout", "--detach", revision)
    revision = git("-C", str(destination), "rev-parse", "HEAD")
    git("-C", str(destination), "remote", "set-url", "origin", repository)
    config["source"] = {
        "repository": repository,
        "revision": revision,
        "local_path": str(destination),
    }
    previous = config.pop("previous_source", None)
    if previous and previous["repository"].removesuffix(".git") != repository.removesuffix(".git"):
        raise ValueError("Saved tests belong to a different repository")
    base = config.pop("requested_base", "")
    if not base and previous and config["adapter"]["kind"] == "hermes":
        if previous["revision"] != revision:
            base = previous["revision"]
    if base:
        base_revision = git("-C", str(destination), "rev-parse", "--verify", base + "^{commit}")
        if base_revision == revision:
            raise ValueError("Base and target revisions are identical")
        base_path = folder / "agent_source_base"
        git("clone", "--no-hardlinks", str(destination), str(base_path))
        git("-C", str(base_path), "checkout", "--detach", base_revision)
        git("-C", str(base_path), "remote", "set-url", "origin", repository)
        config["source"].update(base_revision=base_revision, base_local_path=str(base_path))
    return config


def launch(config, root=DATA):
    folder = Path(root) / "dashboard/launches" / uuid.uuid4().hex
    controller = folder / "controller"
    controller.mkdir(parents=True)
    shutil.copytree(
        ROOT / "src", controller / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    shutil.copy2(ROOT / "scenario.schema.json", controller / "scenario.schema.json")
    config["artifacts_root"] = str(Path(root).resolve() / "dashboard/runs")
    config["regression_index"] = str(Path(root).resolve() / "regression_index.json")
    write_json_atomic(folder / "config.json", config)
    write_json_atomic(
        folder / "launch.json", {"status": "starting", "runtime": config["adapter"]["kind"]}
    )
    env = os.environ.copy()
    values = load_eval_config(ROOT / ".env")
    for spec in (config["orchestrator"], config["target"]):
        env[spec["api_key_env"]] = values[spec["api_key_env"]]
    env["PYTHONUNBUFFERED"] = "1"
    try:
        with (folder / "worker.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "src.orchestrator.app", "--worker", str(folder)],
                cwd=controller,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                start_new_session=os.name != "nt",
            )
    except OSError as exc:
        write_json_atomic(folder / "launch.json", {"status": "error", "error": str(exc)})
        raise
    return {"id": folder.name, "pid": process.pid}


def worker(folder):
    from src.orchestrator.runner import run

    folder = Path(folder).resolve()
    config = read_json(folder / "config.json")
    state = {
        "status": "cloning",
        "runtime": config["adapter"]["kind"],
        "pid": os.getpid(),
        "started_at": datetime.now(UTC).isoformat(),
    }
    write_json_atomic(folder / "launch.json", state)
    try:
        config = resolve_source(config, folder)
        execute = config.pop("execute")
        write_json_atomic(folder / "config.json", config)
        state["status"] = "running"
        write_json_atomic(folder / "launch.json", state)

        def created(output):
            state.update(attempt=str(output), campaign_id=identity(output))
            write_json_atomic(folder / "launch.json", state)

        output = run(config, execute=execute, on_created=created)
        result = read_json(output / "campaign.json", {})
        state.update(status="finished", attempt=str(output), stop_reason=result.get("stop_reason"))
    except Exception as exc:
        keys = [
            os.environ.get(config[role]["api_key_env"], "") for role in ("orchestrator", "target")
        ]
        detail = getattr(exc, "stderr", None) or str(exc)
        state.update(status="error", error=SecretRedactor(keys)(f"{type(exc).__name__}: {detail}"))
    finally:
        state["finished_at"] = datetime.now(UTC).isoformat()
        write_json_atomic(folder / "launch.json", state)
    return 2 if state["status"] == "error" else 0


def handler(catalog, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, value, status=200, content_type="application/json"):
            body = (
                json.dumps(value, ensure_ascii=False).encode()
                if content_type == "application/json"
                else value.encode()
            )
            self.send_response(status)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def allowed_host(self):
            return self.headers.get("Host") in {
                f"localhost:{self.server.server_port}",
                f"127.0.0.1:{self.server.server_port}",
            }

        def do_GET(self):
            if not self.allowed_host():
                return self.send({"error": "Use the localhost dashboard URL"}, 403)
            route = urlparse(self.path).path
            if route in {"/", "/app.js", "/style.css"}:
                filename, mime = {
                    "/": ("index.html", "text/html"),
                    "/app.js": ("app.js", "text/javascript"),
                    "/style.css": ("style.css", "text/css"),
                }[route]
                text = (Path(__file__).parent / "web" / filename).read_text(encoding="utf-8")
                return self.send(text.replace("__SESSION_TOKEN__", token), content_type=mime)
            try:
                if route == "/api/campaigns":
                    jobs = [
                        {"id": p.parent.name, **read_json(p, {})}
                        for p in sorted((catalog.root / "dashboard/launches").glob("*/launch.json"))
                    ]
                    return self.send({"campaigns": catalog.cards(), "jobs": jobs})
                if route == "/api/defaults":
                    values = load_eval_config(ROOT / ".env")
                    runtimes = {}
                    for card in reversed(catalog.cards()):
                        if card.get("bundled"):
                            continue
                        config = read_json(catalog.resolve(card["id"]) / "config.json", {})
                        runtimes[card["runtime"]] = {
                            k: config.get("adapter", {}).get(k) for k in ("python", "runtime_image")
                        }
                    return self.send(
                        {
                            "repositories": REPOSITORIES,
                            "runtimes": runtimes,
                            "python": sys.executable,
                            "meta_key_present": bool(values.get("META_KEY")),
                        }
                    )
                if route.startswith("/api/campaign/"):
                    return self.send(catalog.detail(route.rsplit("/", 1)[-1]))
                return self.send({"error": "Unknown route"}, 404)
            except (ValueError, OSError, KeyError) as exc:
                return self.send({"error": str(exc)}, 400)

        def do_POST(self):
            if not self.allowed_host() or self.headers.get("X-Session-Token") != token:
                return self.send({"error": "Reload the local dashboard before submitting"}, 403)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise ValueError("Invalid request size")
                value = json.loads(self.rfile.read(length))
                if not isinstance(value, dict):
                    raise ValueError("Expected a settings object")
                if self.path == "/api/start":
                    config = validate_request(value, catalog)
                    return self.send(launch(config, catalog.root), 202)
                if self.path == "/api/stop":
                    path = catalog.resolve(value["id"])
                    write_json_atomic(
                        path / "stop.json",
                        {"reason": "Operator requested a stop from the dashboard"},
                    )
                    return self.send({"status": "stop_requested"})
                if self.path == "/api/findings/retry":
                    path = catalog.resolve(value["id"])
                    number = value.get("round")
                    if type(number) is not int or number < 1:
                        raise ValueError("Select an existing round")
                    item = inputs(path / f"round_{number:03d}")
                    if item is None:
                        raise ValueError("This round has no completed analysis")
                    with exclusive(item[2]) as acquired:
                        if (
                            acquired
                            and read_json(item[2] / "status.json", {}).get("status") == "error"
                        ):
                            write_json_atomic(item[2] / "status.json", {"status": "pending"})
                    return self.send({"status": "retry_requested"})
                return self.send({"error": "Unknown route"}, 404)
            except (ValueError, OSError, KeyError, TypeError) as exc:
                return self.send({"error": str(exc)}, 400)

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data", type=Path, default=DATA)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        return worker(args.worker)
    catalog = Catalog(args.data, ROOT / "examples/campaigns")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler(catalog, secrets.token_hex(24)))
    findings = FindingsWorker(catalog, ROOT / ".env")
    findings.thread.start()
    print(
        f"Agent Reliability Lab\nOpen http://localhost:{server.server_port}\nCtrl+C closes the dashboard. Campaign workers have their own lifecycle.",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        findings.stop.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
