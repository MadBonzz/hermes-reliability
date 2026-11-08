# Verification of the plugin branch

Verified on 2026-11-08 with Python 3.14.9 and Hermes source commit
`acdc8f1ba65c0231ae8277e846a4d55928913991`. The official security plugin was used as a reference for packaging and dashboard loading, not copied as the campaign engine.

| Check | Result |
|---|---|
| Unit and integration suite | **278 tests discovered: 264 passed, 14 skipped** |
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

**Limits:** no paid provider inference or live Hermes/OpenHands/DeerFlow target campaign was run in this environment. Eleven tests require native Docker runtimes and three require Windows-specific behavior. Hermes admission lint warns about private imports in the retained, separately executed target harness; those runtime-version dependencies are documented in [runtime integration](runtime-integration.md). A passing admission check does not remove that warning or establish public catalog acceptance.

Only a new Cornell Enterprise branch is intended for publication. No public catalog PR, marketplace submission or hackathon announcement is part of this change.
