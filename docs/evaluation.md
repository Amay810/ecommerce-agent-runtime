# Agent evaluation

## Protocol

- 120 fixed tasks split equally between dev and locked sets;
- three deterministic Qwen3-4B runs per task, producing 360 trajectories;
- source SQLite remains immutable and revised grades are stored separately;
- retrieval, action selection, tool contract, policy compliance and terminal state are scored independently;
- a 40-row systematic audit checks semantic failures that operational grading cannot cover.

## Results

| Automated operational metric | All | Dev | Locked |
|---|---:|---:|---:|
| Operational success | 84.17% | 83.33% | 85.00% |
| Policy compliance | 95.00% | 95.00% | 95.00% |
| Terminal-state accuracy | 100% | 100% | 100% |
| Forbidden-tool attempt | 5.00% | 5.00% | 5.00% |
| Illegal state change | 0% | 0% | 0% |

The 40-row audit is systematic rather than random. It produced 80.0% success agreement and 77.5% policy agreement with the v2 operational grader, so those proportions are not extrapolated to all trajectories. The fail-closed RL gate remained ineligible and no training claim is made.

Metric terminology is frozen as follows: the current reported operational
success is `303/360 = 84.17%`. The evidence JSON retains
`legacy_automatic_operational_success = 94.17%` for historical compatibility;
that legacy field is not the current operational headline.

Scoring versions are additive. `harness-v1` remains the default historical
contract and does not require a non-empty final answer for operational success.
The opt-in `harness-v2-terminal` contract keeps the same state/tool checks and
also requires an explicit trajectory termination reason of `final_answer` or a
successful `handoff`. A max-step, budget-exhausted, or unavailable typed-input
stop does not become complete merely because it contains stop text. It is not
a replacement for old grades: a re-score must write a separate report keyed
by `scoring_version`, retaining the original `harness-v1` grade beside the new
one. Citation binding, evidence coverage, compact observation and
retrieval-experience changes are separate experiments and are not silently
included in either task-success definition.

Closed methodology notes and the 40-row audit CSV are in the private archive:

- [evaluation closeout](https://github.com/Amay810/ecommerce-agentic-rag-archive/blob/main/docs/evaluation_closeout_v2.md)
- [trajectory audit CSV](https://github.com/Amay810/ecommerce-agentic-rag-archive/blob/main/docs/trajectory_audit_40.csv)

The compact machine-readable report that backs the table above is [harness_v2_llm_360_regraded_v2.json](harness_v2_llm_360_regraded_v2.json). Raw trajectories remain under the [`agent-v2-raw`](https://github.com/Amay810/ecommerce-agentic-rag/tree/agent-v2-raw) tag.
