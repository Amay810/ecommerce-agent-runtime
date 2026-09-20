# Reproduction

Commands below assume the repository root and a Python 3.11+ environment.
They do not download model weights or call a hosted model unless the native
endpoint is explicitly configured.

## CPU setup and contract smoke

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
mkdir -p logs
.venv/bin/python -m ecommerce_rag.harness run \
  --tasks ecommerce_rag/data/harness_contract_smoke.jsonl \
  --db logs/contract.db --store logs/contract_trajectories.sqlite \
  --output logs/contract_report.json --policy rule --repeats 1 --seed-db
.venv/bin/python -m pytest -q
```

The smoke uses `RulePolicy`, so it checks tool dispatch, state transitions,
confirmation and scoring wiring only. The latest recorded full CPU result is
`291 passed, 2 warnings`.

The pip command assumes a Python/conda environment with pip. On the current
uv-managed development environment, create/install instead with:

```bash
uv venv .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
```

## Optional retrieval

Install the retrieval extra only for an index-backed run:

```bash
.venv/bin/python -m pip install -r requirements-retrieval.txt
.venv/bin/python -m scripts.build_retrieval_index --output-dir ecommerce_rag/index
.venv/bin/python -m ecommerce_rag.harness run \
  --tasks ecommerce_rag/data/harness_smoke.jsonl \
  --index ecommerce_rag/index --db logs/retrieval.db \
  --store logs/retrieval_trajectories.sqlite \
  --output logs/retrieval_report.json --policy rule --repeats 1 --seed-db
```

The index directory is generated and ignored by Git. Its manifest binds the
embedding model, corpus content, shape and hashes; rebuild it after changing
the source corpus or embedding model.

## Native Qwen service

Use the locally cached `Qwen3-4B-Instruct-2507` with a separately installed
vLLM service. The repository's native client uses an OpenAI-compatible wire
protocol; this does not mean an OpenAI-hosted model.

```bash
vllm serve /models/Qwen3-4B-Instruct-2507 \
  --served-model-name Qwen3-4B-Instruct-2507 --host 127.0.0.1 --port 8123 \
  --dtype bfloat16 --max-model-len 32768 --gpu-memory-utilization 0.90 \
  --enable-auto-tool-choice --tool-call-parser hermes

export ARAG_LLM_BASE_URL=http://127.0.0.1:8123/v1
export ARAG_LLM_MODEL=Qwen3-4B-Instruct-2507
export ARAG_LLM_API_KEY=local-vllm
```

For one real native smoke, use a fresh DB and store:

```bash
.venv/bin/python -m ecommerce_rag.harness run \
  --tasks ecommerce_rag/data/return_closure_smoke.jsonl \
  --db logs/native_smoke.db --store logs/native_smoke.sqlite \
  --output logs/native_smoke.json --policy native --skill skills/return_request/SKILL.md \
  --repeats 1 --seed-db
```

For the fixed return-closure experiment runner, use
`scripts/run_skill_trial.py`. A is no Skill, B is v0, and C is an explicitly
provided candidate. Keep model, tools, runtime, task seeds, user simulator,
database/session reset, and decoding settings unchanged across arms. A missing
endpoint is `not_executed`, not a score. This cleanup does not run exploration,
validation or locked experiments.

## External and historical paths

The old JSON `LLMPolicy` path remains for compatibility and uses
`requirements-llm.txt` only when its local Transformers backend is selected.
Amazon preparation uses `requirements-data.txt`. Tau3 requires its separately
pinned external checkout and is started only through its documented wrapper or
`nscc/` jobs; it is not part of CPU validation.

Historical return-closure freezes and deterministic reports are under
`docs/experiments/`. They retain their original runtime/scoring attribution.
The v1 Skill candidate was rejected, v0 remains active, and the message-source
fix has not yet been credited with a model improvement. Do not rewrite those
artifacts or read locked task contents while diagnosing the next single-step
model comparison.
