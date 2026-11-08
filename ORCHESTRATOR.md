# Orchestrator in the Hermes plugin

The campaign engine is now exposed through the native Hermes integration. Start with [README.md](README.md); configure target execution using [runtime integration](docs/runtime-integration.md).

The source preparation, dataset selection, independent generation/review contexts, candidate/trusted evaluator lanes, schema guards, evidence hashes, audits, and causal analysis remain implemented in `src/orchestrator/` and `src/evaluation/`. Generated suites start empty and are private to each campaign. The original 109-scenario benchmark and its saved examples are deliberately absent from this branch.

Use `hermes reliability start`, the `reliability_start` tool, or the Reliability Lab dashboard tab. The retired standalone HTTP server, repository `.env` setup and provider-specific dashboard defaults are not part of the plugin.
