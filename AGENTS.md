# Agent handoff

这是一个带可信工具边界的电商 Agent runtime。只在本仓库内工作，不修改同级
`tau3-grpo` 或 `fintool-rl`。

## Main path

```text
HarnessRunner (harness.py)
  -> { NativeToolPolicy（主 Qwen 路径）
     | RulePolicy（CPU wiring 验证）
     | LLMPolicy（旧 JSON 兼容路径） }
  -> AgentAction
  -> RetailTools.call
  -> SQLite / retrieval / evidence
  -> UserSimulator or terminal answer
  -> TrajectoryStore and scorer
```

Canonical handoff references: [current status](docs/current_status.md),
[environment and reproduction](docs/reproduction.md), and the detailed
[architecture/file map](docs/architecture.md).

先从这些文件开始：

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

排查问题时从拥有该边界的模块开始：消息和动作转换看
`native_tool_policy.py`、`agent_runtime.py`；业务状态看 `tools.py`、
`orders.py`；证据和回答看 `evidence.py`；任务结果看 `harness.py`；检索看
两个 retrieval 模块；外部 Tau3 看 `tau3_retail_v1.py`、
`scripts/run_tau3_retail_v1.py` 和 `nscc/`。

## 安装与验证

接手时先执行 `git status --short --branch`，阅读
`docs/current_status.md` 和相关验证记录，再根据 diff 决定验证范围。只改
文档时复用既有测试；改了 runtime、Schema、依赖或测试发现规则时跑相关测试；
只有这些核心部分发生变化或明确需要发布级检查时才跑完整回归。

本机推荐使用已经验证过的 uv 路径：

```bash
uv venv .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
UV_CACHE_DIR=/tmp/ecommerce-uv-cache uv pip check --python .venv/bin/python
```

本机 `/usr/bin/python3` 没有 `ensurepip`，因此上面的通用 `venv` 路径在本机
不可用。其他提供 `venv` 和 `pip` 的 Python/conda 环境才使用通用方式：

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
```

Environment identities, the local `uv` result, AutoDL CPU/Qwen roles, Docker
access and the absence of a checked-in GitHub Actions workflow are recorded in
`docs/reproduction.md`; do not treat this Agent process as a host environment.

核心依赖覆盖 SQLite harness、NumPy 和 MCP contract tests。按需安装额外层：

- `requirements-retrieval.txt` for SentenceTransformers, jieba and FAISS;
- `requirements-llm.txt` for the legacy local Transformers adapter;
- `requirements-data.txt` for the Amazon dataset preparation script.

代表性的 CPU 流程是确定性的 `RulePolicy` wiring，不是真实模型运行：

```bash
mkdir -p logs
.venv/bin/python -m ecommerce_rag.harness run \
  --tasks ecommerce_rag/data/harness_contract_smoke.jsonl \
  --db logs/contract.db --store logs/contract_trajectories.sqlite \
  --output logs/contract_report.json --policy rule --repeats 1 --seed-db
```

## AutoDL 上的真实 Qwen 路径

真实 Qwen 只在 AutoDL 执行。服务启动优先使用已经核对过的脚本；脚本内部使用
`/root/autodl-tmp/venvs/qwen-vllm/bin/vllm`，从 `runtime.env` 读取本地模型路径，
不会下载模型：

```bash
bash /root/autodl-tmp/config/start-qwen-vllm.sh
```

服务启动后，客户端也使用 AutoDL 的 qwen-vllm 解释器，而不是本机 `.venv`：

```bash
cd /root/autodl-tmp/src/ecommerce-agent-runtime
export ARAG_LLM_BASE_URL=http://127.0.0.1:8123/v1
export ARAG_LLM_MODEL=Qwen3-4B-Instruct-2507
export ARAG_LLM_API_KEY=local-vllm
/root/autodl-tmp/venvs/qwen-vllm/bin/python -m ecommerce_rag.harness run \
  --tasks ecommerce_rag/data/return_closure_smoke.jsonl \
  --db /tmp/native_smoke.db --store /tmp/native_smoke.sqlite \
  --output /tmp/native_smoke.json --policy native --repeats 1 --seed-db
```

固定 A/B/C 使用同一 AutoDL 解释器运行
`/root/autodl-tmp/venvs/qwen-vllm/bin/python -m scripts.run_skill_trial`，并固定
模型、工具、任务 seed、simulator、runtime、Skill 版本和 decoding 参数；每个任务
重置 DB/session。服务缺失记为 `not_executed`，不是模型分数；探索诊断阶段不要跑
locked。

## 边界与历史

写工具必须同时满足身份、资格和绑定 session/user/operation/parameters 的可信
confirmation ledger 记录。严格协议下普通文本会终止本轮，只有 typed
`request_user_input` 才会继续模拟交互。

当前 Skill 是 v0。真实 Qwen 的 Skill v1 候选已拒绝；旧实验 JSON 是历史证据，
不能改标为消息来源修复后的结果。下一次模型检查是使用已有 exploration 上下文
的单步前后对照，本轮不执行。
