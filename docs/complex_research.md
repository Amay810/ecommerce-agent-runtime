# 复杂咨询实验记录

本文件记录 ResearchState、compact observation、selective tools 和回答证据合同的
任务/接口边界；它们都不是稳定主路径。实验结果、代码快照、机器产物路径和哈希集中
在 [实验索引](experiments/README.md)，运行命令集中在 [reproduction](reproduction.md)。

## 任务与事实边界

探索集 `ecommerce_rag/data/complex_research_exploration.jsonl` 包含六条咨询：

- P001/P007 耳机比较 + 退货政策；
- P002 榨汁杯用途、清洗和退货；
- P004/P019 键盘比较；
- 资料不足的耳机筛选；
- P008 预售商品 + 物流政策；
- 只读订单、政策和退货资格。

验证集 `complex_research_validation.jsonl` 是两条不同问题：过期退货和退款到账信息
缺口，不是探索题的改写。商品/政策事实来自 checked-in JSONL；订单事实来自
`seed_database()`。`gold_doc_ids`、`answer_expectations`、`evaluation_contract`
只给 scorer/人工核验使用，不能进入 `AgentObservation` 或 provider prompt。

## 实验接口

ResearchState 由 `research_state.py::derive_research_state` 从 canonical history、
成功/失败工具结果和 evidence ledger 派生，包含用户约束、已取得事实/来源、缺口、
查询历史和剩余预算；它不维护第二份业务状态。`HarnessRunner(research_enabled=True)`
只为显式实验开启预算和 fail-closed read-only guard，`NativeToolPolicy(research_context=True)`
才向 provider 注入状态。

compact observation 和 selective tools 是另一组 provider projection：

- `observation.py::build_compact_observation` 将目标、来源范围、已知事实、最新结果和
  `unobserved`/`not_stated` 缺口压缩到决策视图；完整 history、ledger、原始结果仍保留在
  Trajectory；
- `tool_selection.py::select_tool_schemas` 只从 canonical `TOOL_SCHEMAS` 投影每轮
  可见工具，并记录 rationale/recovery；隐藏工具不能改变 `RetailTools` 权限；
- `evidence-answer-v1` 只约束最终回答如何区分事实、缺失条件和 `[E#]` 来源，不覆盖
  历史 `grade()` 或 `joint_success`；
- `retrieval_experience` 只在显式 flag 下尝试一次空结果后的替代查询，默认关闭且候选
  状态为 rejected。

## 配对与解释规则

AutoDL 24-task 配对固定任务、seed、Qwen、retrieval manifest、Skill、DB/session reset、
decoding、预算和停止逻辑；实际配置以每个报告中的 `configuration`、
`observation_view`、`tool_visibility` 和逐轮 trace 为准，不能用 A/B 字母替代配置名。
四组结果只按 24 个配对任务解释，不把 96 条轨迹当独立样本。组合版 task success 低于
legacy + full 且成本更高，因此 compact/selective 不进入稳定默认。

CPU fixture 使用本地商品/政策文件和 RulePolicy，只能验证连续检索、evidence 投影、
预算停止、类别/空结果恢复和不可见工具的 fail-closed 行为；不能推出真实模型收益。
本轮不重跑 CPU fixture 或 AutoDL 实验。

## 允许的后续验证

若恢复实验，必须从完整 dirty 快照重建共享 harness/native/domain 改动，并为每个实验
同时保留：代码 revision/hash、任务集/hash、模型和 decoding、retrieval manifest、
Skill、seed/DB reset、scoring_version、原始 JSON/SQLite 路径/hash、正反结果和结论。
实验产物不拼进 Markdown；validation/locked 不因文档整理自动启动。
