# E-commerce Agent Runtime

一个带可信工具边界的电商 Agent runtime：policy 提议动作，runtime 负责身份、资格、
确认、幂等写入、SQLite 状态、检索证据和轨迹评分。项目不包含训练流程，也不把
Rule/Oracle/CPU 结果当成模型质量。

## 从这里开始

1. [AGENTS.md](AGENTS.md)：唯一项目 Agent 指令和工作边界。
2. [docs/current_status.md](docs/current_status.md)：稳定基线、默认配置和未决问题。
3. [docs/reproduction.md](docs/reproduction.md)：安装、CPU smoke、环境和 AutoDL 命令。
4. [docs/architecture.md](docs/architecture.md)：主调用链、接口和可选适配器。
5. [docs/evaluation.md](docs/evaluation.md)：评分版本、指标口径和历史结果索引。
6. [docs/experiments/README.md](docs/experiments/README.md)：实验状态、产物和恢复索引。

工具与授权细节见 [docs/tools_and_safety.md](docs/tools_and_safety.md)；transaction
audit 见 [docs/transaction_contracts.md](docs/transaction_contracts.md)。

## 最短 CPU 上手路径

```bash
uv venv .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
mkdir -p logs
.venv/bin/python -m ecommerce_rag.harness run \
  --tasks ecommerce_rag/data/harness_contract_smoke.jsonl \
  --db logs/contract.db --store logs/contract_trajectories.sqlite \
  --output logs/contract_report.json --policy rule --repeats 1 --seed-db
```

这是显式选择的 `RulePolicy` deterministic wiring smoke，不是 Qwen 结果。是否运行
完整回归按 `docs/current_status.md` 和实际 diff 决定；完整命令、可选依赖和 AutoDL
服务命令不要从 README 复制，统一看 [docs/reproduction.md](docs/reproduction.md)。

## 主调用链

```text
HarnessRunner → NativeToolPolicy / RulePolicy / legacy LLMPolicy → AgentAction
             → RetailTools.call → SQLite / retrieval / evidence
             → typed input / final answer / handoff → TrajectoryStore + scorer
```

`NativeToolPolicy` 是当前 Qwen tool-call adapter；`LLMPolicy` 是旧 JSON envelope
兼容路径。MCP 是可选 façade，但 Direct/MCP 都汇合到 `RetailTools.call`；Tau3 是
外部 adapter，不属于本地 CPU smoke。稳定入口保持 legacy observation、full tools、
answer contract off、retrieval experience off；compact observation、selective tools、
evidence-answer-v1 和 retrieval experience 只在隔离实验中使用。

凭据、模型权重、索引缓存、SQLite 日志、原始轨迹和图谱生成物不提交仓库。
