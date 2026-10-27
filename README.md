# Hermes evaluation harness

Run 20 declarative, three-turn scenarios against a separately installed Hermes
agent and a model served by any compatible Chat Completions API, including
LM Studio, Ollama, and hosted providers such as OpenRouter. The harness registers
scenario-specific mock tools, applies faults, and checks filesystem state, tool
calls, and responses.

## Repository layout

```text
.
|-- README.md                       Setup and development guide
|-- AGENTS.md                       Motivation, architecture, contributor guidance
|-- pyproject.toml                  Dependencies and lint/format settings
|-- uv.lock                         Locked dependency versions
|-- .env.example                    Configuration template; copy to .env
|-- scenario.schema.json            Validation schema for one scenario
|-- scenarios/                      One JSON per scenario, including its mocks
|-- src/
|   |-- eval_runner.py              CLI, turn execution, checkpoints, cleanup
|   |-- scenario_validation.py      Loading and preflight validation
|   |-- mock_registry.py            Mock behavior and Hermes registration
|   |-- perturbations.py            File changes, faults, permissions, compression
|   |-- checks.py                   State, process, and output assertions
|   |-- utils.py                    Configuration, Hermes setup, shared helpers
|   |-- runtime_support.py          Traces, hashes, and file snapshots
|   |-- api_trace.py                API requests, responses, streaming, redaction
|   |-- check_hermes_integration.py  Offline installed-Hermes checks
|   `-- correlate_lmstudio_logs.py  Server-log correlation
|-- tests/
|   |-- helpers.py                  Shared fixtures and fake Hermes runtime
|   `-- test_*.py                   Regression tests grouped by source module
`-- eval_harness/                   Local generated artifacts; entirely ignored
    |-- results/runs/<run-id>/      Reports, traces, and optional run analysis
    `-- eval_workspace/<run-id>/    Task files, normally removed after the run
```

Start with [a scenario](scenarios/scen_01_idempotency_mock_timeout.json), then follow
`main()`, `run_single_scenario()`, and `_run_turn()` in
[eval_runner.py](src/eval_runner.py). The JSON files are independent tasks, not
copies of one configuration. The single [schema](scenario.schema.json) defines
which fields, checks, and behaviors are valid.

See [AGENTS.md](AGENTS.md) for the project motivation, scenario overview, mock and
perturbation mechanisms, scoring limitations, and development conventions.

## Setup

You need **uv and Python 3.11+** for validation and development. Agent execution
also requires an existing Hermes installation with its own dependencies and a
local or hosted Chat Completions API serving a model with tool-calling support.
The tested Hermes environment uses Python 3.14. Windows execution has been
verified; Linux uses the same Python entry points but has not been verified end
to end. Permission-fault scenarios require a
normal user because root can bypass the intended restrictions.

Run commands from the repository root. Create the harness environment and validate
all scenario files without loading Hermes or contacting a model:

```sh
uv sync --locked
uv run --locked python src/eval_runner.py --validate-only
```

Copy [.env.example](.env.example) to `.env` and set `HERMES_AGENT_ROOT`,
`EVAL_BASE_URL`, and `EVAL_MODEL` for your installation. The template documents the
optional API key and Windows path format. Configuration precedence is
**CLI arguments > shell environment > repository-root .env > defaults**.
Values are literal; `${VARIABLE}` interpolation is disabled.

`EVAL_API_KEY` supplies authentication when needed. Existing `LM_STUDIO_URL` and
`LM_STUDIO_API_KEY` settings still work as aliases; generic names win within the
same configuration layer. The model ID is sent exactly as configured, without
mapping names between servers.

`HERMES_AGENT_ROOT` locates the checkout containing `run_agent.py`; it does not
activate Python or install Hermes dependencies. Activate your existing Hermes
virtual environment before running an evaluation:

```sh
# Linux
source /path/to/hermes/venv/bin/activate
```

```powershell
# Windows PowerShell
& 'C:/path/to/hermes/venv/Scripts/Activate.ps1'
```

Install harness dependencies into that active environment:

```sh
uv sync --active --inexact --locked --no-dev
```

`--active` selects Hermes's environment, `--inexact` preserves its other packages,
and `--no-dev` omits development tools. The project lockfile covers harness
packages, not Hermes itself. The `python-dotenv` pin matches the tested Hermes
installation; check compatibility if using another version.

## Run

With the Hermes environment active and your configured model server running:

```sh
# All scenarios
uv run --active --no-sync python src/eval_runner.py

# One scenario; repeat --scenario-id to select more
uv run --active --no-sync python src/eval_runner.py --scenario-id scen_01_idempotency_mock_timeout

# A standalone scenario file
uv run --active --no-sync python src/eval_runner.py --scenarios scenarios/scen_01_idempotency_mock_timeout.json

# Keep task files for inspection
uv run --active --no-sync python src/eval_runner.py --keep-workspace
```

`--no-sync` avoids further dependency changes. Use `--model` to override the exact
model ID. The CLI is the reference for all options and defaults:

```sh
uv run --locked python src/eval_runner.py --help
```

Relative CLI paths resolve from the current working directory. Exit codes are
`0` for success, `1` for completed evaluations with failed scenarios, and `2` for
invalid input, execution errors, or interruption.

### Ollama

Start Ollama (or run `ollama serve` if its service is not already running), pull a
model with tool support, and use the exact tag shown by `ollama list`. For example:

```sh
ollama pull qwen3:8b
ollama list
uv run --active --no-sync python src/eval_runner.py --base-url http://localhost:11434/v1 --api-key ollama --model qwen3:8b
```

To make that your default, set these values in `.env`:

```dotenv
EVAL_BASE_URL=http://localhost:11434/v1
EVAL_MODEL=qwen3:8b
EVAL_API_KEY=ollama
```

The harness uses Ollama's [OpenAI-compatible chat-completions endpoint](https://docs.ollama.com/api/openai-compatibility),
including tool calls. Use `/v1`, not the native `/api/chat` URL. Local Ollama ignores
the API key, but the client requires a nonempty value. No Ollama Python package is
needed. Set an appropriate context window on the Ollama model/server for multi-turn
tasks; the harness does not change server context settings.

Evaluation reports and traces work with either server. The optional
`correlate_lmstudio_logs.py` utility specifically parses LM Studio logs; it does not
parse Ollama logs or participate in evaluation/scoring.

### Hosted APIs, including OpenRouter and Llama models

Use the provider's Chat Completions API root, its exact model ID, and your API key.
For example, [OpenRouter](https://openrouter.ai/docs/quickstart) can serve
[Meta's Llama model](https://openrouter.ai/meta-llama/llama-3.3-70b-instruct):

```dotenv
EVAL_BASE_URL=https://openrouter.ai/api/v1
EVAL_MODEL=meta-llama/llama-3.3-70b-instruct
EVAL_API_KEY=your-provider-api-key
```

Then run the same evaluation command in the prepared Hermes environment:

```sh
uv run --active --no-sync python src/eval_runner.py
```

For another provider, change those three values. Use the API root, not the full
`/chat/completions` URL. Requests use Bearer authentication and the OpenAI Chat
Completions tool-call format. The endpoint and selected model must support that
format; a provider's native API or a Responses-only endpoint needs an adapter.
Provider-specific features and available models can differ. Model names are not
translated, and the harness does not download or substitute a model.

## Scenarios and scoring

Edit [scenarios/](scenarios/) directly. Each file contains `id`, `name`,
`property_tested`, optional `mock_tools`, and `turns`. The loader reads immediate
`*.json` files in filename order and validates them before agent execution.

Each turn supplies a `prompt` and three check groups: `state_checks` inspect files
and JSON values, `process_checks` inspect tool calls, and `output_checks` match
final-answer strings or regular expressions. `{WORKSPACE}` expands to the scenario
workspace path. Optional `pre_turn_perturbations` run before the agent;
`post_turn_cleanup` runs after scoring, including on a failed turn.

Each scenario has a fresh agent and workspace. History and files persist between
its turns. All checks must pass to proceed; a failed turn skips the scenario's
remaining turns, and an agent/harness error stops the suite. Output checks are
literal assertions, not semantic judgments.

Mock definitions belong in their scenario JSON. New behavior or check types need
matching changes in the implementation, [schema](scenario.schema.json), and tests.
Run `--validate-only` after editing definitions.

## Local outputs and privacy

Everything under `eval_harness/` is excluded by [.gitignore](.gitignore), including
reports, traces, correlated requests, analysis documents, and retained workspaces.
The local `.env`, caches, and build artifacts are also ignored. Author-maintained
scenarios, the schema, source, tests, and `uv.lock` remain shareable.

Each run checkpoints evidence under `eval_harness/results/runs/<run-id>/`:

| File | Contents |
|---|---|
| `eval_report.json` | Scores, errors, configuration, file snapshots, and trace paths. |
| `traces/<scenario-id>/turn_<n>.jsonl` | Agent history/results, tool execution, perturbations, and checks. |
| `traces/<scenario-id>/api.jsonl` | Requests and responses captured directly from the API client, for local **and** hosted endpoints. |

API capture writes each request before dispatch, including its messages, tool
schemas, model, and generation settings. It records response status/headers, JSON
bodies, provider IDs, and any usage/reasoning fields returned by the provider.
Streaming responses are saved incrementally as SSE events, including tool-call
deltas and usage events. Network errors and each SDK/Hermes retry dispatch have
their own records. A request ID pairs events; run, scenario, turn, and phase
identify their context, including calls made during context compression.

`api_response_end.complete` indicates that the HTTP body was fully consumed, not
that the task succeeded. Partial/abandoned responses are marked incomplete; a
request without a terminal event can indicate a forced process termination.
Trace write failures fail the evaluation rather than silently discarding evidence.
API trace files remain after workspace cleanup. No provider dashboard or server
log export is required to preserve these exchanges.

Capture covers synchronous and asynchronous HTTPX Chat Completions calls made
inside this evaluator process while a scenario is active, including auxiliary
calls to other endpoints. It cannot capture provider-internal routing, hidden
reasoning that was not returned, or HTTP requests made by separate tool processes.
Scenarios run sequentially so concurrent worker calls retain the active turn's
identity. Credentials in headers/URLs and known API-key values are redacted from
saved evidence; prompts, responses, and task files remain private run data.

Server-log correlation is an optional separate command for LM Studio;
run `python src/correlate_lmstudio_logs.py --help` for its arguments. Keep custom
output destinations inside ignored directories as well; arbitrary paths passed
with `--output` or `--workspace-root` are not automatically made private.

Task workspaces are deleted after final evidence is saved unless `--keep-workspace`
is set. Existing workspaces are never swept or reused. File snapshots preserve
hashes, sizes, and UTF-8 content up to 1 MB; keep the workspace for original larger
or binary files. Failed cleanup, failed evidence saving, or forced termination can
leave task files behind. Workspace binding sets tool working directories; it is
not a filesystem sandbox.

## Development

Use the harness environment for regression tests and Ruff checks; neither needs
Hermes or a model server:

```sh
uv run --locked python -m unittest discover -s tests -v
uv run --locked ruff check --no-cache src tests
uv run --locked ruff format --no-cache --check src tests
```

Omit `--check` from the formatting command to apply changes. To verify real tool
registration, filesystem behavior, and cleanup without invoking a model, activate
the prepared Hermes environment and run:

```sh
uv run --active --no-sync python src/check_hermes_integration.py
```
