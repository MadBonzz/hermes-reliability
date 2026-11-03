# Source-grounded evaluation orchestrator

The Hermes starter campaign uses `muse-spark-1.3-contributor` for the main agent
and all three reviewer agents, and `command-a-03-2025` inside Hermes. Configure
exact endpoints, credential variable names, the Hermes interpreter and a pinned
GitHub revision in `orchestrator.example.json`. Credentials resolve from shell
variables followed by the repository-root `.env`.

## Campaign lifecycle

1. Fetch or verify the configured clean agent checkout. The inspected source and
   executed runtime use the same commit. Index its files for scoped source reads.
2. Create an editable evaluator workspace and a frozen original evaluator copy.
   Begin with an empty scenario directory. Existing benchmark scenarios and
   historical results are excluded from the starter inputs.
3. The main agent reads source and writes up to five initial scenario JSON files
   conforming to the repository's current benchmark schema. It may patch campaign-local evaluator
   code and add regression tests for new mechanisms.
4. Run mechanical validation and obtain three separate reviewer conversations.
   The roles cover user-task quality, experimental validity, and verification /
   attribution. Every role receives the same model configuration.
5. Feed findings back for revision. The maximum sequence is generation, review,
   revision, review, final revision, execution. Unanimous acceptance permits earlier
   execution. Outstanding findings remain recorded when the generation budget ends.
6. Execute the entire active suite twice, through the candidate evaluator and the
   frozen original evaluator. Both receive identical scenario bytes, target model,
   source revision and runtime settings, with independent executions.
7. Export cleaned transcripts and supply them to the main agent alongside execution
   outcomes. Raw reports and traces remain available through indexed reads.
8. The main agent analyzes every test, including successful, controlled and
   diagnostic trajectories, then decides whether to revise or stop. Subsequent
   suites may add, edit or remove tests and contain at most twelve active tests.
   Every revised suite goes through review again and every active test runs again.
9. Stop on an explicit decision that no further changes are warranted, or after
   five target-execution rounds. The stopping reason is saved explicitly.

An incomplete/invalid evaluator execution or a failed evidence audit prevents a
convergence claim. Completed runs can still contain weak oracles or false passes;
the mechanical gate does not establish semantic validity. The analysis must keep
material unverified requirements unresolved and use the configured scope, without
relabeling a full campaign as a smoke test to dismiss deficiencies.
Strict analysis validation also requires an unresolved entry with
`kind: execution_validity`, the affected `lane`, and a description for each
incomplete/invalid lane, even when the model declines convergence. This checks
that the gap is retained; it does not certify the truth of every narrative claim.
Native cleanup exceptions cannot be downgraded to warnings just because task
assertions passed; the evidence audit flags that mismatch with the validity policy.

Preflight validates authored prerequisite references. Authors may also supply a
`reference` on the final turn, including in a one-turn task: preflight then executes
the entire authored sequence. Each reference result reports `validated_turns`,
`total_turns` and `full_task_example`. This is a synthetic positive feasibility
example, not a target-model success or negative-case oracle validation. Ordinary
model execution and diagnostic continuation scoring remain separate.

`orchestrator_usage.json` retains orchestrator-only request, token and role totals
at the top level. Its `target_api_usage` field separately counts unique target
API requests across generation validation, lane preflight and execution, grouped
by stage and runtime phase. This includes DeerFlow auxiliary memory calls even
when preflight fails before task inference. Duplicate copied events are counted
once; incomplete usage and conflicting/malformed records remain explicit gaps.
The totals cover this campaign directory, not recovered ancestor directories or
independent probes. Analysis receives the totals available before its own call.

The twelve-test cap applies to the active suite in each round. Removing a test
frees a slot. Branches, diagnostic continuations and the two evaluator executions
increase execution count. The initial five-test cap applies throughout the first
review cycle.

## Review and validity

Review instructions contain the agreed design requirements, including
neutral presentation, natural requests, discoverable contracts, architecture
awareness, challenge exposure, intermediate effects, cost, and multidimensional
coverage. Reviewer issues have stable role-prefixed IDs and scenario references.
The final revision retains the preceding findings as unresolved review evidence.
Preflight feedback includes failing assertions and indexed access to its reports,
tool traces and worker logs before reviewers or the main agent revise the suite.
Authored-reference failures are test-construction evidence, not target-model
failures. Detailed evidence must guide reference repairs rather than weakening
the property checks to make validation pass.
Main and reviewer agents can use the read-only `probe` workspace action to try
valid and invalid synthetic files, service snapshots, calls and final responses
against a selected turn's trusted built-in oracles. Baseline and treatment probes
use their respective check contracts. Probes do not execute native tools, task
code or candidate Python. They record the scenario/checker hashes, actual results
and whether those results match the author's expectation. A changed/custom checker
still needs its own regression tests in preflight. Reviewers must assess whether
the proposed examples are semantically valid; a passing probe alone proves no
agent capability.
Review roles retain their own conversations across revisions within a round.
Later rounds can inspect indexed analyses from every earlier round. Generated
pairs place the selected fault in treatment and remove it in baseline.

Mechanical checks validate the fixed current benchmark schema and semantic constraints, Python
syntax, authored prerequisite executions through the selected runtime's real tools,
and campaign-local unittest files. Invalid scenario contracts stop execution.
An invalid candidate evaluator is recorded separately while a compatible original
evaluator can still execute the same scenarios. A custom capability unsupported
by the original evaluator appears as a validation/execution discrepancy.

Each evaluator lane receives an independent trajectory. Differences in outcomes
can also arise from different target decisions. The analysis must compare calls,
effects and assertions before assigning a discrepancy to evaluator implementation.
The trusted evidence audit checks captured exchanges, callback completeness, neutral
user messages, branch histories and recomputed efficiency where available.
Its phase count and recomputed token/call/time measurements are supplied directly
to the analyst. Missing dollar pricing does not imply missing usage. Analysis
instructions distinguish recorded gates from task validity and require checking
independent solution defects before calling a verifier rejection a false failure.

The campaign runner currently supports shared service-only prefixes. File-dependent
prefixes require a correctly implemented native-state checkpoint adapter. A pair
can instead begin at turn one with equivalent authored fixtures. Generated proposals
must describe this choice and any coverage gap. The frozen original evaluator
retains its own supported capability boundary.

## Persistent editing and evidence

The main agent edits `working/scenarios/*.json`, `working/src/evaluation/*.py` and
`working/tests/*.py`. Existing files use exact replacements guarded by the file's
expected hash. Reviewers have read-only file access. Schemas are fixed. Every
JSON write or replacement is parsed before committing it; malformed edits return
an error immediately and preserve the previous file, allowing correction within
the same generation. This does not certify schema or semantic validity.
Every
operation and its result is recorded. Review revisions are immutable snapshots
with file hashes. Executions use separate frozen copies, so later edits cannot
change a running revision.
Validation and execution compare revision hashes to detect source/contract changes
made by executed code. Each evaluator retains its own runtime capability checks.
Verification reviewers cannot approve modified `src/evaluation/checks.py` code without an
observed passing regression-test execution. Formatting/comment-only changes are
ignored by AST comparison. This is a minimum evidence requirement, not a proof
that the tests cover behavior: reviewers must still inspect valid/invalid examples,
including broken programs that contain the expected keywords. Missing evidence
must remain an issue. The third-generation no-review execution rule is unchanged.

Source and evidence batches retain each successful read even when another query
fails. Source errors direct the agent to the indexed upstream paths; evaluator
workspace reads use a separate envelope. Nested source/evidence commands inside
workspace actions are rejected instead of silently ignored. Reviewer instructions
require checking implementation semantics before reporting a verifier defect, and
require keeping computed solutions out of prompts when derivation is being tested.
Repeated reads of the same unchanged range receive an advisory after the third
observation within an exchange. The full content is still returned and recorded;
reads remain available and no request/output limit is imposed. Changed content or
a different range is tracked separately. This feedback affects the orchestrator,
not the evaluated agent's prompts or tool behavior.

Campaigns live under `eval_harness/orchestrator/<agent>_<timestamp>_<id>/`, with
each execution or resume stored in `attempts/<id>/`. Resuming an attempt within
this layout creates a sibling attempt in the same experiment. They include
source provenance, redacted API exchanges, action journals, proposals, reviewer
findings, validation evidence, snapshots, both execution lanes, cleaned transcripts,
analyses, usage accounting and checkpoints. `resume_initial_from` can recover an
unanswered read-timeout request during the first generation when no workspace
edits or completed generations exist. Recovery preserves the saved conversation,
source citations and evaluator copies in a new attempt, without replaying actions.
Recovery from an interrupted execution or analysis is not yet supported.

Interrupted model reviews can also resume with `resume_from`. This starts a new
attempt directory at the next generation of the same round, retaining
the authored suite, prior analyses, completed review findings and main-agent
conversation. The interrupted generation still counts toward the three-generation
limit; completed target rounds are not replayed. Original evidence and evaluator
copies stay unchanged. Recovery rejects changed source evidence, changed workspace
snapshots, changed target settings and exhausted generation budgets. The one-off
native-whitelist and Hermes turn-boundary repair modes are retired;
reproduce those historical recoveries with their frozen implementations.

Transient HTTP 408/429/500/502/503/504 and transport failures are retried up to five
times by default (`orchestrator.transient_retries`). Each attempt retains separate
request/response evidence; retry records include delay and cause. Numeric
`Retry-After` delays are honored. These are inference retries only: completed file
actions are never replayed. Authentication/contract failures stop immediately.

Transcripts retain user requests, fixture/fault events, unique returned reasoning,
actual tool arguments/results, state changes, native-check tables, verifier outcomes,
termination and costs. Built-in system prompts/tool definitions, empty content
blocks and repeated callback/message payloads are omitted. Scenario-introduced tool
definitions remain. Raw source links support inspection of the underlying evidence.
Provider responses are used when available to retain messages lost during history
compaction. Analyses use explicit component labels, including `agent_runtime`
(and the historical `hermes_runtime` label),
`mock_service`, `harness` and `verifier`, with evidence-linked attribution certainty.

The orchestrator accepts a single JSON object with optional surrounding prose or
Markdown fencing and preserves the provider's complete original response. Malformed
JSON receives explicit parsing feedback within a bounded exchange. Output-token
limits are supplied only when configured explicitly. Input and request caps are optional configuration. The default campaign applies
no additional token, input-character or model-request cap.
Orchestrator response reads have no timeout by default. `timeout_seconds` controls
connection, write and pool waits; `read_timeout_seconds` can explicitly set a read
timeout. Provider-internal limits still apply.

Execution of generated Python is authorized by `--execute`. Campaign path guards
scope the editor's file API. Generated evaluator code runs on the host; target
terminal/code tools run on the host for Hermes and in Docker for the native
container adapters described below. Source inspection and execution occur in
separate model contexts. Provider credentials are supplied through environment
variables and redacted in recorded API evidence.

## Native OpenHands and DeerFlow adapters

Set `adapter.kind` to `openhands` or `deerflow`, `adapter.python` to the harness
interpreter, and `adapter.runtime_image` to a locally built image. Both use the
same generation, three-reviewer, dual-evaluator and analysis loop. Configure
`orchestrator.model` as `meta-models/muse-spark-open-1026`, its endpoint as
`https://inference.baseten.co/v1`, and `api_key_env` as the environment variable
holding the Baseten key. Target configuration remains independent.

Build from a clean, pinned upstream checkout as the Docker context, using this
repository's `src/evaluation/agent_runtime.Dockerfile` with build arguments `AGENT` and
`SOURCE_REVISION`. Never use this benchmark repository as the build context.
The adapter verifies the image's framework/revision labels against inspected
source and freezes its image ID for the campaign.

OpenHands uses its native SDK `Conversation` with the CLI preset: terminal,
file editor, task tracker and finish. This profile does not enable the browser
or delegation. DeerFlow uses its native embedded `DeerFlowClient` and LangGraph
with planning, subagents, native filesystem/bash tools and memory enabled.
These profiles do not evaluate either project's hosted UI. Docker runs as a
nonroot user, with no network, host mounts, provider secrets or Docker socket.
External search/API behavior comes from scenario mock services. The host relays
only configured model inference and mock dispatches. Cohere's real responses
and usage are captured; the relay buffers HTTP/SSE responses before delivering
them to native clients, so it does not preserve token-stream arrival timing
inside the agent. No adapter output cap is added; native framework defaults
remain visible in the API evidence.
DeerFlow retains the pinned embedded client's default of 100 graph supersteps,
configurable through `adapter.recursion_limit` or `--recursion-limit`. Middleware
consumes these steps, so a turn can reach the limit after relatively few model
calls, even after emitting a final answer. Reports retain `recursion_limit`
termination separately from assertion outcomes. OpenHands uses SDK iterations.
These execution budgets are distinct from output-token limits; changes must be
recorded as runtime configuration changes.

Scenario mocks are extension tools, not native web-provider or MCP traffic.
DeerFlow's mock tools have no MCP metadata. Their `web` group does not by itself
activate middleware that selects remote tools by name or metadata. Inspect the
pinned source's routing before attributing a result to native retrieval defenses.

Before generation, fresh native campaigns inspect an empty container's actual
tool schemas with inference and mock dispatch disabled. Main and review agents
can read this indexed catalog alongside upstream source. DeerFlow's catalog uses
the compiled registry, including middleware and late-bound tools, rather than
only the base tool list. Introspection is skipped for incompatible older frozen
bridges, with the reason recorded; it must not claim to describe a different
runtime revision.

`{WORKSPACE}` maps to `/workspace` for OpenHands and
`/mnt/user-data/workspace` for DeerFlow. DeerFlow rejects ordinary files directly
under `/mnt/user-data`; that parent is not a valid task working directory.
Snapshots also retain DeerFlow's sibling uploads/outputs directories separately
without duplicating task files. Shared prefixes reject uncheckpointed auxiliary
files. Reference-tool name validation follows the selected runtime; Hermes tool
aliases are not added to native agents. Fixture paths expand the current benchmark workspace
placeholder before containment checks. Frozen older evaluator copies retain
their recorded behavior and require separate validity review for these defects.

Native shared prefixes retain the event tree or graph checkpoint, service state
and history. The portable boundary still excludes task files. DeerFlow's memory
is flushed, captured and restored before branching, a recorded checkpoint
intervention rather than an exact copy of asynchronous hidden state. Authored
reference actions execute through native tools; auxiliary memory inference can
still occur and is captured and counted. Supported context intervention is
zero-history truncation, not arbitrary native compression or process restart.
Cross-process deliberately racy file mocks and symlink assertion artifacts are
unsupported and rejected explicitly.

Native tasks may use `artifact_python` state checks with `name`, `python` and
optional `timeout_seconds` (default 30). After the turn, private Python assertions
run in a fresh container using the same pinned image and physical task path.
`WORKSPACE` names that path; `VISIBLE_WORKSPACE` records the agent's virtual path.
Only saved regular-file contents and directories are copied. Permissions, live
services, memory and sibling uploads/outputs are not cloned. Tests of those
properties need other evidence. Check code, results and errors never enter the
agent conversation; writes during verification affect only the disposable copy.
This provides behavioral verification, not a tamper-proof judge against code
deliberately attacking its verifier inside that container.

The verifier retains input, stdout/stderr, hashes, exit status, timeout and elapsed
verification time. An assertion/error/timeout fails the check; setup or evidence
capture failure invalidates evaluation. Exiting zero before the check completes
does not pass. The same checks validate authored references. Hermes and the
host-only `probe` action reject this check explicitly; generated program code
must not be executed on the host to work around that boundary. Positive references
should be supplemented with isolated invalid implementations, including comments
containing expected strings, wrong return values and swallowed subprocess errors.
Existing frozen campaign revisions do not acquire this capability retroactively.

The DeerFlow profile currently uses its native default 30-second memory flush
budget. New runs can explicitly configure `adapter.memory_flush_timeout` (positive
integer seconds), forwarded as `--memory-flush-timeout` for both preflight and
execution. This changes the recorded drain deadline at checkpoint, restore and
shutdown, not the task prompt, memory enablement or output token budget. It is a
runtime configuration change and must be reported when comparing runs; an older
bridge that cannot apply a nondefault deadline is rejected. An incomplete flush
still invalidates checkpoint/cleanup evidence; a completed
HTTP response alone does not establish that the memory update finished in time.
Reports retain `status_before_cleanup` and nested cleanup exception details, and
reference feedback and transcripts expose them separately from turn assertions.
A successful reference gate followed by a failed memory flush remains an invalid
phase; it does not establish a bad authored output contract or a target-model
task failure. This strict harness validity rule is distinct from the upstream
Gateway's best-effort shutdown warning policy.

Tool callbacks retain arguments/results and before/after files and service state,
including failed native turns. Internal OpenHands/DeerFlow guard call sites are
not instrumented; absence of guard events is unobserved coverage. Native failures,
harness errors and assertion failures remain separate. The runtime capability
profile is supplied to the main agent and reviewers so generated tests do not
assume Hermes tools or enforcement.
Fixture and assertion paths are relative to the declared task root. Native tool
arguments use that root's absolute path when their interface requires it. A
literal `workspace/` prefix is a nested directory, not an alias to be removed.
New check diagnostics preserve full relative paths; older basename-only messages
must be interpreted alongside the frozen assertion and snapshot keys.
DeerFlow model-call wrappers also record proposed tool calls before tool execution.
Native guards can reject a proposal before the execution callback runs. Process
checks count these observed attempts; efficiency reports distinguish them from
executed callbacks. Missing callbacks alone do not establish a refusal: inspect
the native tool response, retained in readable transcripts even without a callback.
Raw attempt counts are independently audited against the report. The wrappers
return native responses unchanged and add no graph steps. Other middleware may
suppress proposals outside this observation point; internal guard coverage remains
unobserved. Frozen older campaigns do not acquire these events retrospectively.
DeerFlow delegation callbacks extract the matching native tool message from a
LangGraph command while retaining the original command in raw evidence. Native
integration tests exercise child tool calls and both positive and negative
acceptance verdicts; task completion and acceptance remain separate outcomes.
Efficiency summaries retain native error flags and distinguish tool errors,
nonzero command exits and timeouts. A call with overlapping signals is counted
once in `failed_tool_results`; a nonzero exit can be intentional and is not itself
a scenario failure. No failure is inferred merely from error-like words in text.
Frozen older campaigns retain their original measurement behavior.

The current benchmark CLI accepts `--agent-kind openhands|deerflow --runtime-image <image>`.
Native integration tests use `NATIVE_AGENT_TEST_IMAGE` and
`NATIVE_AGENT_TEST_KIND` with `python -m unittest tests.test_agent_container`;
without these settings, the Docker integration test is skipped. Live provider
and campaign checks remain distinct from the deterministic native tool test.

## Commands

Copy the example configuration to an ignored local location and fill in the Hermes
Python interpreter. An optional `source.local_path` may point to a clean Git checkout
whose origin and HEAD match the configured repository and revision.

```powershell
uv run --locked python -m src.orchestrator --config eval_harness/orchestrator_config.json
uv run --locked python -m src.orchestrator --config eval_harness/orchestrator_config.json --execute
uv run --locked python -m src.orchestrator --config eval_harness/orchestrator_config.json --smoke --execute
```

Generation-only execution checks schema and syntax and stops after preparing the
reviewed files. Executing generated tests and native reference actions requires
`--execute`.
Smoke mode limits the campaign to one small two-turn test and one execution round,
including both evaluator lanes and all applicable reviewer passes.

Offline verification uses `python -m unittest tests.test_orchestrator -v`. A live
smoke campaign is required to verify actual provider response parsing and installed
Hermes integration. Full-suite property coverage and empirical convergence remain
separate from smoke-test completion.

Implementation is split between `src/orchestrator/` and `src/evaluation/`.
All authored model instructions are in [prompts.py](src/orchestrator/prompts.py),
including generation, the three reviewer roles, analysis, smoke scope and repair
feedback. The coordinator imports these definitions. Campaign-local evaluator
workspaces contain `src/evaluation/` and its package initializer, not the
orchestrator. Editable Python paths are `src/evaluation/*.py` and `tests/*.py`.
Revision hashes and source snapshots include nested evaluation modules.
