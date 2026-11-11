# Developing Hermes Reliability Lab

Reliability Lab is a native Hermes plugin with a campaign orchestrator, evaluator adapters, web dashboard and Desktop page. Keep generated scenarios, campaign evidence, credentials and local runtime state outside the source tree. Use focused branches and preserve the evidence and execution contracts below.

## Layout

- `plugin.yaml`, `__init__.py`: native manifest and lazy registration.
- `src/plugin/`: supported Hermes model client, tools, slash command, CLI, source preparation and persistent profile-local jobs.
- `src/plugin/history.py`, `regression.py`: versioned frozen benchmarks, per-run history and generation-free regression execution. Keep memory as verified references to these records; use the native Hermes memory tool in a live session, never direct memory-file writes.
- `src/orchestrator/`: source/dataset discovery, generator, independent reviewers, evidence indexing, bounded execution/analysis loop, runtime adapters and dashboard catalogue.
- `src/evaluation/`, `scenario.schema.json`: machinery needed to validate and execute generated scenarios; these are not a bundled benchmark.
- `dashboard/`: host-authenticated REST router and web SDK tab, with the workbench isolated in its own frame.
- `desktop/plugin.js`: native Desktop SDK page using the same API.
- `tests/`: synthetic fixtures and retained component/lifecycle regression tests.

## Integration rules

Use `ctx.llm`, the public CLI/plugin surfaces, `plugins.plugin_storage`, `agent.secret_scope`, and the documented dashboard/Desktop SDKs. Do not patch Hermes host internals or alias this plugin into global `src`. Package-relative imports support Hermes's synthetic plugin namespace. Direct frozen worker scripts use `src` within their separate evaluator process.

Register lazily, with no network, state creation or campaign launch during `register(ctx)`. Provider credentials stay host-owned. Target credentials are separate, optional, narrowly passed to target execution, and never stored in job configuration. Never read host `.env` or auth files directly. Background workers must retain the originating profile. Model output cannot choose interpreters, trusted factories, credentials, or auto-approval.

Preserve candidate-versus-trusted evaluator execution, frozen schema/path protections, evidence hashes, reference-prefix validation, independent reviews and explicit budgets. Invalid or missing evidence is not a successful target run. A stopped or interrupted campaign is not convergence. Record runtime enforcement separately from agent decisions and evaluator defects. Never weaken assertions to improve a score.

Exploration retains candidate/trusted execution and reviews. Fixed regression deliberately has one frozen trusted lane and no generation or new reviews. It must execute unchanged benchmark content, validate historical evidence and configuration compatibility, and never turn an invalid/incomparable run into a regression verdict. Source revision is the intended comparison variable. Retain prior evidence; changed benchmark content gets a new ID.

The legacy target harness runs in separate subprocesses and may use private interfaces of the *tested* runtime. Keep that distinct from host plugin integration, and document compatibility warnings. Native target containers retain their source/image pinning, no-network/no-host-mount boundaries, and host-only provider secrets. Host evaluator code is not OS-sandboxed.

Test with `uv run python -m unittest discover -s tests -q`, `uv run ruff check .`, JavaScript syntax checks, and Hermes plugin doctor/admission checks. Changes to lifecycle, auth, evidence access, or configuration require relevant negative-path tests. Report fixture-only verification honestly; don't describe it as paid-provider or live target validation.
