# Benchmark contract

The benchmark is authored in `scenarios/` and validated against `scenario.schema.json`.
Run it with `src/evaluation/evaluate.py`. Each phase uses the existing Hermes runner, tool
registry, assertions, state snapshots, API capture and passive control observer.
The campaign runner provides neutral presentation, controlled conditions, separate workload
dimensions and efficiency analysis.

## Task coverage and revisions

There are 109 scenarios and 192 authored user turns. The original 91 were revised from the archived first edition. Eighteen additional cases vary three workflow families across 2, 5 and 10
input files and one versus three user turns.

| Family | Requested result | Dependencies tested |
|---|---|---|
| Reimbursements | Pay approved invoices and update each invoice with its confirmed receipt | Policy, invoice identity, recipient, amount, service effects, matching receipts, repeated confirmation |
| Customer retirement | Retire a customer while retaining their orders across regional registers | Customer register, every regional order register, preservation of unrelated records |
| Family hike research | Recommend trails meeting ratings and independent family-review requirements | Listing data, multiple review sources, duplicate reviewer IDs, changing constraints |

The workload scale counts policy files too. A 10-file reimbursement task contains
one policy and nine invoices. Record volume also changes with file count in the
first two families. The hike panel holds records fixed while changing their
distribution across files, offering a more focused file-distribution comparison.

Research and service tasks now include direct answers, payment actions, private
contact storage, queue submission and inventory/event changes. File output remains
where the requested workflow produces or modifies an artifact. Structured answers
state their fields in the user request and allow JSON formatting differences and
extra object fields. The answer format is an experimental constraint recorded in
the task contract.

Specific revisions include the following.

- Scenario 10 is one email-reading turn with a clean/injected pair. Its safe
  outcome and evidence of recognizing the malicious instruction are recorded
  separately.
- Scenario 41 reviews the event in chat before changing capacity. Its baseline
  preserves Cedar while its perturbed condition preserves the external Maple edit.
- Scenario 47 conveys invoice identity through INV-A and INV-B. Receipts are
  returned in chat.
- Scenario 49 reviews inventory in chat, avoiding the implicit JSON-wrapper
  contract from the old task.
- Scenario 52 names the report identifier APR-2026 explicitly, making exact
  identifier checks part of the visible contract.
- The file named UNTRUSTED_README.md is named README.md in the current suite.

The research expansion was informed by actual AssistantBench tasks about
independent family recommendations for hikes, early classes near a landmark,
current vegan menus, shipping costs, and dated records. The public dataset supplies
task text, answers and explanations. The authored local records here are synthetic
and reproducible. [AssistantBench dataset and examples](https://huggingface.co/datasets/AssistantBench/AssistantBench)

## Neutral model-visible presentation

Each worker runs in a neutral temporary parent with a task directory named
`workspace_1`. Its runtime profile is in `profile`, and task IDs are opaque random
values. User messages have no EVAL correlation prefix. Scenario identifiers,
condition labels, complexity metadata and assertions stay in private reports.
Evaluator file-tool denials use PATH_DENIED and refer to the working directory.

Each phase also creates a fresh Python virtual environment using the worker's
Python version. Default `python`, `python3`, pip and uv package commands use that
environment, with host site-packages excluded. Dependencies persist across turns
within a phase and are removed with its temporary directory. Phase reports record
the interpreter and setup time in `python_environment`. Explicit host interpreter
paths and other shell operations can bypass this routing.

Every API request is checked before dispatch for known private repository paths,
scenario IDs and evaluation markers in messages and tool definitions. Detected
leaks invalidate the execution and remain visible in private evidence. Correlation
uses the API capture context and request IDs. It does not require model-visible
markers.

Hermes file-tool path restrictions cover read_file, write_file, patch and
search_files. Shell/code retain their native capabilities. An agent can inspect
host processes or files through those routes, so neutral presentation has a
defined boundary. If known evaluator content enters a subsequent API request,
the request validator stops that execution. This must be reported as an evidence
contamination event when analyzing results.

## Controlled branching

Seven authored pairs currently cover email injection, external venue editing,
lease expiration, false save acknowledgement and three reimbursement scales.
Each `experiment` declares `branch_turn`, an intervention description, and baseline
overrides. Treatment is the ordinary authored definition. Validation requires
identical user messages and preserves all shared-prefix turns.

At a service-based boundary, the prefix is sampled once. The saved checkpoint
contains the full returned conversation, service state, cumulative effects and
call counters, fault flags, idempotency replay records and receipt counters. Both
conditions receive independent deep copies. Baseline and treatment have separate
fresh Hermes runtime processes, separate task identities and separate evidence.
Condition order is reproducibly shuffled from `--order-seed` and repetition.

Portable prefix support currently covers completed stateful-service tool calls
with an empty task filesystem, plus the exact native refusal reporting disabled
memory. That refusal preserves its warning/history evidence without requiring a
memory-state clone. Successful memory calls, file operations, shell/code sessions, incomplete
tool execution and other runtime state in a prefix cause `unbranchable_prefix`.
The sampled evidence and its cost remain recorded. This constraint avoids
transplanting stale native file metadata or losing Python/kernel/session state.
File-heavy controlled cases branch at turn 1 and execute independently from the
same authored fixtures. Extending nonempty file-prefix support requires a native
Hermes state-checkpoint adapter.

If the sampled prerequisite fails, its failure is retained. An authored reference
prefix can be validated once and used for diagnostic branches, with
`branch_origin: authored_reference`. These branches are distinct from
`sampled_prefix` branches in the analysis. Ordinary cases and each controlled
condition retain separate reference continuations after early failures. A
condition's diagnostic continuation replays that condition's full authored prefix
from fresh state and records its condition explicitly. Its cost is counted once
and its original failed branch remains a failed outcome.

The source implementation is copied into the private run directory before workers
start. Every worker uses that snapshot. Model settings, prices, source/schema
hashes and the selected task contracts are recorded for the run.

## Cost and efficiency

Every phase records all execution callbacks, emitted tool calls, calls by tool,
identical repeated-call groups, failed tool results, observed file-tool paths,
API dispatches and errors, elapsed turn time and pacing delay. Calls used by no
process assertion still contribute to these totals. Repeated calls are observable
overhead candidates and can also be necessary verification or polling.

Native Docker adapters additionally support private `artifact_python` state checks
for executable deliverables. See ORCHESTRATOR.md for the isolation and artifact-copy
limits. Their duration is recorded as `artifact_verification_seconds`, a component
of total elapsed turn time, with no added model tokens or agent tool calls. Separate
this evaluator overhead when comparing agent latency. Historical frozen runs and
the Hermes adapter do not support this optional check.

API usage is reconstructed per request. Streaming usage is counted once per
request. Input, output and cached-input counts are retained where supplied.
Requests with missing usage are listed explicitly. Dollar estimates require an
exact-model price entry and complete usage for the phase. Cached tokens require
an explicit cached-input rate. Local compute cost can be added as a separate
accounting model later.

Price file format is a mapping from exact model ID to numeric rates.

```json
{
  "your-model-id": {
    "input_per_million_usd": 2.5,
    "output_per_million_usd": 10,
    "cached_input_per_million_usd": 0.25
  }
}
```

The example numbers illustrate the file format. Supply the rates applicable to
your provider/account and retain their source and date with the experiment.
Provider token usage and an estimated token charge do not establish the account's
actual invoice or credit deduction.

`metrics.json` counts each shared-prefix execution once in total cost and records
each branch's incremental cost separately. Every continuation still sends its
history to the provider and incurs the corresponding input-token usage. Reference
validation and failed-prefix attempts are included in execution accounting.

## Complexity and recognition evidence

`complexity` records family, turns, input files, services, supplied records,
dependency depth, fault types/count, interaction type and asserted file paths.
Dependency depths are authored estimates of dependent reasoning/action stages.
Fault counts describe adverse episodes, including bad input and context loss,
while ordinary setup and user revisions are excluded. Baseline overrides carry
their own fault dimensions. The analysis
groups results by each axis within family, condition and prefix origin. Covarying
dimensions and sample sizes remain relevant when interpreting differences.

Injection observations record whether the payload appeared in an executed tool
result, named harmful-tool attempts, sensitive argument candidates, and explicit
recognition-language candidates in returned content/reasoning. Each candidate
retains a call or request ID for review. Absence of recognition language yields
`unestablished`. Internal awareness is not identifiable from these traces. A
reported safe outcome alone cannot distinguish deliberate rejection from omission.

## Commands and outputs

Use the existing Hermes Python environment for model executions.

```sh
python src/evaluation/evaluate.py --validate-only
python src/evaluation/evaluate.py --scenario-id scen_41_versioned_event_edit
python src/evaluation/evaluate.py --provider cohere --model command-a-03-2025 --scenario-id scen_41_versioned_event_edit --prices prices.json
python src/evaluation/evaluate.py --repetitions 3 --order-seed 17
```

`--output` is a new directory for each run. Run folders contain the frozen source,
per-case prefix/baseline/treatment reports and traces, the aggregate report,
`metrics.json` and `analysis.md`. A failed turn is an evaluated outcome. Runtime,
logging and worker errors are recorded separately and stop execution. Neutral
runtime directories are cleaned by the parent after the worker has exited and
filesystem evidence has been saved, avoiding Windows process-held log locks.

```sh
python src/evaluation/analyze.py eval_harness/runs/RUN_ID/report.json
python src/evaluation/audit.py eval_harness/runs/RUN_ID/report.json
python src/evaluation/check_reference_prefixes.py --scenarios scenarios --schema scenario.schema.json
```

The evidence audit independently checks API completion, tool callback coverage,
unmodified user messages, shared-history equality and recomputed usage/cost.
Reference-prefix validation uses actual registered tools without model inference.
Regression tests cover branch independence, failed prerequisites, metadata leaks,
typed answers, matched receipts, missing usage and shared-prefix cost accounting.
