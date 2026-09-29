---
name: eval-contract
description: Audit task definitions, acceptance rules, scoring semantics, or experiment-comparison contracts when they are added or changed. Do not use for typo fixes, archiving old results, or ordinary code cleanup.
---

# Eval Contract

Use this skill only when a task, acceptance rule, scoring meaning, or comparison protocol changes. Read only the relevant task definitions, the exact request received by the Agent, available data/tools/budget, scoring rules, and necessary fixtures or examples.

Check the contract before inspecting or changing the scorer:

- Verify that the requested task is feasible with the available data, tools, and budget; keep unproven conditions explicitly unknown.
- Separate legal equivalent completion paths from tools that are merely available. Do not turn an allowed tool into a required tool without evidence.
- Verify that output format and source/citation requirements are actually conveyed to the Agent before scoring them.
- Define business completion, factual correctness, condition coverage, and source binding separately, and name the evidence source for each.
- Record numerator, denominator, termination rule, `scoring_version`, and whether results across versions are directly comparable.

Return a short review in this form:

`finding -> evidence location -> minimum correction -> boundary examples and verification scope`

Delegate arithmetic, batch checks, and re-scoring to existing deterministic scripts or tests. Do not copy formulas into this skill or start model experiments.

Use these boundary probes when reviewing a contract:

- A budget below the requested product price is a feasibility mismatch unless the task defines an equivalent legal alternative.
- A tool described as allowed but not required must not be scored as mandatory.
- A citation format not disclosed to the Agent must not be used as an undisclosed deduction.

Do not treat this review as evidence that a model run succeeded. Keep changed task/scorer tests and any comparison run outside the skill's instructions.
