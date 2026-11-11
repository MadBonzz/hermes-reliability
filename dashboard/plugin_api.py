"""Hermes mounts these routes behind its dashboard authentication and profile scope."""

import hashlib
import importlib.util
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

# Hermes imports dashboard backends standalone. Load only this plugin's package,
# as the official security plugin does; never occupy a Hermes/global `src` name.
_ROOT = Path(__file__).resolve().parents[1]
_NAME = "_hermes_reliability_dashboard_" + hashlib.sha256(str(_ROOT).encode()).hexdigest()[:16]
if _NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        _NAME, _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[_NAME] = module
    spec.loader.exec_module(module)
Service = importlib.import_module(".src.plugin.service", _NAME).Service
router = APIRouter()


@router.get("/defaults")
def defaults():
    obj = Service()
    return {
        "repositories": {
            "hermes": "https://github.com/NousResearch/hermes-agent.git",
            "openhands": "https://github.com/OpenHands/software-agent-sdk.git",
            "deerflow": "https://github.com/bytedance/deer-flow.git",
        },
        "execution_enabled": obj.options.get("execution_enabled") is True,
    }


@router.get("/campaigns")
def campaigns():
    return Service().list()


@router.get("/history")
def history():
    return Service().history.list()


@router.get("/campaign/{campaign_id}")
def detail(campaign_id: str):
    try:
        return Service().catalog.detail(campaign_id)
    except (ValueError, OSError) as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/evidence/{campaign_id}")
def evidence(campaign_id: str, path: str, offset: int = 0):
    try:
        return Service().evidence(campaign_id, path, offset)
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/{action}")
async def action(action: str, request: Request):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 32768:
            raise HTTPException(413, "Request too large")
    import json

    try:
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError("Expected an object")
        obj = Service()
        if action == "start":
            return obj.start(value)
        if action == "stop":
            return obj.stop(campaign_id=value.get("id"), job_id=value.get("job_id"))
        if action == "freeze":
            return obj.promote(
                value["campaign_id"], value.get("round_number"), value.get("remember", False)
            )
        if action == "compare":
            return obj.history.comparison(value["baseline_run_id"], value["current_run_id"])
        if action == "memory-note":
            return obj.history.memory_note(value["benchmark_id"], value["baseline_run_id"])
        if action == "index-history":
            return obj.history.list(refresh=True)
        raise HTTPException(404, "Unknown action")
    except (ValueError, OSError, KeyError) as exc:
        raise HTTPException(400, str(exc)) from exc
