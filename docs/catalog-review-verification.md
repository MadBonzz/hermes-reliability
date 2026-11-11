# Catalog security review verification — November 11, 2026

This change addresses the review of `215d08d68243f67862d8215df73cfd39e569151c` on [Hermes PR #153531](https://github.com/NousResearch/hermes-agent/pull/153531#pullrequestreview-5865918659).

## Changes and regression checks

| Review item | Change | Verification |
| --- | --- | --- |
| Child environment inheritance | One explicit runtime allowlist replaces full inheritance and substring blocklists in preflight and execution. Phase settings and the target credential are added explicitly. | Real subprocesses for each built-in adapter reject unrelated provider keys, passwords, secrets, connection strings, cookies, Python import hooks and approval flags. Hermes preflight has no target key; container preflight and execution receive only their explicit target credential. |
| Wrong-profile credential lookup | The owning worker's supplied target key is carried in memory to generation preflight, version-comparison adapters, execution and fixed regression. Findings threads capture explicitly supplied credentials/client before starting. The controller has no ambient-environment fallback. Misleading `.env` path arguments are removed. | A conflicting ambient target key cannot replace the supplied key. Findings use captured profile values even after the caller's dictionary changes. Existing worker tests ensure it does not load repository credential files. |
| Local source reads | Source submission and worker preparation call Hermes's shared read guard and reject home/profile paths and aliases. Source inventories and snapshots exclude credential stores and browser profiles. Snapshot reads recheck the host guard. | Design-only denial tests cover home, Hermes home, nested paths, ancestors, symlinks and host-policy denials. Snapshots retain ordinary code but omit credential fixtures and host-blocked files. The live host check confirms protected-path denial and no credential canaries in copied source or model/evidence artifacts. |
| Persistent approvals | Approval keys hash source identity, requested revision, runtime and run mode. Local paths are resolved; distinct paths such as `project` and `project.git` remain distinct. | Repeated identical requests reuse the key; changing source, revision, runtime or mode changes it. Design-only requests do not request execution approval. |

These regressions were also run against the reviewed base; the new boundary assertions reproduce its environment leaks, protected-source acceptance and shared approval key. The revised implementation passes them.

## Completed checks

- Full offline suite: **301 tests run; 14 explicit skips; no failures**.
- Targeted security suite: five tests, including subprocess checks for all three built-in adapters; passed. Re-run after the final approval-identity correction; passed.
- Ruff, Python/JavaScript syntax checks and Git whitespace checks: passed.
- Native Hermes Plugin Doctor: passed, eight tools and one hook.
- Native plugin admission: all **16** checks passed. The host shared file-safety guard is an additional compatibility dependency in the undocumented-import warning; no core functions are modified. See [runtime integration](runtime-integration.md).
- Native Hermes execution and frozen regression with a deterministic local model: passed. The initial benchmark passed both controlled phases; the subsequent deliberate wrong-answer change was correctly classified as **two regressions**, with no invalid phases. This exercises real target tools, preflight, the CLI worker and evidence auditing without paid target inference.

## One live Muse Spark Contributor smoke test

The changed plugin ran in an isolated Hermes profile through the real CLI worker and `ctx.llm`, using **`muse-spark-1.3-contributor`**. The source was a small local invoice-assistant fixture with credential canaries alongside its code. This was one design-only scenario, one round and one generation, bounded to twenty requests; no dataset discovery or target execution was requested.

The run completed as **`generated_only`** after **10 API calls**. It generated one scenario and a completed proposal, and passed schema and Python-syntax validation. All ten provider responses returned HTTP 200. Provider-reported usage was 245,683 input tokens and 15,042 output tokens. No provider cost estimate is asserted.

Only `agent.py` and `README.md` were copied from the source fixture. Credential canaries in `auth.json`, `config.yaml`, `.netrc` and `.config/` appeared in neither the snapshot nor any plugin model/evidence artifact. The real host guard rejected home/profile selections.

This one-generation design check did **not** invoke the independent reviewers or execute a paid target; the existing pipeline's last allowed generation proceeds directly to its terminal design result. Independent review behavior is covered by the deterministic integration suite, and native target execution by the separate regression check above. This is not a claim of a complete autonomous live campaign. The final approval-identity suffix-collision correction and additional browser-folder exclusions followed the live snapshot and were verified separately by the targeted security suite; neither changes the observed design-only fixture result.

All test inference has stopped. Private raw logs and credentials are outside the repository; only this verification report is published. Live OpenHands/DeerFlow containers and Windows behavior were not re-tested in this review.
