# Reproduction

## Install

```bash
python -m venv .venv
python -m pip install -r requirements-dev.txt
```

## Deterministic CPU smoke

```bash
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

## Replay and audit

```bash
python -m ecommerce_rag.harness replay \
  --store logs/demo_trajectories.sqlite \
  --trajectory-id TRAJECTORY_ID
python -m scripts.audit_transaction_contracts \
  --artifact docs/harness_v2_llm_360_regraded_v2.json \
  --output-dir reports/transaction_contracts \
  --repetitions 15
```

The audit is read-only with respect to the checked-in source and does not call
a model or external service.

## Model-backed and external evaluation

Copy `.env.example` and record the endpoint, model revision, task split, and
decoding settings with every model-backed report. The optional cluster jobs in
`nscc/` assume that the separately pinned Retail environment is available;
they are not a local CPU smoke path.
