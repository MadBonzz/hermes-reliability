# Verification of the plugin branch

For the November 11 security changes and their targeted, native-runtime and live Muse checks, see [catalog review verification](catalog-review-verification.md).

Verified on 2026-11-08 with Python 3.14.9 and Hermes source commit
`acdc8f1ba65c0231ae8277e846a4d55928913991`.

| Check | Result |
|---|---|
| Unit and integration suite | **294 tests discovered: 280 passed, 14 skipped** |
| Ruff | Passed |
| Web tab, workbench, Desktop JavaScript syntax | Passed |
| `hermes plugins doctor … --ci` | Passed real discovery, manifest parsing, import and registration: eight tools and one hook, plus the bounded baseline/memory system-prompt section |
| `hermes plugins validate …` | Passed manifest, capability probe, dependency constraints, security scan, no-core-override check and Desktop SDK surface checks |
| Actual Hermes ASGI authentication | Campaign API returned 401 without auth and 200 with the Hermes session header; plugin static assets loaded through the host asset route |
| Actual CLI worker from dashboard API | Launched from a queued job, honored a queued stop, and persisted the stopped outcome |
| Named profile | Distinct campaign/job store, correct child-worker home and cancellation; job absent from the default profile |
| Chromium workbench | SDK request bridge, launch validation, disabled execution by default, persisted launches, saved generated scenario, three reviews, design-only findings and explicit saved-test selection |
| Frozen controller | Separate per-job copy, hashes checked before import and after completion; tampered controller rejected before inference |

The browser used the **real Hermes ASGI server, auth middleware, plugin discovery, assets and backend router**, with a small React shell implementing the documented dashboard SDK contract. The Desktop file passed admission lint and syntax checks; the native Desktop application itself was not launched.

The end-to-end design campaign used a deterministic fixture model through the plugin's host-client interface. It exercised source inspection, scenario writes, mechanical validation, all three review contexts, immutable evidence, usage accounting and dashboard rendering. Existing tests separately exercise dataset discovery before generation with mocked catalogue responses, candidate/trusted execution, audit rejection cases and custom runtime contracts. Fixture outcomes are not measurements of a real target agent.

## Version 0.2: frozen regression and native memory

The regression workflow was tested with **real Hermes runtime processes, native
file tools, the actual plugin CLI worker, and a local deterministic Chat Completions
fixture server**. No paid provider inference was used for these additional checks.

- Executed an operator-authored controlled file-reading pair, audited both phases,
  and froze its trusted evaluator/scenarios as a content-addressed benchmark.
- Launched `mode=regression` through the plugin's detached Hermes CLI worker. The
  fixture server deliberately gave wrong answers on the rerun. Both phases were
  correctly labeled regressed, with matching benchmark hashes, passing evidence
  audits and zero generation/model-review calls.
- Exercised the real Hermes agent loop with plugin tools routed through native
  `tool_call`. `reliability_memory` returned a verified reference; the native
  `memory` tool then confirmed an applied write. A **fresh Hermes session received
  both IDs through its native memory context**. The plugin never directly opened
  or wrote the memory file.
- Chromium exercised the actual authenticated Hermes ASGI backend and existing
  SDK test shell: saved history, freeze, baseline selection, two regression rows,
  memory-note handoff, fixed-run rendering, and rejection without execution opt-in.
  Unauthenticated history and mutation requests were rejected. No browser errors.
- Focused tests cover new benchmark versions, preserved assertions, configuration
  mismatch, invalid/empty coverage, failed audits, evidence/evaluator tampering,
  legacy provenance, concurrent history writes, cross-profile/stale IDs, linked
  paths, explicit execution permission and cooperative cancellation.

The native check also exposed an unauthenticated-target issue: an empty API key
caused Hermes to resolve an ambient provider despite an explicit local endpoint.
The target harness now supplies a nonsecret placeholder when no target key is
configured; explicit credentials are preserved. This path passed the native test
and a dedicated regression test.

These fixtures verify the software workflow, including actual native memory
persistence, but do not establish model reliability or complete the outstanding
autonomous paid-provider campaign below. Desktop passed admission and JavaScript
syntax checks; the native Desktop application was not launched. Memory staging,
capacity and alternate providers remain governed by Hermes; the plugin returns a
handoff rather than claiming that those writes succeeded.

## Live Cohere Command A verification

Additional live testing used `command-a-03-2025` at
`https://api.cohere.ai/compatibility/v1`, with the same pinned Hermes revision above.
The model served both the plugin's host-owned orchestration calls and a separately
configured Hermes target. Credentials stayed outside the repository.

| Check | Observed result |
|---|---|
| Model availability and inference | Cohere listed the model and returned a successful Chat Completions response |
| Native CLI worker and dataset retrieval | Real background jobs loaded the plugin through Hermes; AssistantBench supplied four user excerpts and WebLINX two, before generation |
| Initial autonomous campaign | Stopped with a parser exception after four model calls; multiple JSON objects did not enter the controller's repair loop |
| Corrected autonomous campaign | Parser repair worked; stopped by the operator after 12 calls repeatedly failed the scenario-writing contract. No valid scenario, reviewer verdict, target execution or convergence was claimed |
| Separate live target smoke | One operator-authored file-reading scenario, baseline plus changed input, executed twice through the candidate/trusted adapter paths: **four of four phases passed**, with real `read_file` calls |
| Target evidence | Both execution evidence audits passed with no errors; evaluator inputs remained unchanged |
| Current Hermes integration check | Passed native Python/pip/uv phase separation, file-tool guard, control observation, mock restoration, workspace isolation, and streamed HTTP/tool evidence capture |
| Chromium and actual Hermes ASGI | Showed the live failed/stopped campaigns and dataset samples without browser errors; unauthenticated API returned 401 and authenticated campaign detail returned 200 |

The target smoke was authored by the operator solely to isolate transport, native
tool execution and evidence collection. Both executions used the same unchanged
evaluator; there was no model-authored candidate change. It is **not** a completed
autonomous, dataset-grounded campaign or a general reliability result. Smoke inputs
and run artifacts remain outside the plugin repository.

Two fixes resulted from this test: malformed multi-object host responses now raise
the parse-error type handled by the bounded repair loop, and target profiles activate
their disposable Python environment through Hermes's shell-init setting after login
shell initialization. Neither fix changes scenario assertions or evidence acceptance.

Recorded usage: orchestration consumed 332,345 input and 16,585 output tokens across
16 calls; the separate target smoke consumed 71,422 input and 199 output tokens across
eight requests, with no target API errors. No billing estimate is asserted. Direct
model-availability and connection probes are separate from these counts.

Runtime setup used Hermes core dependencies without `browser-harness`: upstream
Hermes pinned Pillow 13.0.0 while `browser-harness==0.1.13` required Pillow 12.3.0.
Live browser automation was therefore not tested. The web dashboard used Chromium
through the existing SDK test shell described above.

**Limits:** a fully autonomous live campaign has not yet completed. Command A worked
as the Hermes target in the bounded smoke but did not reliably author the required
scenario/action contract in this test. No live OpenHands/DeerFlow execution was run.
Eleven tests require native Docker runtimes and three require Windows-specific behavior.
Hermes admission lint warns about private imports in the retained, separately executed
target harness; those runtime-version dependencies are documented in
[runtime integration](runtime-integration.md). A passing admission check does not remove
that warning or establish public catalog acceptance.

## Release verification

On 2026-11-11 the offline suite was rerun: **294 tests, 280 passed, 14 skipped**.
Ruff, JavaScript syntax checks for the web tab, workbench and Desktop entrypoint,
and Git whitespace validation passed. These checks used no paid model calls.

Catalog admission was also checked against Hermes upstream commit
`77e16d5f0f69328003adc3dd91e5f36823df982e` using an isolated runtime and profile:

- `hermes plugins validate /path/to/plugin --install-deps --json`: all 16 checks
  passed, including dependency preparation, capability declarations, install
  security scan, core-override checks and Desktop SDK checks.
- `hermes plugins doctor /path/to/plugin --ci`: discovery, imports and
  registration passed; eight tools and one hook registered.
- The validator reported the documented private-runtime-import warning for the
  separate target evaluation harness. This warning remains subject to maintainer
  compatibility review; a local pass is not catalog acceptance.

The dashboard screenshot shows the deterministic regression test described above.

## Final submission audit: 2026-11-11

A fresh Hermes profile installed the public repository at
`dd2fd0580b0c25292edf97460ffe851e7838a101` using the documented Git-URL install
flow. Installation, enabling, Plugin Doctor, and all 16 admission checks passed
against Hermes `77e16d5f0f69328003adc3dd91e5f36823df982e`.

- Chromium loaded the installed plugin through the real Hermes ASGI backend and
  the SDK test shell. Authentication, rejected credential-bearing repository URLs,
  execution opt-in, and saved campaign rendering passed without JavaScript exceptions.
- Native deterministic regression testing again froze an audited benchmark and
  correctly reported two regressed phases. The native memory tool saved the
  baseline reference, and a fresh Hermes session received both IDs.
- A response-handling audit reproduced crashes on null rationale rows and
  non-string review issue IDs, plus unhelpful errors on object-shaped citations.
  These now produce actionable validation feedback inside the existing repair loop.
  The updated suite has **296 tests: 282 passed, 14 skipped**. Ruff and admission
  checks pass. The existing target-runtime private-import warning remains.

The live orchestration check used **Muse Spark 1.3 Contributor through Hermes's
native CLI worker and `ctx.llm`**, with real AssistantBench and WebLINX retrieval.
It was bounded to one scenario, one execution round, up to three generation/review
passes, and a shared 24-request Muse ceiling. The first attempt used 13 requests
on inspection and hit the test relay's cumulative input limit. One additional
request belonged to an operator-stopped test-relay restart. A focused retry used
the remaining 10 requests, authored a scenario, submitted its proposal, and passed
schema, Python syntax, and native preflight validation. The shared request ceiling
then blocked the first reviewer call. No reviewer verdict or target execution was
completed, and no autonomous end-to-end success is claimed. All 24 dispatched
provider requests returned HTTP 200 with `finish_reason=stop`; the stopping limits
were local test-budget guards, not provider rejection or output truncation.

The focused retry is a functional generation/preflight result, not a scenario
difficulty or reliability benchmark. The live check used the published revision
above; the malformed-response fixes were separately verified by repair-loop tests.
Fully autonomous live campaigns, native Desktop rendering, Windows behavior and
live OpenHands/DeerFlow execution remain outside the verified results here.
