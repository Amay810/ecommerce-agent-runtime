# Agent handoff

This repository is the runtime half of a guarded e-commerce Agent. Work only
inside this checkout; do not modify the sibling `tau3-grpo` or `fintool-rl`
projects.

## Main path

```text
HarnessRunner (harness.py)
  -> NativeToolPolicy or LLMPolicy
  -> AgentAction
  -> RetailTools.call
  -> SQLite / retrieval / evidence
  -> UserSimulator or terminal answer
  -> TrajectoryStore and scorer
```

Canonical handoff references: [current status](docs/current_status.md),
[environment and reproduction](docs/reproduction.md), and the detailed
[architecture/file map](docs/architecture.md).

Start at these files:

- `ecommerce_rag/harness.py`: task loading, user turns, execution loop,
  trajectory storage and scoring.
- `ecommerce_rag/native_tool_policy.py`: current Qwen tool-call adapter and
  Skill injection; `ecommerce_rag/agent_runtime.py`: provider message/runtime
  contracts.
- `ecommerce_rag/tools.py`, `confirmation.py`, `orders.py`, and
  `tool_schema.py`: identity, eligibility, confirmation, write and idempotency
  boundaries. Do not move authorization into the model or Skill.
- `ecommerce_rag/retrieval_index.py` and `hybrid_retriever.py`: optional local
  product/policy index and manifest checks.
- `ecommerce_rag/mcp_server.py`: optional MCP façade; it must continue to use
  `RetailTools.call`.
- `skills/return_request/SKILL.md`: active v0 workflow guidance. Skill text
  cannot change tools, permissions, scoring, or confirmation authorization.

For a problem, begin with the boundary that owns it: message/action parsing in
`native_tool_policy.py` and `agent_runtime.py`; business state in `tools.py`
and `orders.py`; evidence/answers in `evidence.py`; task results in
`harness.py`; retrieval in the two retrieval modules; external Tau3 behavior
in `tau3_retail_v1.py`, `scripts/run_tau3_retail_v1.py`, and `nscc/`.

## Install and verify

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

The command above assumes the selected Python provides `venv` and `pip`. For
the uv-managed environment used on the development machine, use:

```bash
uv venv .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
```

Environment identities, the local `uv` result, AutoDL CPU/Qwen roles, Docker
access and the absence of a checked-in GitHub Actions workflow are recorded in
`docs/reproduction.md`; do not treat this Agent process as a host environment.

The core requirements cover the SQLite harness, NumPy test support and MCP
contract tests. Install extras only when needed:

- `requirements-retrieval.txt` for SentenceTransformers, jieba and FAISS;
- `requirements-llm.txt` for the legacy local Transformers adapter;
- `requirements-data.txt` for the Amazon dataset preparation script.

The representative CPU flow is deterministic RulePolicy wiring, not a model
run:

```bash
mkdir -p logs
.venv/bin/python -m ecommerce_rag.harness run \
  --tasks ecommerce_rag/data/harness_contract_smoke.jsonl \
  --db logs/contract.db --store logs/contract_trajectories.sqlite \
  --output logs/contract_report.json --policy rule --repeats 1 --seed-db
```

## Real Qwen path

Use a separately installed, locally cached `Qwen3-4B-Instruct-2507` behind an
OpenAI-compatible vLLM endpoint. The wire protocol name does not select an
OpenAI-hosted model. The repository does not download weights or start a GPU
server during CPU tests.

```bash
vllm serve /models/Qwen3-4B-Instruct-2507 \
  --served-model-name Qwen3-4B-Instruct-2507 --host 127.0.0.1 --port 8123 \
  --dtype bfloat16 --max-model-len 32768 --gpu-memory-utilization 0.90 \
  --enable-auto-tool-choice --tool-call-parser hermes
export ARAG_LLM_BASE_URL=http://127.0.0.1:8123/v1
export ARAG_LLM_MODEL=Qwen3-4B-Instruct-2507
export ARAG_LLM_API_KEY=local-vllm
```

Use `python -m scripts.run_skill_trial` for fixed A/B/C arms. Keep model,
tools, task seeds, simulator, runtime, Skill version and decoding settings
fixed; reset DB/session state per task. A missing service is `not_executed`,
not a model score. Do not run locked during exploratory diagnosis.

## Boundaries and history

Write tools require verified identity, eligibility and a trusted confirmation
ledger record bound to session, user, operation and parameters. A normal text
question is terminal under the strict protocol; only the typed
`request_user_input` path continues the simulated interaction.

The current Skill is v0. The real Qwen Skill v1 candidate was rejected; old
experiment JSON remains historical evidence and must not be relabelled as the
message-source-fix result. The next model check is a single-step comparison
using the existing exploration context; it is deliberately not run in this
cleanup.
