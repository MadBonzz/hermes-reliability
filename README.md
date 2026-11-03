# Agent reliability evaluation harness

Evaluate how a model and agent runtime handle tasks involving persistent state,
multiple tools, changing requirements and failures. Scenarios test properties such
as idempotency, consistency, recovery, ordering, authorization and confidentiality.
The harness supplies fixtures, mock services, perturbations and assertions, and
records the evidence needed to explain each outcome.

Supported runtimes are Hermes (installed separately on the host), OpenHands and
DeerFlow (pinned native runtimes in Docker). Models connect through a Chat
Completions API with structured tool calls. The orchestrator can generate and
review new tests against each runtime's pinned source and available tools.

## Benchmark

The current suite contains **109 scenarios and 192 user turns** in [scenarios/](scenarios/),
validated by [scenario.schema.json](scenario.schema.json). Run it with `src/evaluation/evaluate.py`.
It includes seven controlled baseline/treatment pairs, diagnostic continuations,
neutral workspaces, cost accounting and workload complexity dimensions. Eighteen
scaled tasks vary reimbursement, customer retirement and research across 2, 5 and
10 input files and one or three turns.

[BENCHMARK.md](BENCHMARK.md) describes execution contracts and limitations;
[PROPERTY_CATALOG.md](PROPERTY_CATALOG.md) maps properties to tasks.

The retired V1 scenarios, three runs, transcripts, analyses and source snapshots
are stored locally in the ignored `archives/benchmark_v1_20261103.zip`. Its embedded
`ARCHIVE_MANIFEST.json` records original paths and SHA-256 hashes. Extract it into
a separate directory to inspect the old edition. The archive is not published.
Existing newer run directories retain their original paths and frozen evidence.
New runs use `eval_harness/runs/`. Scenario numbers are unchanged; current IDs no
longer have the `v2_` prefix. `benchmark_version: 2` remains the data-format
identifier, not a second selectable benchmark.

## Setup

Use uv and Python 3.11+ for validation and development. Hermes evaluations require
its Python environment; the tested host installation uses Python 3.14 on Windows.
OpenHands and DeerFlow use Linux Docker images and do not require a host Hermes
installation. See [native runtime setup](ORCHESTRATOR.md#native-openhands-and-deerflow-adapters)
for image builds, source pinning and configuration. Permission-fault tasks require
a normal user account because root can bypass the intended restrictions.

Run these commands from the repository root.

~~~sh
uv sync --locked
uv run --locked python src/evaluation/evaluate.py --validate-only
~~~

For Hermes, copy [.env.example](.env.example) to .env and configure your installation.

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

## Run the benchmark

With the Hermes environment active, use the configured model endpoint.

~~~sh
# Full suite
uv run --active --no-sync python src/evaluation/evaluate.py

# Select a scenario; repeat --scenario-id for more
uv run --active --no-sync python src/evaluation/evaluate.py --scenario-id scen_41_versioned_event_edit

# Repeat the suite with reproducible condition ordering
uv run --active --no-sync python src/evaluation/evaluate.py --repetitions 3 --order-seed 17

# Choose a fresh output directory
uv run --active --no-sync python src/evaluation/evaluate.py --output eval_harness/my_run
~~~

Each run freezes the source implementation, schema and selected scenario
contracts before starting workers. Each phase runs in a fresh process, a neutral
workspace_1 directory and a private Hermes profile. Tool-side Python environments
are created per phase. History, files and service state persist across turns
within that phase.

The runner sends the authored user request without an EVAL prefix. Scenario IDs, condition
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
pairs branch before the first turn. See [the branching contract](BENCHMARK.md#controlled-branching).

Separate model runs can use separate CLI processes and output directories.
Scenarios inside one process execute sequentially because API capture and runtime
hooks have a single active scope. Frozen worker source protects an ongoing run
from later edits to the checkout.

The runner exits with 0 when campaign execution completes, including campaigns with failed
tasks. Runtime, logging and worker errors stop the campaign and return 2.
Inspect report.json for task outcomes. Hermes's iteration limit counts model API
iterations, and one iteration can request multiple tools. Native OpenHands uses
SDK iterations; DeerFlow uses `--recursion-limit` graph supersteps, which include
middleware execution and are not equivalent to model calls. See the
[native runtime contract](ORCHESTRATOR.md#native-openhands-and-deerflow-adapters).

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
uv run --active --no-sync python src/evaluation/evaluate.py --provider cohere --model command-a-03-2025
~~~

The profile's default model is command-a-plus-05-2026. Command A+ uses high
reasoning effort. The Cohere profile defaults to seven seconds between API
dispatches, including retries. Use --request-interval to change pacing.

The Baseten profile defaults to meta-models/muse-spark-open-1026.

~~~sh
uv run --active --no-sync python src/evaluation/evaluate.py --provider baseten
~~~

Explicit connection arguments override profile defaults. Provider availability,
model access and request allowances are controlled by the serving account.

## Evidence, cost and analysis

Campaign outputs live under `eval_harness/runs/<run-id>/` by default.

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
[the price format](BENCHMARK.md#cost-and-efficiency). Dollar estimates require
complete usage and applicable rates. Local hardware/electricity costs require
separate measurement.

Run read-only post-processing on a saved campaign.

~~~sh
uv run --locked python src/evaluation/analyze.py eval_harness/runs/RUN_ID/report.json
uv run --locked python src/evaluation/audit.py eval_harness/runs/RUN_ID/report.json
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

DeerFlow final deliverables use its native outputs directory. Select
`artifact_scope: "outputs"` on file assertions for those artifacts; ordinary
checks continue to use the task workspace. See [artifact scopes](ORCHESTRATOR.md)
for supported checks and read-only oracle probes.

Edit the standalone JSON files directly using
[scenario.schema.json](scenario.schema.json). Each task defines its request,
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
uv run --active --no-sync python src/evaluation/check_reference_prefixes.py --scenarios scenarios --schema scenario.schema.json
~~~

## Exploratory orchestrator

The separate [orchestrator](ORCHESTRATOR.md) generates scenarios from a pinned
Hermes, OpenHands or DeerFlow GitHub checkout. Three reviewer agents assess the
suite, and the main agent edits campaign-local scenarios and evaluator code. Each execution round compares
the edited evaluator with a frozen copy of this repository's evaluator, then feeds
cleaned transcripts back to the main agent. The starter begins with up to five
tests, permits twelve active tests and defaults to three execution rounds.

Copy [orchestrator.example.json](orchestrator.example.json) to an ignored local
location. Configure exact orchestrator and target models, credential environment
variable names, a source revision and budgets. This entry point reads credentials
from the shell environment and repository-root `.env`.

~~~sh
# Generate and review proposals
uv run --locked python -m src.orchestrator --config eval_harness/my_orchestrator.json

# Also execute reviewed proposals
uv run --locked python -m src.orchestrator --config eval_harness/my_orchestrator.json --execute

# One-test, one-round live smoke campaign through both evaluators
uv run --locked python -m src.orchestrator --config eval_harness/my_orchestrator.json --smoke --execute
~~~

## Repository layout

| Path | Responsibility |
|---|---|
| scenarios/ | Authored tasks and mock definitions |
| scenario.schema.json | Validation contracts |
| src/evaluation/hermes_runner.py | Hermes turn execution, verification and evidence capture |
| src/evaluation/evaluate.py, src/evaluation/eval_worker.py, src/evaluation/benchmark.py | Campaign workers, branch lifecycle and presentation |
| src/evaluation/mock_registry.py, src/evaluation/mock_services.py, src/evaluation/perturbations.py | Tool registration, service state and injected changes |
| src/evaluation/checks.py, src/evaluation/scenario_validation.py, src/evaluation/reference_execution.py | Assertions, preflight and authored prefixes |
| src/evaluation/artifact_checks.py | Private container-based behavioral artifact checks |
| src/evaluation/api_trace.py, src/evaluation/runtime_support.py, src/evaluation/control_observer.py | API capture, state snapshots and native-control observations |
| src/evaluation/evaluation_metrics.py, src/evaluation/analyze.py, src/evaluation/audit.py | Cost, complexity and campaign audits |
| src/evaluation/attribute_failures.py, src/evaluation/audit_api_evidence.py | Reusable turn-attribution and API-evidence helpers |
| src/evaluation/execution_isolation.py, src/evaluation/utils.py | File-tool path policy and runtime configuration |
| src/orchestrator/ | Generation/review loop, workspace editing, execution adapters and transcript export |
| src/orchestrator/prompts.py | Main-agent, reviewer, analysis and response-repair instructions |
| src/evaluation/agent_container.py, src/evaluation/agent_worker.py, src/evaluation/native_agent_bridges.py | Docker isolation, host relay and native OpenHands/DeerFlow entry points |
| src/evaluation/eval_container_worker.py, src/evaluation/agent_runtime.Dockerfile | Native phase execution and pinned-source runtime images |
| src/evaluation/transcript_formatting.py | Shared native-check glossary and transcript tables |
| tests/ | Regression tests |
| eval_harness/ | Private generated workspaces, runs, traces, analyses and orchestrator output |

[AGENTS.md](AGENTS.md) supplies contributor guidance. `src/evaluation/` contains
the benchmark runner, runtime bridges, checks and evidence capture.
`src/orchestrator/` contains generation, review, revision and execution coordination.
Its [prompts.py](src/orchestrator/prompts.py) defines the main-agent policy, design
requirements, reviewer roles, analysis instructions and repair feedback; runtime
code imports them. Evaluated-agent tasks remain in scenario JSON files, and the
agent frameworks supply their own system prompts. Tests live under `tests/`.

Private data is grouped by experiment: `eval_harness/orchestrator/<experiment>/attempts/`
keeps resumed attempts together, with historical launch records and analyses beside
them. New benchmark results live under `eval_harness/runs/`; the retained completed
109-scenario campaign remains under `eval_harness/v2_runs/`. Pinned checkouts and runtime
build evidence live under `eval_harness/sources/` and `eval_harness/runtime/`.
Historical evidence retains its recorded paths; `eval_harness/relocations.json` maps
paths moved during cleanup. `uv.lock` is the dependency lockfile.

## Privacy and execution boundaries

.gitignore excludes eval_harness/, credentials, common output directories,
analysis exports, logs and runtime caches. Git contains source, authored scenarios,
schemas, tests, dependency files and maintained documentation. Keep custom output
paths under eval_harness/ so those ignore rules apply.

For Hermes, file-tool path guards cover read_file, write_file, patch and search_files. Terminal
and arbitrary code retain native access and can bypass this policy. It is not OS
isolation. The request validator checks for known evaluator content before model
dispatch, but cannot prevent every possible host-file or subprocess access.
OpenHands/DeerFlow target execution instead runs in network-disabled, nonroot
containers without host mounts. The evaluator and generated evaluator edits still
execute on the host; container isolation applies to target-agent code.

Traces contain task content and are private even after credential redaction.
API capture cannot recover hidden reasoning, unreceived bytes, provider-internal
activity or traffic in separate tool processes. Preserve incomplete evidence as
such when interpreting a run.

## Development checks

~~~sh
uv sync --locked
uv run --locked python src/evaluation/evaluate.py --validate-only
uv run --locked python -m unittest discover -s tests -v
uv run --locked ruff check --no-cache src tests
uv run --locked ruff format --no-cache --check src tests
~~~

In the prepared Hermes environment, exercise installed tools, prompt isolation,
local HTTP retry/streaming capture and cleanup without a model request.

~~~sh
uv run --active --no-sync python src/evaluation/check_hermes_integration.py
~~~

Native Docker integration tests are opt-in; ordinary unit tests do not contact
hosted models. See [native runtime setup](ORCHESTRATOR.md#native-openhands-and-deerflow-adapters)
for the image-specific test commands. Historical campaign recovery should use
its frozen implementation; the current orchestrator supports current-schema execution and
general interrupted-generation/review recovery.
