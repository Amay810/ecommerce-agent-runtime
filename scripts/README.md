# Script index

The supported local entry point is `python -m ecommerce_rag.harness` for run,
replay, and compare.

The scripts in this repository cover:

- deterministic catalogue, retrieval-index, harness, and SFT-data preparation;
- external Retail evaluation and process audits;
- context-history measurement and trajectory export;
- paired complex-consultation A/B wiring in `scripts/run_research_trial.py`;
- the transaction-contract audit in `scripts/audit_transaction_contracts.py`.

Scripts that require a model endpoint, external environment, or cluster are
explicitly documented as such. They are not part of the CPU-only smoke path.
