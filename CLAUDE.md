# CLAUDE.md — Agent Runtime Working Agreement

This repository is the standalone guarded e-commerce Agent Runtime. Keep
changes within its current scope: provider-facing orchestration, typed retail
tools, SQLite state, retrieval support, trajectory replay, and offline audits.

## Scope boundaries

- Do not add GRPO, VERL, verifier, contrast, or training features here.
- External Tau2/Tau3 evaluation is separately pinned and is not vendored.
- Retrieval is a supporting capability; runtime state and transaction safety
  remain the primary project boundary.

## Validation rules

- Run CPU import, compile, and test checks before reporting completion.
- Distinguish deterministic contract smoke from retrieval evaluation and from
  model-backed or cluster execution.
- Do not claim model quality, training success, or external-runtime validation
  from local CPU checks.
- Record failures and their exact scope; do not make a command appear green by
  changing the acceptance criterion.

## Repository hygiene

- Keep logs, indexes, model files, credentials, and raw trajectories out of
  version control.
- Use `TAU_ROOT` or an explicit CLI argument for external Tau2 checkouts.
- Do not introduce developer-machine absolute paths as operational defaults.
- Preserve evidence provenance, but label historical or external artifacts
  clearly.
