# Target integration and scope

Reliability Lab uses a structured scenario contract and evaluator to test configured agents. Orchestration uses Hermes's public model API; runtime adapters connect the target under test.

## Built-in targets

- **Hermes:** configure a Python interpreter containing the target Hermes dependencies. The supplied repository is privately cloned and pinned; the evaluator imports that target checkout only in separate worker processes. The worker has its own recorded Hermes home. The adapter forces `hermes_approval: gate` in this plugin. Required interactive operations can fail in an unattended worker.
- **OpenHands / DeerFlow:** configure `runtimes.<kind>.runtime_image`. Build from a clean, pinned upstream checkout as the Docker build context, using this plugin's `src/evaluation/agent_runtime.Dockerfile` and the build arguments `AGENT` and `SOURCE_REVISION`. Do not use the plugin repository as the target build context. The image must carry matching `org.agent-evaluation.runtime` and `org.opencontainers.image.revision` labels. Source and image revisions are checked before execution. Docker and the image must exist before launching an execution campaign.
- **Custom:** configure `runtimes.custom.factory` as `/absolute/path/integration.py:create_adapter`. This is trusted operator configuration, copied and hashed into the campaign. It is never accepted from the generator or a tool request. Keep the factory self-contained or install its dependencies in the Hermes environment; do not assume a globally importable `src` package inside the plugin host.

A custom factory receives the adapter configuration and returns an object implementing:

```python
class Adapter:
    def describe(self):
        return {
            "framework": "My agent",
            "reference_tools": ["read_repository", "edit_file"],
        }

    def preflight(self, evaluator, output):
        # Validate the generated suite and its prerequisite references.
        # Retain evidence at output. Never mutate evaluator inputs.
        return {"passed": True, "reference_validation": "describe your evidence"}

    def execute_revision(self, evaluator, output, target, key):
        # Run the real target with the generated suite, write report.json and
        # its referenced traces/state evidence under output, and return status.
        # key is the separately configured target credential, not Hermes auth.
        return {"status": "complete"}
```

That sketch describes method signatures, not a complete runnable integration. The wrapper checks suite coverage, report shape, reference tools, and immutable input hashes. The controller independently audits execution evidence. Returning a success string without valid reports and traces does not establish a passing test. See `src/orchestrator/custom_adapter.py`, `src/evaluation/hermes_runner.py`, and `src/evaluation/audit.py` for executable contracts, and the custom-adapter unit tests for rejection cases.

## Retained limitations

The target harness predates the plugin. It records detailed tool/service/file evidence and API exchanges by instrumenting the separately tested runtime. Hermes admission lint flags its private target-runtime imports. The host-facing plugin itself registers supported surfaces and does not replace Hermes core functions, but a new target Hermes version can still break that evaluator integration. Treat target compatibility as version-specific until its integration checks pass.

Target API capture currently covers Chat Completions traffic. Provider protocols that do not produce that evidence need a new adapter/audit implementation. Container-only artifact checks and DeerFlow output scopes are not universal custom-runtime features. Reference prefixes support a restricted portable state model, not arbitrary runtime checkpoints or exact hidden-model-state restoration. Local snapshots omit symlinks, dependency directories and credential files; they cap source size at 100 MB / 20,000 files, preserve file permissions, and require a destination outside the source tree.

Built-in preflight/execution children inherit only `PATH`, `HOME`, temporary-directory and locale variables, Windows executable-discovery variables where present, and `PYTHONNOUSERSITE=1`. Phase-specific evaluation settings are added explicitly. They do not inherit host proxy URLs, provider credentials, `PYTHONPATH` or Hermes approval overrides. If a target needs additional integration settings, provide them through a trusted runtime integration rather than broadening inheritance to arbitrary host variables. Container auxiliary inference uses the same owning-profile target credential as execution; credentials are never added to saved adapter configuration.

Local source selection calls `agent.file_safety.get_read_block_error` and the profile-aware Hermes home helpers. This shared guard is a host compatibility dependency and currently appears in admission's undocumented-import warning, alongside the existing target-runtime imports. The plugin calls the guard without replacing or modifying it and does not fall back to unguarded local access if it is unavailable.

The generator may propose evaluator code changes. Candidate and frozen trusted lanes help expose disagreements, but neither model review nor an independent lane proves that a task's oracle is correct. The dashboard preserves reviewer disagreement, uncertainty, enforcement attribution and missing evidence. Source-only design has no target execution findings.
