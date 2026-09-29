---
name: run-evidence
description: Build a concise provenance card for local-to-AutoDL handoffs, new experiment conclusions, artifact freezing or transfer, or genuinely ambiguous evidence identity. Do not use for ordinary local code edits, unchanged reliable records, or generic documentation cleanup.
---

# Run Evidence

Use this skill only for an evidence-boundary event. For an ordinary local code change, confirm the current directory and relevant diff without invoking this workflow; if a reliable record is unchanged, cite it instead of rescanning or recomputing it.

Collect only the fields needed for the request:

- worktree, branch, commit, and dirty state;
- interpreter/runtime identity, task set, run configuration, and scoring version;
- raw artifact path and hash only when identifying, freezing, or transferring that artifact requires it.

Classify every fact as `executed`, `historical`, `static`, `pending`, or `not located`. If AutoDL information is unavailable, limit the AutoDL conclusion to `not located` or `not executed`; do not block unrelated CPU work.

Return one short evidence card:

- purpose and scope;
- code/worktree and execution identity;
- task/configuration/scoring identity;
- artifact path/hash and command/result when applicable;
- explicit limits and unknowns.

Update the existing canonical document when a new stable fact is established. Do not create a new report for every check, scan every environment, or recompute every hash. Never hardcode volatile paths, scores, commits, or test counts in this skill.

Do not start Qwen/GPU work merely to fill the card; record missing evidence explicitly.
