# Transaction contract CPU audit

This audit treats the model as an action proposer and SQLite-backed
`RetailTools` as the state-transition boundary. It does not call an LLM,
download weights, start vLLM, or execute a network replay.

Run it with the bundled/runtime Python or an environment containing the
project dependencies:

```text
python3 -m scripts.audit_transaction_contracts \
  --artifact data/simulations/tau3_g0e_train_qwen3_4b_temp08_k8/results.json \
  --artifact reports/argument_provenance_audit/canonical_inputs/base_results.json \
  --artifact reports/argument_provenance_audit/canonical_inputs/step9_results.json \
  --artifact reports/argument_provenance_audit/canonical_inputs/step37_results.json \
  --artifact docs/harness_v2_llm_360_regraded_v2.json \
  --output-dir reports/transaction_contracts \
  --repetitions 15
```

The run produces:

- `transaction_contract_spec.json`: frozen predicates and normalization;
- `artifact_capability_audit.json`: message/trajectory schema evidence;
- `adversarial_suite.json`: deterministic guardrail ON/OFF cases;
- `direct_mcp_differential.json`: all 15 exposed tools through both surfaces,
  across 42 deterministic happy-path, rejection, confirmation, terminal-state,
  invalid-argument, and idempotent/no-op cases;
- `failure_semantics.json`: denominator reconciliation for the historical
  operational summary;
- `transaction_audit.json`: combined per-step evidence;
- `final_report.md`: compact findings.

The enabled contracts are identity binding, boolean confirmation, legal state
transitions, mutation scope, item identity/cardinality, refund destination
closure, idempotency, and read-only purity. The current schema has no amount,
tax, discount, balance, or payment-history fields, so money conservation is
not enabled. Historical confirmation binding is also unresolved because the
tool API accepts a boolean but does not persist the evidence that produced it.

Natural trajectory JSON is classified as non-replayable when it contains only
messages, tool calls, and observations. The audit never treats natural-language
observations as database state.

## Latest-baseline rule

When the working tree is dirty, do not fast-forward or reset it. Create a
detached clean worktree from `origin/main`, run a baseline CPU suite, apply only
the audit patch there, and rerun the same command. The 2026-09-04 comparison is
recorded in `latest_baseline.json`; the deterministic replay reproduction is in
`latest_deterministic_revalidation.json`. The Tau3/GRPO simulation artifacts
remain post-training scope even when they are physically present in the
repository checkout.
