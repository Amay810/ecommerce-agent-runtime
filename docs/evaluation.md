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

Closed methodology notes and the 40-row audit CSV are in the private archive:

- [evaluation closeout](https://github.com/Amay810/ecommerce-agentic-rag-archive/blob/main/docs/evaluation_closeout_v2.md)
- [trajectory audit CSV](https://github.com/Amay810/ecommerce-agentic-rag-archive/blob/main/docs/trajectory_audit_40.csv)

The compact machine-readable report that backs the table above is [harness_v2_llm_360_regraded_v2.json](harness_v2_llm_360_regraded_v2.json). Raw trajectories remain under the [`agent-v2-raw`](https://github.com/Amay810/ecommerce-agentic-rag/tree/agent-v2-raw) tag.
