# Transaction contract CPU audit

This audit treats the model as an action proposer and SQLite-backed
`RetailTools` as the state-transition boundary. It does not call an LLM,
download weights, start a model server, or execute a network replay.

Run it with the project environment:

```text
python3 -m scripts.audit_transaction_contracts \
  --artifact docs/harness_v2_llm_360_regraded_v2.json \
  --output-dir reports/transaction_contracts \
  --repetitions 15
```

The run produces frozen predicates, schema evidence, deterministic guardrail
cases, differential tool-surface checks, and a compact final report. The
enabled contracts are identity binding, boolean confirmation, legal state
transitions, mutation scope, item identity/cardinality, refund destination
closure, idempotency, and read-only purity.

When the working tree is dirty, create a clean worktree from the selected
baseline and record the exact revision before comparing audit results. Generated
provider artifacts stay outside the runtime checkout.
