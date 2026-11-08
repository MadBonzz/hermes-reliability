# Agent Reliability Lab

An agent that designs experiments for other agents. Point it at **any agentic system's
repository or local source directory**, watch it generate and review realistic tasks, then inspect what happened
when the agent encountered changing state, failed tools, untrusted content, or interrupted work.

The local dashboard follows the full loop — source inspection, dataset discovery, scenario generation,
three independent reviewers, execution, causal analysis, and the next iteration.
Scenario design and review are framework-independent. **Hermes, OpenHands, and DeerFlow**
have built-in execution adapters; other systems connect through a custom execution
adapter. Hermes also supports comparisons between framework revisions.

## 1. Install

Requirements are **Python 3.11+**, **Git**, and **uv**. OpenHands and DeerFlow also
require a running **Docker Engine** with a built runtime image. Hermes uses its own
installed Python environment.

From your clone of this repository, run

```sh
uv sync --locked
```

To explore the dashboard immediately, run `uv run --locked python -m src.orchestrator.app`
and open `http://localhost:8765`. It includes three completed example campaigns,
one each for Hermes, OpenHands and DeerFlow. Viewing these examples requires no API key
or installed agent runtime. Continue below to launch new experiments.

Copy `.env.example` to `.env` using your editor or file manager. For the default
Meta configuration, add

```dotenv
META_KEY=your-meta-api-key
```

The default models are **muse-wonder-1.0** for the orchestrator and all three reviewers,
and **muse-spark-1.3-contributor** for the agent being evaluated. Credentials are read
from the shell environment, then the repository-root `.env`. Keep `.env` private.

## 2. Prepare the agent runtime

### Hermes

Install Hermes and its dependencies using the Hermes repository's setup process.
Locate the Python executable in that installation's environment. The dashboard asks
for this executable under **Runtime Python**.

Install this project's dependencies into the Hermes environment while preserving
its existing packages. Activate that environment, then run from this project

```sh
uv sync --active --inexact --locked --no-dev
```

For direct benchmark commands, also set `HERMES_AGENT_ROOT` in `.env` to the checkout
containing `run_agent.py`. Dashboard campaigns clone their selected source into
private run storage and pass that checkout to Hermes.

### OpenHands or DeerFlow

Build a native runtime image from a clean checkout at the revision you want to test.
The following OpenHands example uses a pinned revision already exercised by this project

```sh
git clone https://github.com/OpenHands/software-agent-sdk.git eval_harness/sources/openhands_sdk
git -C eval_harness/sources/openhands_sdk checkout b0789c73151a967fdd4a10f8383d2c0bc7109563
docker build -f src/evaluation/agent_runtime.Dockerfile --build-arg AGENT=openhands --build-arg SOURCE_REVISION=b0789c73151a967fdd4a10f8383d2c0bc7109563 -t agent-eval-openhands:b0789c7 eval_harness/sources/openhands_sdk
```

For DeerFlow

```sh
git clone https://github.com/bytedance/deer-flow.git eval_harness/sources/deerflow
git -C eval_harness/sources/deerflow checkout 14abd9a28eb035f277aa56098caab1b0600c6e8d
docker build -f src/evaluation/agent_runtime.Dockerfile --build-arg AGENT=deerflow --build-arg SOURCE_REVISION=14abd9a28eb035f277aa56098caab1b0600c6e8d -t agent-eval-deerflow:14abd9a eval_harness/sources/deerflow
```

Select that image in the dashboard. Leave **Revision** empty to use its labelled
source commit. The image and inspected source must match. Newer source revisions
require a matching rebuilt image and may need adapter updates.

For these runtimes, use this project's `.venv` Python as **Runtime Python**.
The dashboard fills settings from earlier local campaigns when available.

### Other agentic systems

Choose **Custom agent** and supply a repository URL or local code directory. No Git
history is required for local code. The campaign takes a private content-hashed snapshot;
local Git directories without a selected revision include working changes and untracked
files. Dependency directories, `.env` files, private-key files and symlinks are excluded.
Remote Git repositories are cloned and pinned to the resolved commit.

You can generate and review scenarios immediately with **Execute generated tests**
unchecked. To run them against your framework, provide a trusted Python adapter factory
such as `/absolute/path/adapter.py:create_adapter`. This supplies the framework's launch,
tool, state and trace integration. A source path alone does not define those runtime
interfaces. See [the custom adapter contract](ORCHESTRATOR.md#custom-agent-execution).

## 3. Open the dashboard

```sh
uv run --locked python -m src.orchestrator.app
```

Open **http://localhost:8765**. Use `--port 8766` if that port is occupied.

1. Click **New campaign** and choose a built-in runtime or **Custom agent**.
2. Enter a repository URL or local source directory. Built-in execution adapters
   require a clean Git checkout matching the runtime; custom agents also accept local working code.
3. Configure runtime execution if needed: Python for Hermes, a matching Docker image
   for OpenHands/DeerFlow, or a custom adapter factory. Leave execution unchecked for scenario design.
4. Choose an earlier campaign to reuse its tests, or choose **Start a new test suite**.
5. Review the model settings and budgets, then click **Launch campaign**.

The main agent and reviewers use the same orchestrator model. Model endpoints,
credential-variable names, scope, approval mode, and budgets are editable under
**Models, budgets & comparison settings**. The defaults allow five initial tests,
twelve active tests, three generations per round, and three execution rounds.
A seeded campaign can begin with up to twelve existing tests.

The dashboard shows selected datasets and their sample availability, source reads and edits, readable scenario cards with actual
user turns and checks, reviewer findings, results by evaluator and condition, and
causal findings. The sidebar shows active campaigns and completed campaigns with
final causal findings, plus completed scenario-design campaigns. The dashboard prepares readable finding cards using an additional
call to that campaign's orchestrator model for each analyzed round. It caches the result
and records the additional requests and tokens. Each card retains its original findings,
attribution and citations. **Stop at next boundary**
requests a cooperative stop after the current request or execution stage finishes.
Closing the dashboard leaves its separately launched campaign worker running.

The three bundled examples are portable, read-only snapshots under `examples/campaigns/`.
They include scenario contracts, reviews, branch outcomes, analyses, cached finding cards
and usage totals. Private machine paths are replaced with placeholders. Citations retain
their original identifiers. Complete raw API and execution traces are retained locally.
If the matching full campaign exists on your machine, the sidebar shows that local campaign.

Generated evaluator code and Hermes tools execute on the host. OpenHands and
DeerFlow task tools execute in nonroot containers without host mounts or network
access. Model API calls are relayed by the host and billed to your provider account.
Custom adapters define their own execution environment. The dashboard binds to localhost
and uses a session token for launch/stop requests.

### Workload-specific scenario sources

Before the first scenario-generation request, the campaign fetches relevant dataset
cards and small user-request samples. Each runtime has a different default source set:

| Agent | Sources | Workload |
|---|---|---|
| Hermes | [AssistantBench](https://huggingface.co/datasets/AssistantBench/AssistantBench), [WebLINX](https://huggingface.co/datasets/McGill-NLP/WebLINX) | Personal assistance, conversational browsing and research |
| OpenHands | [SWE-chat](https://huggingface.co/datasets/SALT-NLP/SWE-chat), [Pi sessions](https://huggingface.co/datasets/badlogicgames/pi-mono) | Coding conversations, debugging and repository changes |
| DeerFlow | [Clem's ML Intern sessions](https://huggingface.co/datasets/clem/ml-intern-sessions), [Lewis Tunstall's sessions](https://huggingface.co/datasets/lewtun/ml-intern-sessions), WebLINX, AssistantBench | Research, ML engineering and information synthesis |
| Custom agent | Source-guided searches of Hugging Face datasets, its [Agent Traces catalogue](https://huggingface.co/datasets?format=format%3Aagent-traces), and Zenodo | Workflows inferred from the agent's actual source and campaign scope |

For custom agents, the orchestrator first reads source to identify the workload and
form short search queries, calls the public catalogue APIs, and selects relevant results.
No paid search-service key is required. An optional `HF_TOKEN` in `.env` enables dataset
access granted to your Hugging Face account; gated sources still require that account's approval.

The generator and all three reviewers receive the selected sources, access limitations,
and available excerpts. Each scenario rationale records its dataset and adaptation, or
explains why it comes directly from the architecture. The sources seed synthetic tasks
and independent checks; their recorded answers or reasoning are not correctness oracles.
AssistantBench contains benchmark tasks, WebLINX human demonstrations, and session-upload
provenance varies by source. These are not interchangeable kinds of real-user evidence.

Campaigns save `datasets/manifest.json`, request metadata/hashes and custom-search candidates.
Hugging Face cards and JSONL samples use pinned revisions. Viewer samples record their
fetch time, split and row because that API does not pin rows to a commit. Gated or unsupported
samples appear as card-only or metadata-only evidence. If no selected source can be inspected,
the campaign stops with an explicit discovery error. It never substitutes AssistantBench
for an unavailable coding or custom-agent source. See [discovery settings](ORCHESTRATOR.md#dataset-discovery)
to override sources or deliberately run offline without discovery.

### Local models and other APIs

Both orchestrator and target connections accept a Chat Completions API root and
an exact model ID. Tool-capable models are required for the target. The orchestrator
must be able to return the structured action/response objects described in its prompts.

| Provider | API root | Credential variable example |
|---|---|---|
| Meta | `https://api.meta.ai/v1` | `META_KEY` |
| LM Studio | The URL shown by your server, ending in `/v1` | `LOCAL_API_KEY` |
| Ollama | `http://localhost:11434/v1` | `LOCAL_API_KEY` |
| Other compatible API | Your provider's Chat Completions API root | `PROVIDER_API_KEY` |

Add the named variable to `.env`. For a local endpoint without authentication,
use a nonempty placeholder such as `LOCAL_API_KEY=local`. Enter the server's exact
model identifier. Orchestrator output-token caps are optional explicit configuration.
Target frameworks can supply their own SDK or retry defaults, recorded in API traces.

## Reuse tests and compare framework versions

The dashboard discovers earlier campaigns for the selected repository. Reusing one
imports its hash-verified scenario snapshot into the new campaign. The orchestrator
can read the selected campaign's analyses, reports and cleaned transcripts through
indexed evidence. It
can retain, revise, remove, or add tests, with review and validation applied again.
Earlier custom evaluator code requires explicit reimplementation if its capabilities
are needed by the retained tests.

For Hermes, selecting an earlier campaign at a different revision automatically
sets that revision as the comparison base. You can also enter a **Base revision**.
The orchestrator receives the source diff and indexed prior results, then records
predicted outcomes before execution.

| Execution lane | Framework | Evaluator |
|---|---|---|
| `candidate` | Selected revision | Orchestrator-edited evaluator |
| `trusted` | Selected revision | Original evaluator |
| `base_version` | Earlier revision | Original evaluator, in version mode |

Each lane samples its own trajectory. The differential report classifies observed
changes by property layer. Interpretation uses transcripts and evidence to assess
framework changes, model variability, verifier defects, and unavailable capabilities.

## CLI alternatives

Copy `orchestrator.example.json` or `orchestrator.regression.example.json` into
`eval_harness/`, configure the pinned source and runtime, then run

```sh
uv run --locked python -m src.orchestrator --config eval_harness/my_campaign.json --execute
```

Omit `--execute` to generate and review tests. Add `--smoke` for one active test and
one execution round. The full configuration and native adapter contracts are in
[ORCHESTRATOR.md](ORCHESTRATOR.md).

## Run the authored benchmark

The authored suite contains **109 scenarios and 192 user turns**, including seven
controlled pairs and eighteen scaled workflows. It uses `scenario.schema.json`.
Validate without installing an agent runtime

```sh
uv run --locked python -m src.evaluation.evaluate --validate-only
```

For Hermes, activate its prepared Python environment and configure

```dotenv
HERMES_AGENT_ROOT=/path/to/hermes-agent
EVAL_BASE_URL=https://api.meta.ai/v1
EVAL_MODEL=muse-spark-1.3-contributor
EVAL_API_KEY=your-api-key
```

Then run

```sh
uv run --active --no-sync python -m src.evaluation.evaluate
uv run --active --no-sync python -m src.evaluation.evaluate --suite-file suites/core_v1.json --hermes-approval gate
uv run --active --no-sync python -m src.evaluation.evaluate --scenario-id scen_41_versioned_event_edit --repetitions 3
```

For local models, change `EVAL_BASE_URL`, `EVAL_MODEL`, and `EVAL_API_KEY` to the local
server settings. CLI arguments override the shell environment and `.env`.
The frozen `core_v1` selection contains 32 scenarios for comparisons.

## Evidence and interpretation

Dashboard launches live under `eval_harness/orchestrator/dashboard/`. Each launch
freezes controller source and schema. Campaigns retain configuration, source
provenance, scenario revisions, reviewer feedback, both evaluator results,
transcripts, raw API/tool evidence, costs, and analyses. Benchmark runs live under
`eval_harness/runs/`. These directories and `.env` are ignored by Git.

A failed prerequisite can produce a separately labelled diagnostic continuation
from an authored successful prefix. Controlled comparisons use independent branch
state. Shared sampled prefixes currently support portable service state. File-bearing
workflows can branch at turn one. Task completion, property exposure, enforcement,
and evaluation validity are reported separately. Dollar estimates require explicit
model pricing. Missing usage remains unknown.

Read [BENCHMARK.md](BENCHMARK.md) for scoring and branching contracts and
[PROPERTY_CATALOG.md](PROPERTY_CATALOG.md) for property coverage.

## Development

```sh
uv run --locked python -m unittest discover -s tests -v
uv run --locked ruff check src tests
uv run --locked ruff format --check src tests
node --check src/orchestrator/web/app.js
```

The web interface uses the Python standard library and static HTML/CSS/JavaScript.
It requires no frontend build step. Tests use fake model transports unless a native
integration test is explicitly enabled.

| Directory | Purpose |
|---|---|
| `src/orchestrator/` | Generation, review, campaign lifecycle, dashboard, and regression index |
| `src/orchestrator/prompts.py` | Main, reviewer, and analysis instructions |
| `src/orchestrator/web/` | Dashboard interface |
| `examples/campaigns/` | Three reviewed, portable dashboard result snapshots |
| `src/evaluation/` | Runtime adapters, perturbations, mocks, verification, and evidence |
| `scenarios/`, `suites/` | Authored tasks and frozen selections |
| `tests/` | Regression and integration checks |
| `eval_harness/` | Private local experiments |
| `archives/` | Retired benchmark and historical evidence |
