# Exploratory evaluation orchestrator

This is a separate, bounded generate–review–execute–analyze loop. It evaluates the
**complete agentic system**, using component attribution to explain outcomes. It
does not modify the existing harness, authored scenarios, historical analyses or
scores. All campaign artifacts go to a fresh `eval_harness/orchestrator/<uuid>/`.

The first execution adapter reuses the current Hermes harness in a child process.
The core has no Hermes imports. Other systems use a trusted command adapter with
their own scenario schema and execution implementation. Arbitrary frameworks do
not automatically acquire tool/fault support from an architecture diagram.

## Start a campaign

Copy `orchestrator.example.json` to an ignored local path such as
`eval_harness/my_orchestrator.json`. Run commands from the repository root. Supply:

- Separate exact orchestrator and target model IDs and API roots. They may be the
  same. Calls are sequential: planning completes before target execution starts.
- Environment variable names for credentials, not literal keys. Set these in the
  calling shell. This new entry point deliberately does not read repository `.env`.
- The Hermes interpreter and checkout, or a command adapter. The interpreter must
  already have Hermes plus harness dependencies installed.
- Architecture descriptions, past analysis/report paths in `documents`, diagrams
  in `diagrams`, and scenario files/directories in `past_scenarios`.
- A scope and explicit cycle/request/scenario budgets.

Diagrams are sent as image inputs; the orchestrator endpoint must support images.
For text-only endpoints, provide a human-checked architecture/data-flow description
instead. It does not silently pretend to have inspected omitted diagrams.

```powershell
uv run --locked python -m src.orchestrator --config eval_harness/my_orchestrator.json
```

This contacts the orchestrator model and generates a reviewed, schema-validated
suite, but does not run the target. It still consumes API usage.

```powershell
uv run --locked python -m src.orchestrator --config eval_harness/my_orchestrator.json --execute
```

This starts a fresh campaign and runs generated tests after validation. It does
not resume or execute the exact proposal from a previous generation-only campaign.
For exact reviewed-proposal execution, use the existing runner on that saved
`cycle_001/scenarios/` directory. Resume support is not implemented.

`--execute` authorizes local generated tasks, including reference actions. These
can use shell/code through the target. Neither this loop nor the existing file
wrapper is an OS sandbox. Use an isolated host/account when running untrusted
generated workflows. A timeout stops the adapter's direct process; descendant
process termination and OS containment are not guaranteed.

## What each cycle does

1. Supplies the adapter schema, declared capabilities, diagrams, evidence index,
   prior scenario definitions and prior cycle conclusions to the planning model.
   It can read indexed evidence in explicit character ranges; partial chunks are
   labelled and include hashes and offsets. No unrestricted filesystem tool is
   exposed to the orchestrator model.
2. Requests new scenarios with property/architecture rationale, novelty, challenge
   triggers and oracle justification. All generation stays declarative. New mock
   behaviors or perturbation mechanisms outside the adapter become capability
   gaps, not invented Python implementations.
3. Uses another model call to critique the proposal, then validates the schema,
   paths and existing Hermes semantic constraints. Exact duplicates are detected
   ignoring ID/title changes. Semantic novelty and oracle adequacy still depend on
   review; a same-model critique is not independent verification. A rejected
   proposal feeds the next cycle. Invalid schemas stop with retained evidence.
4. Starts a fresh adapter process with explicit target model/endpoint and its key.
   Hermes registers mocks, applies perturbations, runs checks, and records its
   usual causal/API evidence and diagnostic continuations. Completed failed tests
   proceed to analysis; execution/harness errors stop the campaign.
5. Requests a cited analysis of every generated scenario, including passes. The
   analysis distinguishes task outcome, challenge exposure, property outcome,
   observed versus inferred contributions and unresolved evidence gaps. Citations
   must refer to inspected chunks and include current execution evidence. This
   validates provenance, not the semantic truth of a model's causal claim.
6. Supplies that analysis to the next planning cycle, allowing broader coverage,
   deeper failures and discriminating follow-ups. It does not replace raw scores.

The analysis reader can inspect supplied historical report files. To inspect their
full associated logs, explicitly list those trace files in `documents` as well.
New adapter runs automatically index their returned evidence. The model is not
guaranteed to exhaust all indexed data within the request budget; unresolved
coverage must remain explicit. File changes after indexing are rejected.

## Stopping and convergence

Hard limits bound cycles, proposals per cycle, model requests, input characters,
request timeouts and execution timeouts. Output tokens are capped only if you
explicitly configure `orchestrator.max_output_tokens`; otherwise the request omits
`max_tokens` and the provider's default applies. These bound work but do
not implement a dollar-cost cap. Generation-only and adapter capability gaps have
distinct stopping reasons. Errors retain the campaign and evidence index.

Two or more consecutive reviewed empty proposals with no reported capability,
review or prior-analysis uncertainty gaps produce an
`empirical_saturation_candidate_not_exhaustive` stop. This is a model-assessed
saturation signal within the supplied scope and evidence, **not proof that no
properties remain**. Budget exhaustion is never labelled convergence. Unresolved
findings are conservatively retained; automatic resolution/closure of a finding
ledger and statistical convergence across repeated runs are future work.

## Evidence and isolation boundaries

Each campaign preserves configuration (credential variable names only), diagram
hashes, input evidence hashes, proposals, reviews, generated scenario files,
execution logs, report/trace evidence, analyses and cycle checkpoints. The model
client saves redacted requests and full nonstreaming responses, including returned
usage/reasoning fields, and transport errors. It does not request extra verbosity
from the target or modify the target prompt. Generated scenario prompts naturally
differ from previous benchmark prompts.

Known keys are redacted from orchestrator API records. Synthetic task secrets are
retained. Subprocess stdout is native diagnostic output, not guaranteed sanitized;
all campaign files remain private. A hosted orchestrator receives the supplied
documents, diagrams and requested evidence chunks. Choose local hosting if those
must not leave the machine. Existing source/evidence is read, never rescored or
rewritten by this layer. Hermes may create additional fresh private run directories
through its existing lifecycle; the bridge copies their traces into the campaign.

## Adding an agent system

Configure `adapter.kind` as `command`, `adapter.schema` as a local Draft-7 JSON
schema for one scenario (including a path-safe `id`), and `adapter.command` as an
argument array for a trusted executable. No shell is used. The orchestrator adds:

```text
--orchestrator-request <request.json> --orchestrator-result <result.json>
```

The request contains `scenarios` (directory), `target` (model/endpoint/config) and
`output_directory`. Credentials are supplied as `EVAL_API_KEY`; model and endpoint
also appear as `EVAL_MODEL` and `EVAL_BASE_URL`. The command must implement the
target-specific mock registration, perturbations, fresh state, execution, checks
and causal capture. Keep evaluator controls distinguishable from native controls.

On a completed evaluation, including failed scenario assertions, exit zero and
write this manifest. Evidence paths are relative to the cycle directory and must
resolve to files within it:

```json
{"status": "complete", "evidence_files": ["execution/report.json", "execution/events.jsonl"]}
```

For useful attribution, return prompts, ordered calls and results, before/after
effects, fault timing, verifier definitions/outcomes, termination data and observed
guard events. Explicitly document unobserved controls. Nonzero exit, missing
evidence or incomplete status stops the loop rather than inventing conclusions.

Only Hermes has an implemented target bridge here. Generic command support is an
extension contract, not verified integration with other agents.

## Verification and present limits

Offline regression tests cover the two-cycle loop with fake components, evidence
ranges/hashes, cited-analysis validation, schema/path rejection, model separation,
API logging/redaction/budgets and completed-failure handling by the Hermes bridge.
They do not establish live model generation quality or a real Hermes campaign.
Schema/model review does not prove an oracle sound; reference actions are validated
by the existing harness when a diagnostic prefix executes, not proactively for all
generated prerequisites. No automatic source-code generation, framework patching,
unbounded recursion, parallel campaign scheduling or automatic resume is included.
