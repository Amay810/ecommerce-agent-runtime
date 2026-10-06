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

## Confirmation in the Direct/MCP checks

The Direct/MCP differential acts as the trusted host for every `confirmed=True`
write row. Direct gets its confirmation through the `RetailTools` confirmation
ledger. MCP gets it through `MCPRetailFacade.issue_confirmation` and
`record_user_confirmation`. Rows with `confirmed=False` (`missing_confirmation`)
stay unconfirmed. Each row records `trusted_confirmation_issued`.
`tests/test_transaction_audit.py` fixes the expected outcome of every write row:
happy paths commit the same state on both surfaces, each rejection row returns
its labelled error, and the idempotent no-op stays a no-op.

The adversarial suite also replays every case through the guarded MCP facade.
Those results are in a separate `mcp_on` block, so the Direct `on`/`off` counts
stay directly comparable with runs that predate it.

## Lesson: identical rejection is not coverage

Two surfaces that are stopped by the same gate still report 0 state,
observation and error mismatches. The 09fc5bd differential below is an example.
A differential check therefore has to fix the expected outcome of each row, not
just case counts and mismatch totals: committed state for a happy path and the
labelled error for each rejection.

## Recorded runs

These runs are local CPU deterministic fixture replays with no model in the
loop. They are evidence about wiring and safety contracts, not about model
quality. The numbers below come from the frozen JSON artifacts named in each
entry.

### ff0f8a2 (current)

- Revision `ff0f8a222bc82ff1a22ea3cc370c21296f0981a6`, clean worktree, local
  `.venv` (Python 3.12.3), `--repetitions 15`.
- Artifacts: `~/archives/ecommerce-audit/transaction_contracts_ff0f8a2/` with
  `PROVENANCE.txt` and `SHA256SUMS`. `adversarial_suite.json` SHA-256
  `f2761bf1513d64209db88d15d61a9b00af7a0f2991ba2097ed555fd5a9d66f86`.
  `direct_mcp_differential.json` SHA-256
  `6fd3d0c1879285e955cc5511c7d2c5aaafed8afdb4fae4f6eaa0b5b7610ed746`.
- Adversarial Direct, from `adversarial_suite.json`:
  - `execution_classification` is `150 = 105 + 30 + 15`.
  - `on` attempts/blocked/committed are `105/105/0`, and `off` is `105/0/105`.
  - For the stale probe, `on_state_commits_without_binding=0` and
    `off_state_commits_without_binding=15`.
  - `on`, `off`, `execution_classification`, `by_family` and
    `known_unresolved_gap` are identical to the 09fc5bd run.
- Adversarial MCP, from `mcp_on`:
  - executions/attempts/blocked/committed are `150/105/105/0`.
  - `stale_confirmation.state_commits_without_binding=0`.
  - `vs_direct_on` has `post_state_mismatches=0` and `error_mismatches=15`. All
    15 are in `stale_confirmation`: Direct returns `confirmation_stale` and MCP
    returns `confirmation_required`. Both block the write.
- Differential, from `direct_mcp_differential.json`:
  - `tools_tested=15`, `executions=42` and `trusted_confirmation_rows=21`.
  - State, observation and error mismatches are `0/0/0`, and both surfaces
    produce the same outcome on every row.
  - The write-tool rows are listed below. The 09fc5bd column is read from that
    run's `direct_mcp_differential.json`.

| coverage case | tool | trusted confirmation | 09fc5bd (both surfaces) | ff0f8a2 (both surfaces) |
|---|---|---|---|---|
| happy_write | `create_return_request` | yes | `confirmation_required` | ok, changed=True, state: `return_status`, `version` |
| missing_confirmation | `create_return_request` | no | `confirmation_required` | `confirmation_required` |
| business_ineligible_state | `create_return_request` | yes | `order_not_delivered` | `order_not_delivered` |
| happy_write | `cancel_pending_order` | yes | `confirmation_required` | ok, changed=True, state: `cancel_reason`, `status`, `version` |
| missing_confirmation | `cancel_pending_order` | no | `confirmation_required` | `confirmation_required` |
| terminal_state_rejection | `cancel_pending_order` | yes | `confirmation_required` | `order_not_pending` |
| invalid_verification | `cancel_pending_order` | yes | `identity_verification_failed` | `identity_verification_failed` |
| happy_write | `modify_pending_order_address` | yes | `confirmation_required` | ok, changed=True, state: `shipping_address`, `version` |
| missing_confirmation | `modify_pending_order_address` | no | `confirmation_required` | `confirmation_required` |
| terminal_state_rejection | `modify_pending_order_address` | yes | `confirmation_required` | `order_not_pending` |
| happy_write | `modify_pending_order_items` | yes | `confirmation_required` | ok, changed=True, state: `item_ids`, `product_id`, `version` |
| invalid_item_identity | `modify_pending_order_items` | yes | `confirmation_required` | `item_not_found` |
| item_cardinality_rejection | `modify_pending_order_items` | yes | `confirmation_required` | `item_length_mismatch` |
| happy_write | `modify_pending_order_payment` | yes | `confirmation_required` | ok, changed=True, state: `payment_method_id`, `version` |
| idempotent_noop | `modify_pending_order_payment` | yes | `confirmation_required` | ok, changed=False, no state diff |
| missing_confirmation | `modify_pending_order_payment` | no | `confirmation_required` | `confirmation_required` |
| happy_write | `modify_user_address` | yes | `confirmation_required` | ok, changed=True, state: `address` |
| missing_confirmation | `modify_user_address` | no | `confirmation_required` | `confirmation_required` |
| invalid_verification | `modify_user_address` | yes | `identity_verification_failed` | `identity_verification_failed` |
| happy_write | `return_delivered_order_items` | yes | `confirmation_required` | ok, changed=True, state: `return_status`, `version` |
| invalid_payment_ownership | `return_delivered_order_items` | yes | `confirmation_required` | `payment_method_not_found` |
| missing_confirmation | `return_delivered_order_items` | no | `confirmation_required` | `confirmation_required` |
| invalid_item_identity | `return_delivered_order_items` | yes | `confirmation_required` | `item_not_found` |
| happy_write | `exchange_delivered_order_items` | yes | `confirmation_required` | ok, changed=True, state: `exchange_status`, `item_ids`, `product_id`, `version` |
| terminal_state_rejection | `exchange_delivered_order_items` | yes | `confirmation_required` | `order_not_delivered` |
| item_cardinality_rejection | `exchange_delivered_order_items` | yes | `confirmation_required` | `item_length_mismatch` |
| invalid_item_identity | `exchange_delivered_order_items` | yes | `confirmation_required` | `item_not_found` |

The "trusted confirmation" column describes the ff0f8a2 run. The 09fc5bd run
issued no trusted confirmation for any row.

### 09fc5bd (differential superseded)

The ff0f8a2 run supersedes the Direct/MCP differential conclusions of the
09fc5bd run. The 09fc5bd artifacts in
`~/archives/ecommerce-audit/transaction_contracts_09fc5bd/` are left unchanged.

That run issued no trusted confirmation, so 18 of its 42 rows stopped at
`confirmation_required` on both surfaces:

- all 8 confirmed `happy_write` rows;
- 3 `terminal_state_rejection` rows;
- 3 `invalid_item_identity` rows;
- 2 `item_cardinality_rejection` rows;
- 1 `invalid_payment_ownership` row;
- 1 `idempotent_noop` row.

Its `0/0/0` mismatches therefore do not show that happy-path writes,
terminal-state rejection, item or payment rejection, or the idempotent no-op
were exercised. Its adversarial ON/OFF counts are reproduced exactly by
ff0f8a2.
