# Hermes Reliability Lab

A native **Hermes plugin** that turns an agent repository or local code directory into an evidence-based reliability campaign. It includes the orchestrator, the evaluator machinery needed by generated tests, a Hermes web dashboard tab, and a Desktop page. **The original 109 authored experiments, saved runs, and example campaign exports are not included.**

This branch is developed privately in Cornell GitHub Enterprise. The original lab and its benchmark remain on their existing branches.

## What it does

1. **Inspect the system.** Freeze a private source snapshot, read its architecture and tool interfaces, and retain source hashes and citations.
2. **Find realistic workloads.** Select relevant datasets before scenario generation. For unfamiliar agents, a model-assisted source profile guides Hugging Face and Zenodo API searches.
3. **Build experiments.** Generate scenario JSON, controlled comparisons, assertions, and campaign-local evaluator changes. Validate schema, paths, assertion coverage, and source references.
4. **Challenge the design.** Separate task-quality, experimental-validity, and verification/attribution reviewers inspect the proposal. Their conversations and judgments are saved independently.
5. **Execute when configured.** Preflight prerequisite references and run candidate and frozen trusted evaluators against the target. Retain tool traces, service/file state, API evidence, audit results, and revision hashes.
6. **Explain and iterate.** Analyze cited execution evidence, distinguish agent failures from evaluator defects and missing evidence, and refine within explicit request, generation, test-count, and round budgets.

This is executable orchestration, validation, runtime integration, and evidence storage—not a collection of prompts. Model judgments are still judgments: a passing check or reviewer approval is not a general reliability guarantee. Reaching a generation cap does not mean all reviewers approved; those results remain visible.

```mermaid
flowchart LR
    A[Repository or local code] --> B[Source snapshot + dataset discovery]
    B --> C[Scenario and evaluator generation]
    C --> D[Three independent reviews]
    D --> E[Mechanical validation]
    E --> F[Candidate + trusted execution]
    F --> G[Evidence audit + causal analysis]
    G --> C
    G --> H[Hermes dashboard + saved findings]
```

## Install in Hermes

Use a current Hermes release with native plugins, `ctx.llm`, CLI subcommands, and dashboard extensions. The manifest requires Hermes **0.21.5+**; validation for this build uses upstream commit `acdc8f1ba65c0231ae8277e846a4d55928913991`. Target-runtime compatibility is a separate concern described below.

Clone the **plugin branch** into your active profile's plugin directory. Authenticate Git to Cornell in your usual way first:

```bash
git clone --branch codex/hermes-reliability-plugin --single-branch \
  https://github.coecis.cornell.edu/sj945/agent-regression.git \
  "${HERMES_HOME:-$HOME/.hermes}/plugins/hermes-reliability"

hermes plugins enable hermes-reliability
hermes plugins doctor "${HERMES_HOME:-$HOME/.hermes}/plugins/hermes-reliability" --ci
```

Accept Hermes's dependency installation if prompted. Restart your running Hermes session/gateway after enabling. For named profiles, use that profile's `HERMES_HOME` and enable the plugin in that profile. No plugin-specific orchestration API key is required: **orchestration uses the active profile's Hermes model and host-owned authentication through `ctx.llm`**. Session-only model overrides are not transferred into the independent CLI worker.

Open `hermes dashboard` and select **Reliability Lab**. Hermes Desktop also has a **Reliability Lab** extension/page; enable its Desktop half in Plugins. Both use the same profile-local campaign store.

Start with scenario design:

```text
Use Reliability Lab to design reliability tests for /absolute/path/to/my-agent.
Focus on recovery after interrupted tool calls and repeated external actions.
```

Or use the CLI:

```bash
hermes reliability start /absolute/path/to/my-agent --runtime custom \
  --scope "Recovery from interrupted tool calls and duplicate actions"
hermes reliability status
hermes reliability stop JOB_ID
hermes reliability evidence CAMPAIGN_ID campaign.json
```

The native tools are `reliability_start`, `reliability_status`, `reliability_stop`, `reliability_evidence`, `reliability_history`, `reliability_freeze`, `reliability_compare`, and `reliability_memory`. `/reliability status` lists campaigns. Launch returns a job ID; a separate Hermes CLI worker performs the campaign, and closing the browser does not stop it. Stops are cooperative at safe boundaries; an in-flight source request, provider call, or target execution finishes or reaches its configured timeout first. If a worker exits unexpectedly, its job is shown as interrupted and its evidence is retained. You can reuse its generated tests in a new campaign; the plugin does not silently replay interrupted external actions.

## Frozen benchmarks and regression history

There are two explicit modes:

- **Explore** discovers workloads, generates/revises tests, and runs independent design reviews and candidate/trusted evaluators. “Saved tests” seeds further exploration; the generator can revise them.
- **Regression** executes a frozen benchmark against a new source snapshot and compares its outcomes with a selected historical run. It makes no generation calls, does not edit scenarios or assertions, and uses the exact saved trusted evaluator. Its single trusted execution lane compares against historical evidence; it does not pretend to perform new design reviews or establish a causal explanation.

After an execution campaign finishes, select **Freeze selected round** in the web dashboard, or **Freeze latest valid trusted round** in Desktop. Only a trusted execution with complete scenario coverage, matching input hashes and a passing evidence audit can establish a benchmark. A valid run may contain failed task assertions: that is a useful baseline for measuring improvements.

Each benchmark has a content-based ID covering its repository, runtime, scenario files, schema and evaluator. Changed assertions or evaluator code create a different benchmark version. Every finished campaign is indexed, including ordinary exploration runs, invalid executions and design-only attempts. Records are separate files so concurrent workers do not overwrite a shared index. Use **Import earlier runs** to backfill campaigns saved before version 0.2.0; legacy reports without sufficient input provenance cannot become baselines.

The benchmark also retains hashed copies of its original dataset manifest, proposals and review decisions where available. Regression runs reuse that saved provenance; they do not silently substitute newly fetched datasets or claim fresh reviewer approval.

```bash
hermes reliability history --refresh
hermes reliability freeze CAMPAIGN_ID --round-number 1
# Use the full benchmark_id and baseline_run_id returned above:
hermes reliability start /absolute/path/to/my-agent --runtime hermes \
  --mode regression --benchmark-id BENCHMARK_ID --baseline-run-id RUN_ID --execute
hermes reliability compare BASELINE_RUN_ID CURRENT_RUN_ID
```

The dashboard offers benchmark and baseline selectors, saved-run inspection, and comparison results. Regressions, improvements and unchanged outcomes are reported only when benchmark versions and recorded model/runtime settings match and both runs retain valid evidence. Changed models, endpoint URLs, budgets, runtime images or adapter implementations are labeled **incomparable**; missing, changed or invalid evidence is labeled **invalid**. Source revision is the intended variable and can differ. These are observed outcomes, not statistical significance from a single rerun. Installed dependencies and provider-side changes are not fully captured by configuration equality.

Fixed-benchmark execution works through any configured adapter that supplies the required evidence contract. The older exploration workflow that executes both old and new source checkouts in one campaign remains Hermes-specific.

## Remembering baselines with Hermes

Ask Hermes: **“Freeze this campaign as a regression benchmark and remember its baseline.”** The plugin validates the saved run and returns a compact reference containing repository, runtime, benchmark ID and baseline-run ID. Hermes then uses its **native `memory` tool** to save that note in the active profile. `reliability_memory` can prepare the same note for an existing baseline. At the next session, Hermes can resolve those IDs through `reliability_history` when you ask to check changes against the same benchmark.

Exact benchmarks, reports and traces remain in plugin storage. Memory holds references, not test answers or raw logs. A remembered baseline does not authorize execution: the usual explicit execution request and configured runtime are still required. Background workers retrieve the pinned records directly and do not depend on a conversational memory snapshot.

The web and Desktop **Prepare memory note** action produces a request to send to Hermes; it does not claim to write memory. Likewise, `hermes reliability memory-note BENCHMARK_ID RUN_ID` or `freeze … --remember` returns arguments for the native tool. Native memory writes require a live Hermes agent session and can be staged, disabled or rejected at capacity. The plugin never edits `MEMORY.md` directly, replaces the user's memory provider, or silently removes existing notes. Verify the native tool's result before treating a note as saved. Stale or foreign-profile references fail rather than selecting another baseline.

The tested agent's memory stays separate from these operator notes. The current Hermes evaluation harness starts with isolated target profiles and memory disabled; testing the target's memory behavior requires an explicit adapter/scenario implementation with controlled seeding and reset semantics.

## Dataset grounding

| Target | Starting sources |
|---|---|
| Hermes | [AssistantBench](https://huggingface.co/datasets/AssistantBench/AssistantBench), [WebLINX](https://huggingface.co/datasets/McGill-NLP/WebLINX) |
| OpenHands | [SWE-chat](https://huggingface.co/datasets/SALT-NLP/SWE-chat), [Pi development sessions](https://huggingface.co/datasets/badlogicgames/pi-mono) |
| DeerFlow | [Clem's ML Intern sessions](https://huggingface.co/datasets/clem/ml-intern-sessions), [Lewis Tunstall's sessions](https://huggingface.co/datasets/lewtun/ml-intern-sessions), WebLINX, AssistantBench |
| Any other agent | Architecture-guided searches of the Hugging Face dataset catalogue/agent-traces collection and Zenodo records, followed by relevance selection |

No paid search service is required. Discovery obtains bounded dataset cards and user-message excerpts where available, preserving URLs, hashes, selection rationale, and sampling errors. It excludes answer keys, tool results, and recorded thinking from workload seeds. These are inspiration for controlled scenarios; the plugin does not replay entire public sessions or claim they all have equivalent provenance. Some sources are unavailable or gated, and a metadata-only source is labeled accordingly. An empty custom search fails visibly rather than silently substituting AssistantBench.

`HF_TOKEN` is optional for gated/private Hugging Face access. Configure it through Hermes's secret management, not a file in this repository. Public discovery works without it. `dataset_discovery` in plugin settings can select explicit sources or disable discovery for a controlled offline development run; disabled discovery is recorded in campaign evidence.

## Executing targets

**Any repository can be inspected and used for scenario design. Executing an arbitrary agent requires a compatible integration.** Built-in runtime adapters remain available for Hermes, OpenHands, and DeerFlow; other systems use a trusted adapter factory. The plugin does not infer an unknown runtime's deployment, tools, checkpoint semantics, or credentials merely from a URL.

Execution is off by default. Configure the target and runtimes under `plugins.entries.hermes-reliability.settings` in the active Hermes profile. Example:

```yaml
plugins:
  enabled: [hermes-reliability]  # retain your other enabled plugins
  entries:
    hermes-reliability:
      settings:
        execution_enabled: true
        requests: 100
        timeout_seconds: 180
        target:
          base_url: http://localhost:1234/v1
          model: your-exact-target-model-id
        runtimes:
          hermes:
            python: /absolute/path/to/target-hermes/.venv/bin/python
            max_iterations: 15
            timeout_seconds: 1800
          custom:
            factory: /absolute/path/to/integration.py:create_adapter
```

For a hosted target, configure `RELIABILITY_TARGET_API_KEY` through Hermes. The orchestration model's credentials are never extracted or copied. The target adapter receives only the separately configured target credential. An unauthenticated local model server may leave it unset. Target inference currently requires an OpenAI-compatible Chat Completions endpoint; orchestration follows the provider support of Hermes itself.

Cohere Command A was live-tested as a Hermes target with `model: command-a-03-2025` and `base_url: https://api.cohere.ai/compatibility/v1`. Native file-tool execution and evidence audits passed a small operator-authored smoke. Using the same model for orchestration did not complete autonomous scenario generation; see the [live verification results](docs/verification.md#live-cohere-command-a-verification) before treating this as an end-to-end validated model choice.

Then explicitly request execution through the dashboard checkbox, the `execute` tool parameter, or:

```bash
hermes reliability start https://github.com/NousResearch/hermes-agent.git \
  --runtime hermes --revision TARGET_COMMIT --execute
```

Model-initiated execution passes through Hermes's approval hook. The CLI flag and dashboard checkbox are direct operator requests. Target Hermes runs retain their native approval gate; this plugin does not select auto-approval. An unattended target may refuse operations that require an interactive approval, and those refusals must be interpreted as such.

**Execution boundaries matter:** the target Hermes process and generated evaluator Python run on the host. The existing OpenHands/DeerFlow adapters run target tools in pinned, nonroot Docker containers with their configured no-network/no-host-mount boundary; that does not sandbox the host evaluator. Custom integrations control their own target isolation. Use an appropriate test machine/environment for execution. Design mode never executes generated evaluator Python or target tools.

See [runtime integration](docs/runtime-integration.md) for the custom adapter contract and retained runtime limitations.

## Evidence and privacy

State lives under `<active Hermes home>/plugin-data/hermes-reliability/`, outside the plugin install directory. Each job has its configuration, a frozen and hash-checked controller snapshot, and worker log; each campaign has private source snapshots, dataset provenance, scenarios, review decisions where applicable, evaluator snapshots, execution reports, and analysis or regression comparisons. `benchmarks/<id>/` stores frozen evaluator/scenario snapshots and `history/<run-id>.json` stores execution references, hashes, validity and summaries. Original evidence remains with its campaign and must be retained for comparison. Evidence reads are bounded and confined to the selected campaign. The web dashboard uses Hermes authentication and the documented dashboard SDK; it exposes no separate unauthenticated listener.

Source excerpts, sampled dataset user messages, and campaign evidence are sent to the configured model provider during orchestration. Public catalogues receive search queries; Git contacts the requested repository. Target execution may incur separate model costs. Recognized credential values are redacted, but repository and task contents can still be sensitive. Nothing uploads campaigns or findings to Cornell, GitHub, or a marketplace automatically.

## Development and verification

```bash
uv sync --group dev
uv run python -m unittest discover -s tests -q
uv run ruff check .
node --check dashboard/index.js
node --check dashboard/workbench/app.js
node --check desktop/plugin.js
hermes plugins doctor . --ci
hermes plugins validate .
```

Tests use small synthetic fixtures; they do not require the removed benchmark, paid model calls, or public trace downloads. Docker-dependent target tests skip without their images. The plugin integration tests exercise host-client request budgets and evidence, source preparation, scenario generation, three reviewer passes, job cancellation, approval gating, and isolated registration. Browser verification exercises the web workbench with a local authenticated SDK test host. See [verification notes](docs/verification.md) for this build's results and limits.

This private Cornell branch is **not a public catalog submission**. Hermes catalog admission requires a publicly cloneable source repository, and the retained target harness's private-runtime imports need explicit compatibility review before claiming broad release support. No public repository, marketplace entry, hackathon form, or social post is changed by installing or running this plugin.
