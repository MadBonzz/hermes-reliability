# Reliability Lab campaign engine

Start campaigns through the native Hermes integration. See the [README](README.md) for installation and workflows, and [runtime integration](docs/runtime-integration.md) for target setup.

`src/orchestrator/` implements source preparation, dataset discovery, scenario generation, independent reviewer contexts and evidence-based analysis. `src/evaluation/` supplies scenario validation, runtime execution and evidence audits. Exploration compares candidate and frozen trusted evaluators; fixed regression executes a saved benchmark against a selected historical baseline.

Use `hermes reliability start`, the `reliability_start` tool, or the Reliability Lab dashboard tab. The active Hermes profile supplies the orchestration model, authentication and campaign storage. Each job freezes its controller code and source snapshot, and retains the configuration and evidence needed to inspect its result.
