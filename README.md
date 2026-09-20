# E-commerce Agent Runtime

一个带可信工具边界的电商 Agent 运行时：模型负责选择下一步动作，运行时负责身份、资格、确认、幂等写入、SQLite 状态、检索证据和轨迹记录。项目不把 Rule/Oracle 离线结果当成模型质量，也不包含训练流程。

## 从这里开始

- 新 Agent：先读 [AGENTS.md](AGENTS.md)。
- 当前状态、已知限制和实验归属：[docs/current_status.md](docs/current_status.md)。
- 安装、CPU smoke、检索和 Qwen 服务：[docs/reproduction.md](docs/reproduction.md)。
- 工具和授权边界：[docs/tools_and_safety.md](docs/tools_and_safety.md)。

## 快速开始（CPU）

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
mkdir -p logs
.venv/bin/python -m ecommerce_rag.harness run \
  --tasks ecommerce_rag/data/harness_contract_smoke.jsonl \
  --db logs/contract.db \
  --store logs/contract_trajectories.sqlite \
  --output logs/contract_report.json \
  --policy rule --repeats 1 --seed-db
.venv/bin/python -m pytest -q
```

RulePolicy 是确定性 CPU wiring smoke，不是 Qwen 结果。最近一次完整 CPU 证据为 `291 passed, 2 warnings`；MCP 测试包含在内。

若 Python 没有 `venv`/`pip`，使用 conda 环境或 `uv venv .venv` 加
`uv pip install --python .venv/bin/python -r requirements-dev.txt`。本机现有
`.venv` 是 uv 管理环境。

需要检索时再安装 `requirements-retrieval.txt` 并构建本地索引。需要旧 JSON 模型适配器的本地 Transformers 路径时安装 `requirements-llm.txt`；Amazon 数据准备脚本才需要 `requirements-data.txt`。

## 运行路径

```text
HarnessRunner → NativeToolPolicy / LLMPolicy → AgentAction
             → RetailTools.call → SQLite / retrieval / evidence
             → UserSimulator or final answer → TrajectoryStore + scorer
```

- 当前电商模型实验入口是 `NativeToolPolicy`，使用 OpenAI-compatible wire protocol；实际模型固定为本地 `Qwen3-4B-Instruct-2507`，不是 OpenAI 托管模型。
- `LLMPolicy` 是保留的旧 JSON action envelope 兼容路径。
- `ecommerce_rag/mcp_server.py` 是可选 MCP façade，业务调用仍汇合到 `RetailTools.call`。
- `scripts/run_tau3_retail_v1.py` 和 `nscc/` 是外部 Tau3 环境适配，不属于本地 CPU smoke。

## 当前限制

消息来源修复已经通过 CPU 回归：非空 history 是唯一消息来源，工具结果保持 `tool` 角色，用户重复文本保留，tool-call/result ID 配对。修复是否改变真实 Qwen 的单步动作尚未验证。

历史 return-closure 的确定性报告属于 Rule/Oracle 或 wiring 验证；真实 Qwen exploration 曾使用旧消息序列版本。候选 Skill v1 已拒绝，当前 Skill 为 v0；这些事实不能解释成 Skill 已有效，也不能改写历史报告。后续模型对照只使用 exploration 上下文，暂不运行 locked。

凭据、模型权重、索引缓存、SQLite 日志和原始轨迹不提交仓库。需要浅克隆时使用 `git clone --depth 1 <repo-url>`；完整 Git 历史不会因删除当前文件而变小。
