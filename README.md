# Agent reliability evaluation harness

Evaluate how a model and agent runtime handle tasks involving persistent state,
multiple tools, changing requirements and failures. Scenarios test properties such
as idempotency, consistency, recovery, ordering, authorization and confidentiality.
The harness supplies fixtures, mock services, perturbations and assertions, and
records the evidence needed to explain each outcome.

Hermes is the first supported runtime and is installed separately. Models connect
through a Chat Completions API with structured tool calls. Local LM Studio and
Ollama endpoints and compatible hosted endpoints use the same evaluation path.

## Benchmark versions

| Suite | Definitions | User turns | Entry point |
|---|---|---|---|
| V2 | 109 scenarios in [scenarios_v2/](scenarios_v2/) | 192 | `src/eval_v2.py` |
| V1 | 91 scenarios in [scenarios/](scenarios/) | 157 | `src/eval_runner.py` |

V2 is the current suite. It adds neutral model-visible workspace names, seven
controlled baseline/treatment pairs, diagnostic continuations after failed
prerequisites, cost accounting and several dimensions of complexity. Eighteen
scaled tasks vary reimbursement, customer-retirement and research workflows
across 2, 5 and 10 files and one or three turns.

[BENCHMARK_V2.md](BENCHMARK_V2.md) describes the V2 contracts and limitations.
[PROPERTY_CATALOG.md](PROPERTY_CATALOG.md) documents the underlying properties and
scenario coverage. Source hashes identify the exact definitions used by each run.

## Setup

Use uv and Python 3.11+ for validation and development. Actual model evaluations
require a Python environment containing Hermes and its dependencies. The tested
Hermes environment uses Python 3.14 on Windows. Linux execution has not been
verified end to end. Permission-fault tasks require a normal user account because
root can bypass the intended restrictions.

Run these commands from the repository root.

~~~sh
uv sync --locked
uv run --locked python src/eval_v2.py --validate-only
uv run --locked python src/eval_runner.py --validate-only
~~~

Copy [.env.example](.env.example) to .env and configure your installation.

~~~dotenv
HERMES_AGENT_ROOT=/absolute/path/to/hermes/workspace
EVAL_BASE_URL=http://localhost:1235/v1
EVAL_MODEL=your-exact-model-id
# EVAL_API_KEY=your-key-if-required
~~~

HERMES_AGENT_ROOT identifies the checkout containing run_agent.py. Activate the
Hermes Python environment separately.

~~~sh
# Linux
source /path/to/hermes/venv/bin/activate
~~~

~~~powershell
# Windows PowerShell
& 'C:/path/to/hermes/venv/Scripts/Activate.ps1'
~~~

Install harness dependencies into that environment while preserving Hermes's
other packages.

~~~sh
uv sync --active --inexact --locked --no-dev
~~~

Configuration precedence is CLI arguments, shell environment, repository-root
.env, then defaults. Values are literal and interpolation is disabled. Legacy
LM_STUDIO_URL and LM_STUDIO_API_KEY aliases remain supported.

## Run V2

With the Hermes environment active, use the configured model endpoint.

~~~sh
# Full suite
uv run --active --no-sync python src/eval_v2.py

# Select a scenario; repeat --scenario-id for more
uv run --active --no-sync python src/eval_v2.py --scenario-id v2_scen_41_versioned_event_edit

# Repeat the suite with reproducible condition ordering
uv run --active --no-sync python src/eval_v2.py --repetitions 3 --order-seed 17

# Choose a fresh output directory
uv run --active --no-sync python src/eval_v2.py --output eval_harness/my_v2_run
~~~

Each run freezes the source implementation, schema and selected scenario
contracts before starting workers. Each phase runs in a fresh process, a neutral
workspace_1 directory and a private Hermes profile. Tool-side Python environments
are created per phase. History, files and service state persist across turns
within that phase.

V2 sends the authored user request without an EVAL prefix. Scenario IDs, condition
labels, assertions and complexity metadata stay in evaluator evidence. Before API
dispatch, a validator checks messages and tool definitions for known private
repository paths and evaluation markers.

Ordinary task failures retain their failed gates. An early failure can launch a
fresh diagnostic continuation with an authored successful prefix, including real
tool calls, service effects and verified state. Its assistant messages are
synthetic. Conditional continuation results are reported separately from ordinary
task completion.

Controlled cases run baseline and treatment conditions. Supported service-only
prefixes are sampled once and copied into independent branch state. Shared
prefix support excludes successful file operations and persistent shell/code
sessions because their native state cannot yet be checkpointed safely. File-heavy
pairs branch before the first turn. See [the branching contract](BENCHMARK_V2.md#controlled-branching).

Separate model runs can use separate CLI processes and output directories.
Scenarios inside one process execute sequentially because API capture and runtime
hooks have a single active scope. Frozen worker source protects an ongoing V2 run
from later edits to the checkout.

V2 exits with 0 when campaign execution completes, including campaigns with failed
tasks. Runtime, logging and worker errors stop the campaign and return 2.
Inspect report.json for task outcomes. The iteration limit counts model API
iterations, and one iteration can request multiple tools.

## Model endpoints

Use an API root such as /v1 and the provider's exact model identifier. The harness
passes the identifier through unchanged. The endpoint must support Chat
Completions and structured tools.

| Connection | Configuration |
|---|---|
| LM Studio | Use the server's displayed URL and loaded model ID. |
| Ollama | Use `http://localhost:11434/v1`, a tool-capable model tag and a nonempty placeholder API key. |
| Cohere | Set COHERE_API_KEY or COHERE_KEY and select `--provider cohere`. |
| Baseten | Set BASETEN_API_KEY and select `--provider baseten`. BASETEN_KEY and BAESTEN_KEY are accepted aliases. |
| Other compatible providers | Set EVAL_BASE_URL, EVAL_MODEL and EVAL_API_KEY, or supply CLI overrides. |

Select Command A explicitly when using the Cohere profile.

~~~sh
uv run --active --no-sync python src/eval_v2.py --provider cohere --model command-a-03-2025
~~~

The profile's default model is command-a-plus-05-2026. Command A+ uses high
reasoning effort. The Cohere profile defaults to seven seconds between API
dispatches, including retries. Use --request-interval to change pacing.

The Baseten profile defaults to meta-models/muse-spark-open-1026.

~~~sh
uv run --active --no-sync python src/eval_v2.py --provider baseten
~~~

Explicit connection arguments override profile defaults. Provider availability,
model access and request allowances are controlled by the serving account.

## Evidence, cost and analysis

V2 outputs live under `eval_harness/v2_runs/<run-id>/` by default.

| Artifact | Contents |
|---|---|
| report.json | Campaign configuration, source hashes, case outcomes and branch reports |
| source/ | Frozen worker implementation and schema |
| repeat_N/ | Per-scenario ordinary, prefix, baseline, treatment and diagnostic phases |
| Phase report.json | Frozen contract, turn checks, tool events, service state and filesystem snapshots |
| Phase traces/ | Turn JSONL and captured API exchanges |
| metrics.json and analysis.md | Computed completion, cost and complexity summaries |

API capture records requests, streamed or nonstreamed responses, retries,
transport errors and provider-returned usage. Correlation uses request IDs and
private run/turn context. Known API credentials are redacted. Captured task
content and synthetic secrets remain available for evaluation.

Every executed tool callback contributes to cost metrics, including calls unused
by a process assertion. Metrics include repeated identical calls, failed results,
API requests, elapsed turn time and pacing. Repetition may be necessary
verification, polling or retry. Missing token usage is recorded explicitly.

Optional --prices accepts an exact-model rate mapping. Keep local pricing
configuration under eval_harness/ and consult
[the price format](BENCHMARK_V2.md#cost-and-efficiency). Dollar estimates require
complete usage and applicable rates. Local hardware/electricity costs require
separate measurement.

Run read-only post-processing on a saved campaign.

~~~sh
uv run --locked python src/analyze_v2.py eval_harness/v2_runs/RUN_ID/report.json
uv run --locked python src/audit_v2.py eval_harness/v2_runs/RUN_ID/report.json
~~~

The audit checks capture completeness, tool callbacks, shared histories and
recomputed usage. An interrupted response may have an explicit transport error
and no body or usage. Logging failures invalidate the evaluation.

Passive native-control observations record selected Hermes checks, including
allowed operations, refusals, source hashes and hook availability. Missing
observations leave coverage unresolved. A refusal can block an unsafe attempt or
obstruct a legitimate operation. Task completion and property enforcement need
the actual call, state and verifier evidence.

The automatic summaries and attribution rules have bounded coverage. Detailed
causal interpretation still requires reviewing traces. Native file protections,
mock-service preconditions, harness interventions and application-specific
requirements have different responsibilities.

## Scenarios and verification

Edit the standalone JSON files directly. V1 uses
[scenario.schema.json](scenario.schema.json), and V2 uses
[scenario_v2.schema.json](scenario_v2.schema.json). Each task defines its request,
mock interfaces, turn perturbations and state/process/output checks.

Mock services support conditional updates, idempotent replay, pagination,
transient errors and prerequisite-dependent actions. Their state and committed
effects are recorded independently of agent-editable files. File-backed mocks
also exist. Perturbations can change files, permissions, service state, fault
flags and conversation history.

All assertions must pass at a turn's gate. State checks inspect artifacts and
service effects. Process checks inspect structured tool calls. Output checks
include text and structured JSON assertions. Visible requests must communicate
material policies and output contracts. A final-state pass alone cannot prove
that every intermediate action was safe.

Validate authored prefixes through real installed tools without model inference.

~~~sh
uv run --active --no-sync python src/check_reference_prefixes.py --scenarios scenarios_v2 --schema scenario_v2.schema.json
~~~

## V1 commands

V1 remains available for its own scenario contracts and output layout.

~~~sh
uv run --active --no-sync python src/eval_runner.py
uv run --active --no-sync python src/eval_runner.py --scenario-id scen_41_versioned_event_edit
uv run --active --no-sync python src/eval_runner.py --keep-workspace
uv run --locked python src/audit_api_evidence.py eval_harness/results/runs/RUN_ID
uv run --locked python src/attribute_failures.py eval_harness/results/runs/RUN_ID
~~~

V1 writes eval_report.json and traces under eval_harness/results/runs/. Its
default user messages include a correlation prefix. Exit codes are 0 for success,
1 for completed evaluations containing task failures and 2 for invalid input or
execution failure. Historical contracts and source hashes govern interpretation.

## Exploratory orchestrator

The separate [orchestrator](ORCHESTRATOR.md) generates candidate scenarios from
architecture information and prior evidence, reviews them, optionally executes
them through an adapter and analyzes outcomes. A Hermes adapter is included.
Other runtimes require their own trusted execution adapter.

Copy [orchestrator.example.json](orchestrator.example.json) to an ignored local
location. Configure exact orchestrator and target models, credential environment
variable names, evidence inputs and budgets. This entry point reads credentials
from the shell environment.

~~~sh
# Generate and review proposals
uv run --locked python -m src.orchestrator --config eval_harness/my_orchestrator.json

# Also execute reviewed proposals
uv run --locked python -m src.orchestrator --config eval_harness/my_orchestrator.json --execute
~~~

## Repository layout

| Path | Responsibility |
|---|---|
| scenarios/, scenarios_v2/ | Authored tasks and mock definitions |
| scenario.schema.json, scenario_v2.schema.json | Validation contracts |
| src/eval_runner.py | Shared turn execution, verification and V1 CLI |
| src/eval_v2.py, src/eval_v2_worker.py, src/benchmark_v2.py | V2 workers, branch lifecycle and presentation |
| src/mock_registry.py, src/mock_services.py, src/perturbations.py | Tool registration, service state and injected changes |
| src/checks.py, src/scenario_validation.py, src/reference_execution.py | Assertions, preflight and authored prefixes |
| src/api_trace.py, src/runtime_support.py, src/control_observer.py | API capture, state snapshots and native-control observations |
| src/evaluation_metrics.py, src/analyze_v2.py, src/audit_v2.py | Cost, complexity and campaign audits |
| src/attribute_failures.py, src/audit_api_evidence.py | Evidence-based diagnostic rules and exchange audits |
| src/execution_isolation.py, src/utils.py | File-tool path policy and runtime configuration |
| src/orchestrator*.py | Scenario-generation loop and adapters |
| src/transcript_formatting.py | Shared native-check glossary and transcript tables |
| tests/ | Regression tests |
| eval_harness/ | Private generated workspaces, runs, traces, analyses and orchestrator output |

[AGENTS.md](AGENTS.md) supplies contributor guidance. Source modules live directly
under src/ and tests under tests/. Scenarios are authored JSON files. uv.lock is
the dependency lockfile.

## Privacy and execution boundaries

.gitignore excludes eval_harness/, credentials, common output directories,
analysis exports, logs and runtime caches. Git contains source, authored scenarios,
schemas, tests, dependency files and maintained documentation. Keep custom output
paths under eval_harness/ so those ignore rules apply.

File-tool path guards cover read_file, write_file, patch and search_files. Terminal
and arbitrary code retain native access and can bypass this policy. It is not OS
isolation. V2's request validator checks for known evaluator content before model
dispatch, but cannot prevent every possible host-file or subprocess access.

Traces contain task content and are private even after credential redaction.
API capture cannot recover hidden reasoning, unreceived bytes, provider-internal
activity or traffic in separate tool processes. Preserve incomplete evidence as
such when interpreting a run.

## Development checks

~~~sh
uv sync --locked
uv run --locked python src/eval_v2.py --validate-only
uv run --locked python src/eval_runner.py --validate-only
uv run --locked python -m unittest discover -s tests -v
uv run --locked ruff check --no-cache src tests
uv run --locked ruff format --no-cache --check src tests
~~~

In the prepared Hermes environment, exercise installed tools, prompt isolation,
local HTTP retry/streaming capture and cleanup without a model request.

~~~sh
uv run --active --no-sync python src/check_hermes_integration.py
~~~
