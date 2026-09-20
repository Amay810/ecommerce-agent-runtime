# Current status

This file is the single short status reference for the runtime checkout. The
last implementation freeze before this cleanup was `0bb1952`; cleanup commits
may be newer without changing the frozen scoring or model experiment results.

## Supported path

- `HarnessRunner` + `RulePolicy` for deterministic CPU wiring and tests;
- `NativeToolPolicy` with local Qwen3-4B-Instruct-2507 for the real-model path;
- typed retail tools, SQLite state, identity/eligibility/confirmation checks,
  idempotent writes, evidence and trajectory replay;
- optional hybrid retrieval and optional MCP exposure through the same
  `RetailTools.call` boundary;
- optional external Tau3 integration under `scripts/` and `nscc/`.

## Verified facts

- The latest CPU evidence is `291 passed, 2 warnings`; it includes MCP
  contract tests and does not call a model or GPU.
- The message-source fix is in `native_tool_policy.py`: non-empty history is
  the only message source, tool results stay `tool`, repeated user text stays
  a user event, and reconstructed tool-call/result IDs remain paired.
- The fix's effect on Qwen action selection has not been measured. The next
  allowed model check is a single-step before/after comparison using existing
  exploration context and fixed parameters.

## Experiment boundary

The return-closure v2 freeze and deterministic reports under
`docs/experiments/` are historical artifacts. The real Qwen exploration and
the rejected Skill v1 validation were run before the message-source fix; do
not reclassify them as post-fix results. Skill v0 remains active. No new
validation or locked run belongs to this cleanup.

Rule/Oracle runs are environment checks, not model or Skill-effect evidence.
Credentials, model weights, retrieval indexes, caches, SQLite logs and raw
trajectories stay outside Git.
