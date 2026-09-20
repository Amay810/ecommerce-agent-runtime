# Reproduction and environment identity

Commands are relative to the repository root and do not download model
weights, call a hosted model or start GPU work unless explicitly stated.
Evidence labels in this file distinguish executed checks from static or
historical records.

## Environment matrix

| Environment | Actual identity and dependencies | Appropriate work | Current status |
|---|---|---|---|
| Local host | `/home/may/Code/repos/ecommerce-agent-runtime`; Ubuntu 24.04.4, kernel `7.0.0-31-generic`, x86_64; `/usr/bin/python3` 3.12.3; `uv` 0.12.10; current `.venv/bin/python` is uv-managed CPython 3.12.3. | Documentation, CPU tests, deterministic harness, static checks and optional local retrieval. | **Executed**: pytest 291 passed; `uv pip check` passed for 35 packages; dev requirements dry-run made no changes. |
| Local Docker | Docker 29.8.0 is installed, default context is present, but this user cannot access `/var/run/docker.sock`. | Optional `Dockerfile` core import smoke only. | **Static/unavailable**: no container was inspected or started. |
| AutoDL CPU | `/root/autodl-tmp/src/ecommerce-agent-runtime`; `/root/autodl-tmp/venvs/qwen-vllm/bin/python`, Python 3.12.3; accessed through the existing SSH helper. | CPU regression and file/version synchronization. | **Current**: reachable; no GPU visible; vLLM endpoint not running. Historical CPU run: 291 passed, 2 warnings. |
| AutoDL Qwen | Same checkout plus `/root/autodl-tmp/models/Qwen3-4B-Instruct-2507`, vLLM launcher `/root/autodl-tmp/config/start-qwen-vllm.sh`, endpoint `127.0.0.1:8123`. | Only after allocating GPU: native Qwen tool-call smoke and fixed model experiments. | **Pending**: model files exist, but this cleanup intentionally did not start GPU/service. |
| GitHub Actions | Repository remote is `https://github.com/Amay810/ecommerce-agent-runtime.git`. | Would provide hosted CI if workflows were checked in. | **Unavailable as evidence**: this checkout has no `.github/workflows`; no CI run is claimed. |

The Codex execution environment is not a substitute for the local host or
AutoDL. Always record the path, interpreter and command with a result.

## CPU install and checks

On a Python/conda installation with `venv` and `pip`:

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
```

On this local host, system Python has no `ensurepip`, so the standard
`python -m venv` route was not usable. The verified local route is:

```bash
uv venv .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
UV_CACHE_DIR=/tmp/ecommerce-uv-cache uv pip check --python .venv/bin/python
UV_CACHE_DIR=/tmp/ecommerce-uv-cache uv pip install --dry-run --no-deps \
  --python .venv/bin/python -r requirements-dev.txt
```

The last two commands check the existing environment and installation plan;
they are not proof of a fresh isolated install. The current local environment
passed both. Core requirements are in `requirements.txt`; install
`requirements-retrieval.txt`, `requirements-llm.txt` or `requirements-data.txt`
only for the corresponding optional path.

## Deterministic CPU smoke

This is the representative command used on the local host:

```bash
mkdir -p logs
.venv/bin/python -m ecommerce_rag.harness run \
  --tasks ecommerce_rag/data/harness_contract_smoke.jsonl \
  --db /tmp/ecommerce-runtime-cpu-smoke-final.db \
  --store /tmp/ecommerce-runtime-cpu-smoke-final.sqlite \
  --output /tmp/ecommerce-runtime-cpu-smoke-final.json \
  --policy rule --repeats 1 --seed-db
```

**Executed, local host**: four trajectories; task success, policy compliance
and terminal-state accuracy were all `1.0`. This uses `RulePolicy` and proves
dispatch/state/scoring wiring only. It does not prove Qwen behavior, Skill
effectiveness, MCP deployment or GPU readiness.

The local full suite command is:

```bash
.venv/bin/python -m pytest -q
```

**Executed, local host at `ecb3e3d`**: `291 passed in 6.95s`. A prior AutoDL
CPU run on the message-source code reported `291 passed, 2 warnings`; its
warnings and environment are not silently attributed to this local run.

## Retrieval path

Install and build only when an index-backed run is needed:

```bash
.venv/bin/python -m pip install -r requirements-retrieval.txt
.venv/bin/python -m scripts.build_retrieval_index --output-dir ecommerce_rag/index
```

`HybridRetriever` validates `retrieval_manifest.json`, corpus fingerprints,
embedding shape/hash and then uses FAISS when available, otherwise its NumPy
dense fallback, plus BM25/RRF. The generated index and embedding/model cache
are machine-local and ignored by Git. The same backend and manifest must be
used for a paired experiment.

## Native Qwen path (AutoDL GPU only)

The repository client speaks an OpenAI-compatible HTTP protocol; the model is
the local Qwen checkpoint. This protocol name does not select OpenAI hosting.
Use the existing AutoDL launcher or the equivalent command after GPU allocation:

```bash
vllm serve /root/autodl-tmp/models/Qwen3-4B-Instruct-2507 \
  --served-model-name Qwen3-4B-Instruct-2507 --host 127.0.0.1 --port 8123 \
  --dtype bfloat16 --max-model-len 32768 --gpu-memory-utilization 0.90 \
  --enable-auto-tool-choice --tool-call-parser hermes
export ARAG_LLM_BASE_URL=http://127.0.0.1:8123/v1
export ARAG_LLM_MODEL=Qwen3-4B-Instruct-2507
export ARAG_LLM_API_KEY=local-vllm
```

Use a fresh database/store for each native smoke. The CLI entry is:

```bash
.venv/bin/python -m ecommerce_rag.harness run \
  --tasks ecommerce_rag/data/return_closure_smoke.jsonl \
  --db logs/native_smoke.db --store logs/native_smoke.sqlite \
  --output logs/native_smoke.json --policy native \
  --skill skills/return_request/SKILL.md --repeats 1 --seed-db
```

`scripts/run_skill_trial.py` is the fixed historical A/B/C runner. Missing
service means `not_executed`, not a model score. Do not run validation or
locked merely because a deterministic smoke passed.

## Environment issues and reuse notes

- The local standard `venv` route failed because `/usr/bin/python3` lacks
  `ensurepip`; use uv or a conda/pip interpreter. This is an installation
  limitation, not a project import failure.
- Local Docker cannot currently be used because of Docker socket permissions;
  no container result is available. The Dockerfile is a minimal core smoke,
  not the retrieval or Qwen environment.
- AutoDL is intentionally in CPU/no-service state for this cleanup. The model
  directory and launcher were found, but no GPU or vLLM result is claimed.
- No GitHub Actions workflow exists in the checkout, so CI configuration is
  not evidence that any version ran.
- The message-source fix at `7b52e80` has targeted tests and the CPU regression
  described above. Its effect on Qwen action selection remains pending; do not
  mix the earlier Qwen trajectories with a post-fix result.

Historical experiment JSON under `docs/experiments/` keeps its original
commit, task, policy and execution class. Deterministic Rule/Oracle runs are
wiring evidence; they are not model baselines or Skill-effect evidence.
