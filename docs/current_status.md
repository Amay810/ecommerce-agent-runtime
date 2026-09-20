# Current status

This is the short status reference for the checkout. The canonical map is
`docs/architecture.md`; environment identities and commands are in
`docs/reproduction.md`.

## Code and supported path

- Local cleanup baseline: `ecb3e3d` (`chore: streamline runtime handoff and dependencies`).
- Frozen return-closure scoring baseline: `0bb1952` and its v2 contract history;
  cleanup commits do not rewrite those artifacts.
- Main path: `HarnessRunner` + `NativeToolPolicy` + `RetailTools.call`, with
  SQLite, optional hybrid retrieval, evidence and typed confirmation.
- CPU smoke path: `RulePolicy`; it is deterministic wiring validation, not
  model or Skill evidence.
- Optional paths: legacy `LLMPolicy`, MCP façade and external Tau3 wrappers.

## Evidence

- **Executed, local host**: `.venv/bin/python -m pytest -q` at `ecb3e3d`:
  `291 passed in 6.95s`.
- **Executed, local host**: four-task contract smoke with `RulePolicy` reached
  1.0 task success, policy compliance and terminal-state accuracy; this is CPU
  wiring only.
- **Historical executed, AutoDL CPU**: `291 passed, 2 warnings` after the
  message-source files were applied to the older checkout. The final AutoDL
  alignment is recorded separately and does not turn this into Qwen evidence.
- **Static/targeted**: `7b52e80` and its tests preserve message provenance:
  non-empty history is the sole source, tool results stay `tool`, repeated
  user text stays a user event, and reconstructed tool-call/result ids remain
  paired.
- **Historical model evidence**: earlier Qwen exploration and rejected Skill
  v1 validation ran before this fix. They remain historical and must not be
  re-labelled as post-fix results.

## Environment now

- Local host: usable CPU Python/uv environment; Docker is installed but the
  current user cannot access `/var/run/docker.sock`.
- AutoDL: reachable CPU container with the Qwen virtualenv and model files;
  `nvidia-smi` reports no devices and the local vLLM endpoint is stopped by
  design in this cleanup.
- GitHub Actions: no `.github/workflows` is present in this checkout; no CI
  execution is claimed.

## Experiment boundary

Skill v0 remains active; Skill v1 was rejected historically. No validation,
locked run, or real-Qwen message-source-effect comparison is part of this
cleanup. Credentials, model weights, indexes, caches, SQLite logs and raw
trajectories remain outside Git.
