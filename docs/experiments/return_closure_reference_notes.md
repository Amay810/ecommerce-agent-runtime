# Return-closure implementation references

These repositories were inspected as local design references only; no source
code or runtime dependency was copied into this repository.

* DeerFlow: the public `main` tree exposes `.agent/skills` and `skills/public`.
  The inspected skill export implementation was commit
  [`f52818fe5ec00174691911fc6d89c4ad617027b1`](https://github.com/bytedance/deer-flow/commit/f52818fe5ec00174691911fc6d89c4ad617027b1), especially
  `backend/packages/harness/deerflow/skills/{export.py,installer.py,projection.py,validation.py}`.
  The main-branch history also showed `5727449` on 2026-09-11. We used only the
  idea of a file-scoped, versioned Skill package.
* Recuris: the public `main` tree exposes `skill_memories/`, `src/recuris/`,
  `scripts/`, `splits/`, and `integrity/`. The inspected main-branch history
  showed commit `7d3745a` on 2026-08-30; its README describes structured
  failure traces and paired held-out admission. We used only those evaluation
  boundaries, not its benchmark or implementation.

This project remains a single-agent runtime with explicit configuration and a
small local Skill file; it does not claim to reproduce either project.
