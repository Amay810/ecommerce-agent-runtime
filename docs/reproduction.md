# Reproduction

## Install

```bash
python -m venv .venv
python -m pip install -r requirements-dev.txt
```

## Deterministic CPU contract smoke

```bash
python -m ecommerce_rag.harness run \
  --tasks ecommerce_rag/data/harness_contract_smoke.jsonl \
  --db logs/demo_agent.db \
  --store logs/demo_trajectories.sqlite \
  --output logs/demo_report.json \
  --policy rule \
  --repeats 1 \
  --seed-db
python -m pytest tests -q
```

For the native Qwen experiment, the endpoint is an OpenAI-compatible wire
protocol only; it does not imply an OpenAI-hosted model. Set the endpoint and
credentials explicitly, for example:

```bash
export ARAG_LLM_BASE_URL=http://127.0.0.1:8123/v1
export ARAG_LLM_MODEL=Qwen/Qwen3-4B-Instruct-2507
export ARAG_LLM_API_KEY=local-vllm
```

If `ARAG_LLM_BASE_URL` is unset, the native policy fails closed instead of
falling back to a hosted provider. Do not commit these values when the key is
real.

This four-task fixture checks order reads, an idempotent write, a blocked
write, and an identity-verification handoff. Retrieval tasks are separate:

```bash
python -m scripts.build_retrieval_index --output-dir ecommerce_rag/index
python -m ecommerce_rag.harness run \
  --tasks ecommerce_rag/data/harness_smoke.jsonl \
  --index ecommerce_rag/index \
  --db logs/retrieval_agent.db \
  --store logs/retrieval_trajectories.sqlite \
  --output logs/retrieval_report.json \
  --policy rule \
  --repeats 1 \
  --seed-db
```

The build also writes `retrieval_manifest.json`, binding the embedding model,
chunk/parent content, shape and embedding hash. A retriever refuses an index
without a manifest or with mismatched content; rebuild an index after changing
the corpus or embedding model.

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

## Return-closure workflow

Freeze the deterministic date, policy/task hashes, SQLite seed, runtime and
model configuration before a trial:

```bash
python -m scripts.freeze_return_closure \
  --tasks ecommerce_rag/data/return_closure_tasks.jsonl \
  --output docs/experiments/return_closure_freeze_v1.json
```

The four business cases are covered in the 24-task file (8 exploration, 8
validation, 8 locked): eligible three-day unopened/no-quality return, expired
ten-day return, explicit confirmation refusal, and idempotent duplicate.
Deterministic CPU wiring can be checked with the rule policy, but that is not a
model result:

```bash
python -m scripts.run_skill_trial --policy rule --arm A --split exploration \
  --output logs/return_a_exploration.json
python -m scripts.run_skill_trial --policy rule --arm B --split exploration \
  --output logs/return_b_exploration.json
python -m scripts.propose_skill_patch \
  --exploration-report logs/return_b_exploration.json \
  --store logs/return_closure_trajectories.sqlite \
  --output logs/return_candidate.json
```

The same command accepts `--split smoke` when used with
`ecommerce_rag/data/return_closure_smoke.jsonl`. Rule/Oracle runs are
deterministic offline checks and must not be reported as model or Skill-effect
measurements.

For a configured OpenAI-compatible native tool service, run A/B/C with the
same model settings and task seeds. The Skill is enabled only when passed:

```bash
python -m scripts.run_skill_trial --policy native --arm A --split validation \
  --output logs/return_a_validation.json
python -m scripts.run_skill_trial --policy native --arm B --split validation \
  --output logs/return_b_validation.json
python -m scripts.run_skill_trial --policy native --arm all \
  --candidate logs/return_request_candidate_v1/SKILL.md \
  --split validation --output logs/return_abc_validation.json
python -m scripts.compare_skill_trials --report logs/return_abc_validation.json \
  --output logs/return_candidate_decision.json
```

The comparator is intentionally conservative: it requires a strict task-count
increase, no regression, zero illegal writes, no increase in illegal attempts,
non-degraded answer evidence/tool errors, and no more than 20% additional tool
calls. A missing model service is recorded as `not_executed`, not as a score.

## Model-backed and external evaluation

Copy `.env.example` and record the endpoint, model revision, task split, and
decoding settings with every model-backed report. The optional cluster jobs in
`nscc/` assume that the separately pinned Retail environment is available;
they are not a local CPU smoke path.
