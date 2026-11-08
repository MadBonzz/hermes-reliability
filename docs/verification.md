# Verification of the plugin branch

Verified on 2026-11-08 with Python 3.14.9 and Hermes source commit
`acdc8f1ba65c0231ae8277e846a4d55928913991`. The official security plugin was used as a reference for packaging and dashboard loading, not copied as the campaign engine.

| Check | Result |
|---|---|
| Unit and integration suite | **279 tests discovered: 265 passed, 14 skipped** |
| Ruff | Passed |
| Web tab, workbench, Desktop JavaScript syntax | Passed |
| `hermes plugins doctor … --ci` | Passed real discovery, manifest parsing, import and registration: four tools and one hook |
| `hermes plugins validate …` | Passed manifest, capability probe, dependency constraints, security scan, no-core-override check and Desktop SDK surface checks |
| Actual Hermes ASGI authentication | Campaign API returned 401 without auth and 200 with the Hermes session header; plugin static assets loaded through the host asset route |
| Actual CLI worker from dashboard API | Launched from a queued job, honored a queued stop, and persisted the stopped outcome |
| Named profile | Distinct campaign/job store, correct child-worker home and cancellation; job absent from the default profile |
| Chromium workbench | SDK request bridge, launch validation, disabled execution by default, persisted launches, saved generated scenario, three reviews, design-only findings and explicit saved-test selection |
| Frozen controller | Separate per-job copy, hashes checked before import and after completion; tampered controller rejected before inference |
| Removed benchmark data | No tracked `scenarios/`, `suites/`, or `examples/` payloads in the plugin working tree |

The browser used the **real Hermes ASGI server, auth middleware, plugin discovery, assets and backend router**, with a small React shell implementing the documented dashboard SDK contract. The Desktop file passed admission lint and syntax checks; the native Desktop application itself was not launched.

The end-to-end design campaign used a deterministic fixture model through the plugin's host-client interface. It exercised source inspection, scenario writes, mechanical validation, all three review contexts, immutable evidence, usage accounting and dashboard rendering. Existing tests separately exercise dataset discovery before generation with mocked catalogue responses, candidate/trusted execution, audit rejection cases and custom runtime contracts. Fixture outcomes are not measurements of a real target agent.

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

Only a new Cornell Enterprise branch is intended for publication. No public catalog PR, marketplace submission or hackathon announcement is part of this change.
