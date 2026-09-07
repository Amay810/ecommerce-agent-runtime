# Current status

Last updated: 2026-09-07.

The runtime baseline is frozen for the repository split. The supported local
path is the deterministic harness plus CPU diagnostics; model-backed calls and
external Retail evaluation remain environment-dependent.

## Supported surface

- typed retail tools with identity, eligibility, confirmation, and idempotency
  checks;
- hybrid retrieval over the checked-in sample and policy data;
- native function-call adaptation through the runtime boundary;
- trajectory replay, process auditing, and transaction-contract auditing;
- optional external Retail evaluation through the pinned environment described
  in `docs/data_source_manifest.json`.

## Validation boundary

The CPU suite does not download weights, start a model server, or contact the
external evaluation environment. A successful local test run therefore proves
runtime contracts and deterministic fixtures, not model quality or cluster
throughput.

The external Retail environment is pinned to commit
`fc0055dc4e0a316c3f83133267fbd6faaa770992`. It must be checked out separately
and its train/test split must remain frozen when an evaluation is run.

## Next work

Keep changes inside the runtime/tool/retrieval contracts and record any new
evaluation artifact with its provider, model revision, task split, and exact
command. Do not add generated datasets, checkpoints, or provider logs to this
repository.
