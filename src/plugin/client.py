"""Use Hermes's supported model surface without handling provider tokens."""

import json
import time
from dataclasses import asdict, is_dataclass

from ..orchestrator.runner import canonical, check_stop, save


class HermesClient:
    def __init__(self, config, directory, redactor, key, limit, *, llm):
        self.config, self.directory, self.redactor = config, directory, redactor
        self.limit, self.calls, self.llm = limit, 0, llm

    def ask(self, messages):
        check_stop(self.directory)
        if self.calls >= self.limit:
            raise RuntimeError("Orchestrator request budget exhausted")
        if len(canonical(messages)) > self.config["max_request_characters"]:
            raise ValueError("Orchestrator input budget exceeded")
        self.calls += 1
        prefix = self.directory / f"api_{self.calls:04d}"
        save(
            prefix.with_suffix(".request.json"),
            self.redactor({"messages": messages, "model": "Hermes active model"}),
        )
        started = time.monotonic()
        try:
            result = self.llm.complete(
                messages=messages,
                max_tokens=self.config["max_output_tokens"],
                timeout=self.config["timeout_seconds"],
                purpose="reliability.campaign",
            )
        except Exception as exc:
            save(prefix.with_suffix(".error.json"), {"type": type(exc).__name__})
            raise RuntimeError(
                f"Hermes model request failed ({type(exc).__name__}); see Hermes logs"
            ) from exc
        usage = asdict(result.usage) if is_dataclass(result.usage) else (result.usage or {})
        # Keep the controller's evidence/usage format, recording host attribution.
        body = {
            "model": result.model,
            "choices": [{"message": {"content": result.text}}],
            "usage": {
                **usage,
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
                "prompt_tokens_details": {"cached_tokens": usage.get("cache_read_tokens", 0)},
            },
        }
        save(
            prefix.with_suffix(".response.json"),
            self.redactor(
                {
                    "status": 200,
                    "body": canonical(body),
                    "elapsed_seconds": time.monotonic() - started,
                    "host_audit": result.audit,
                    "provider": result.provider,
                }
            ),
        )
        check_stop(self.directory)
        content = result.text.strip()
        if content.startswith("```") and content.endswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        start = content.find("{")
        if start < 0:
            raise json.JSONDecodeError("No JSON object in response", content, 0)
        value, end = json.JSONDecoder().raw_decode(content, start)
        if "{" in content[end:] or not isinstance(value, dict):
            raise ValueError("Expected exactly one JSON object")
        return value
