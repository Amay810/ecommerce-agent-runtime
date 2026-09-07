# E-commerce Agent Runtime

This repository contains the standalone runtime for a guarded e-commerce
assistant. It owns retrieval, typed retail tools, transactional checks,
trajectory recording, offline audits, and the local evaluation harness.

## Runtime chain

```text
user request
  -> observation and policy
  -> retrieval or typed tool call
  -> execution-time identity/eligibility/confirmation checks
  -> SQLite state transition
  -> evidence and trajectory audit
```

The model proposes the next action; the runtime validates the tool schema and
protects write operations. The database, not the model, is the source of truth
for order state.

## Quick start

```bash
python -m venv .venv
python -m pip install -r requirements-dev.txt
python -m ecommerce_rag.harness run \
  --tasks ecommerce_rag/data/harness_smoke.jsonl \
  --db logs/demo_agent.db \
  --store logs/demo_trajectories.sqlite \
  --output logs/demo_report.json \
  --policy rule \
  --repeats 1 \
  --seed-db
python -m pytest tests -q
```

For a model-backed run, copy `.env.example`, configure the local or
OpenAI-compatible endpoint, and use `--policy llm`. The harness records the
trajectory so it can be replayed and audited without calling the model again.

## Main entry points

- `ecommerce_rag/harness.py`: run, replay, and compare trajectories;
- `ecommerce_rag/agent_runtime.py`: provider-facing function-call runtime;
- `ecommerce_rag/tools.py`: retail tools and write guardrails;
- `ecommerce_rag/retrieval_index.py` and `hybrid_retriever.py`: indexed facts;
- `ecommerce_rag/process_audit.py`: trajectory/process checks;
- `ecommerce_rag/diagnostics/transaction_audit.py`: CPU transaction contract audit;
- `scripts/run_tau3_retail_v1.py`: optional external Retail evaluation wrapper;
- `nscc/`: cluster serving and evaluation job files.

## Boundaries

This is a reproducible research runtime, not a production service. External
Retail evaluation requires the separately pinned environment recorded in the
data-source documents; its source tree is not bundled here. Retrieval data in
this repository is for local research use and carries the restrictions recorded
in `docs/data_source_manifest.json`.

The repository does not claim model-training results. Offline reports are
kept separate from the runtime path, and raw large artifacts are intentionally
not part of this tree.

See [reproduction](docs/reproduction.md), [current status](docs/current_status.md),
[evaluation](docs/evaluation.md), and [transaction contracts](docs/transaction_contracts.md).
