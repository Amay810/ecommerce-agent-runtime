# 架构与接口地图

这是仓库的 source-grounded handoff map。入口导航在 [README](../README.md)，唯一
Agent 指令在 [AGENTS.md](../AGENTS.md)，当前状态和运行命令分别见
[current_status](current_status.md) 与 [reproduction](reproduction.md)。

## 证据标签

- **Static**：由当前源码、schema、import 或配置核对；不等于运行验证。
- **Executed**：有明确环境、命令、代码 revision 和结果的执行记录。
- **Historical**：保留旧 revision、策略、任务或评分口径的结果。
- **Pending**：本轮有意没有执行，或依赖当前不可用的外部环境。

## 主调用链

```text
ecommerce_rag.harness CLI
  -> load_tasks(TaskSpec)
  -> HarnessRunner.run
  -> AgentObservation(history, session, public tool schemas, evidence)
  -> NativeToolPolicy | RulePolicy
  -> AgentAction
  -> RetailTools.call(name, typed args, session/confirmation context)
  -> SQLite / local retrieval / evidence ledger
  -> tool event in canonical history
  -> typed user input | final answer | handoff | max-step stop
  -> TrajectoryStore + grade(TaskSpec, Trajectory)
```

`HarnessRunner.run` 在 `ecommerce_rag/harness.py` 中拥有 task loading、用户模拟、
执行循环、轨迹持久化和评分调用。非空 history 是 Native provider message 的唯一来源；
assistant tool call 与后续 `role=tool` result 由
`ecommerce_rag/native_tool_policy.py::_history_messages` 配对。当前代码还保留
`AgentRuntime` 的较低层 provider/Tau3 message contract。[Static]

## 核心边界

| 边界 | 所有者与事实 | 主要验证 |
|---|---|---|
| policy/action | `domain.py` 的 `AgentObservation`、`AgentAction`、`ToolCall`；`native_tool_policy.py` 将单个 provider tool call 转成 typed action | `tests/test_native_tool_policy.py`、`tests/test_agent_runtime.py` |
| identity/qualification/confirmation/write | `tools.py::RetailTools.call` 汇合身份、资格、trusted confirmation、schema、SQLite 条件更新和幂等；`confirmation.py` 持有 ledger；`orders.py` 持有 seed/schema | `tests/test_confirmation.py`、`tests/test_retail_write_tools.py`、`tests/test_stale_confirmation_binding.py` |
| public tool surface | `tool_schema.py::TOOL_SCHEMAS` 是 canonical schema；`mcp_server.py::MCPRetailFacade` 不绕过 `RetailTools.call` | `tests/test_tool_schema.py`、`tests/test_mcp_server.py`、transaction audit |
| evidence/answer | `evidence.py` 从 tool results 建 ledger 并验证回答；历史评分仍由 `harness.py::grade` 按 `scoring_version` 执行 | `tests/test_evidence_grounding.py`、`tests/test_harness_tools.py` |
| retrieval | `retrieval_index.py` 校验 manifest/index；`hybrid_retriever.py` 负责 dense/BM25/RRF 和可选 reranker；工具和 policy corpus 提供本地 fallback | `docs/retrieval.md`、retrieval tests |
| audit | `diagnostics/transaction_audit.py` 只用 SQLite 差分作为状态事实；Direct/MCP 结果和历史 artifact audit 分开 | `tests/test_transaction_audit.py`、`docs/transaction_contracts.md` |

## 稳定默认与显式可选路径

基于 `harness.py` CLI parser 和 `NativeToolPolicy.__init__` 的 **Static** 核对：

| 路径 | 入口/调用者 | 外部依赖与价值 | 默认/关闭方式 |
|---|---|---|---|
| Native | `harness run --policy native` → `NativeToolPolicy` | AutoDL 本地 Qwen、OpenAI-compatible wire protocol；当前模型入口 | 显式 `--policy native`；`skill_enabled=False`、`research_context=False` |
| Rule | `harness run --policy rule` → `RulePolicy` | 无模型；CPU wiring/guard 合同 | 仅显式用于 smoke；不解释为模型结果 |
| RAG/retrieval | `HybridRetriever`、`get_product/search_catalog/get_policy`、`scripts/build_retrieval_index.py` | 本地产品/政策索引；可选 SentenceTransformers/FAISS/BM25 | 不安装 retrieval extras 或不传 index 时走当前可用 fallback；索引不入 Git |
| MCP | `mcp_server.py` 的 façade/server | MCP client/contract tests；外部工具协议复现 | 不启动 MCP server 即关闭；业务仍走 Direct typed boundary |
| ResearchState | `HarnessRunner(research_enabled=True)`、`NativeToolPolicy(research_context=True)`、`scripts/run_research_trial.py` | 复杂咨询的缺口/来源/预算投影；Rule fixture 只做 CPU wiring | 默认不启用；实验 flag/显式构造器开启 |
| Tau3 | `tau3_agent_adapter.py`、`tau3_retail_v1.py`、`scripts/run_tau3_retail_v1.py`、`nscc/` | 外部 Tau2/Tau3 runtime、cluster job 和 adapter 复现 | 外部依赖不可用时不执行；不属于本地 CPU smoke |

旧轨迹的只读读取仍由 `TrajectoryStore` 和 `harness replay` 提供；它不依赖已经删除的
旧 JSON 执行器。`RAG`、MCP、ResearchState 和 Tau3 仍是兼容或可选路径，不能仅因不在
Native 主路径就删除。

## 数据、轨迹与评分

- `TaskSpec` 保存用户目标、允许/禁止工具、状态期望、answer/evaluation contract、
  research budget 和 `scoring_version`；hidden expectations 不能进入 policy observation。
- `Trajectory` 保存 canonical history、tool calls、retrieval/evidence、termination
  reason 和可选 research spans；`TrajectoryStore` 写入 SQLite。原始 SQLite/JSON 不
  拼进 Markdown。
- `harness-v1` 与 `harness-v2-terminal` 是 additive scoring versions；新评分写入
  独立报告，不能覆盖历史 grade。定义和 303/360 provenance 见 [evaluation](evaluation.md)。
- 当前稳定基线的既有 CPU 记录是 `67566944` / `313 passed`；这不是模型结果。实验
  数字和 raw artifact 路径/哈希集中在 [实验索引](experiments/README.md)。

## 代码与文档入口

| 入口 | 作用 |
|---|---|
| `ecommerce_rag/harness.py` | CLI、任务、循环、模拟器、轨迹和评分 |
| `ecommerce_rag/native_tool_policy.py` / `agent_runtime.py` | 当前 Native message/tool contract |
| `ecommerce_rag/tools.py` / `confirmation.py` / `orders.py` / `tool_schema.py` | trusted tool boundary 和 SQLite 状态 |
| `ecommerce_rag/evidence.py` / `retrieval_index.py` / `hybrid_retriever.py` | evidence ledger 与可选本地 retrieval |
| `ecommerce_rag/mcp_server.py` | MCP façade |
| `ecommerce_rag/diagnostics/transaction_audit.py` | model-free transaction/Direct-MCP audit |
| `docs/tools_and_safety.md` / `docs/transaction_contracts.md` | 授权和审计细节 |
| `docs/experiments/README.md` | 隔离实验、manifest、机器产物和恢复快照索引 |

本地文件地图不证明真实 Qwen 的行为效果；AutoDL 无 GPU、未启动服务或缺失原始轨迹时，
只记录 `Pending`/`Not located`，不补成成功实验。
