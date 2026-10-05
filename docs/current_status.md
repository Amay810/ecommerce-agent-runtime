# 当前状态

## 稳定候选

- 当前清理候选为 `8ae1c0e4ad6ec62eb73a5f2b72a6a41910c389db`，本机 CPU 完整回归为
  `262 passed`。这是 deterministic/runtime 验证，不是 Qwen 质量证据。
- 清理前稳定基线 `67566944ad79c03feaf42a9ca72a5d20571cad06` 的记录仍保留为
  `313 passed`；减少的 51 个测试全部属于已删除的旧 LLM 专属执行路径，不是回归。
- 进入项目时仍以 `git rev-parse HEAD` 和当前 worktree 状态为准；本轮不运行 Qwen、GPU
  或历史模型实验。
- 主路径是 `HarnessRunner` + `NativeToolPolicy` + `RetailTools.call`，连接 SQLite、
  可选 retrieval、evidence、typed confirmation 和 TrajectoryStore。`RulePolicy`
  仅用于 CPU wiring smoke。
- 稳定配置是 legacy observation、full tools、answer contract off、retrieval
  experience off。`NativeToolPolicy` 的 `skill_enabled`、`research_context` 默认关闭；
  compact observation、selective tools、evidence-answer-v1 和 retrieval experience
  只在隔离实验中出现。
- AutoDL 当前无可见 GPU；本轮不启动 vLLM、不运行 Qwen、不重跑 24-task 或 360-task。

## 入口与可选路径

| 入口 | 当前定位 |
|---|---|
| `ecommerce_rag.harness run --policy rule` | 推荐 CPU contract smoke；结果不代表模型。CLI 的 policy 默认值仍按代码为 `oracle`，文档命令显式选择 `rule`。 |
| `--policy native` / `NativeToolPolicy` | AutoDL 本地 Qwen tool-call 路径；真实效果仍需按固定配置单独记录。 |
| `mcp_server.py` | 可选 MCP façade；业务 dispatch 仍汇合到 `RetailTools.call`。 |
| `--policy retrieval_top1` | `research-find-v1` 的确定性单次检索基线；不是模型结果。 |
| `ResearchState` / `run_research_trial.py` | 可选补证实验路径，默认不注入主 Native。 |
| Tau3 adapter / `nscc/` | 外部运行环境适配，不属于本机 CPU smoke。 |

## 已知实验结论

- compact observation + selective tools 的 24-task AutoDL 配对未通过默认替换门槛：
  组合版 `12/24`，低于 legacy + full 的 `16/24`，且 token/延迟更高；保留为隔离
  实验，不改稳定默认。
- evidence-answer-v1 的来源绑定有所改善，但 task success 和成本门槛未过；继续
  隔离验证，不覆盖历史评分。
- retrieval experience 候选状态为 rejected，默认关闭。
- RulePolicy 的 ResearchState/CPU fixture 只验证接线、证据投影和 fail-closed 行为，
  不提供模型收益结论。
- `research-find-v1`（`a380b90`，AutoDL）：200 题答案级找商品任务；确定性检索基线
  25.5%，Native Qwen3-4B 在 exploration 上 36%，与基线配对 +6pp（CI [−7, +19]），
  未显著；主要失败是模型改写查询后召回下降并误拒答。`db3374c` 增加默认关闭的
  `--ground-search-filters`、`--search-query-fusion`，exploration 上 37% → 57%
  （CI [+10, +30]）；locked 一次性评估 40% → 48%（CI [−2, +19]，未显著），有答案题
  显著提升而无解题拒答下降。两个开关保持默认关闭，详见 [research_find](research_find.md)。

实验路径、配置、原始产物和哈希见 [实验索引](experiments/README.md)；不要把共享
dirty diff 拆写成可独立运行的实验 patch。

## 评分与未决问题

- `harness-v1` 保留历史 operational 语义；`harness-v2-terminal` 仅显式启用，并以
  `scoring_version` 写入独立报告，不能覆盖旧 grade。定义和 360 summary 见
  [evaluation](evaluation.md)。
- 用户确认前两天完成了真实 `303/360` 复测；本次只读定位未找到该运行的独立
  SQLite/JSON/log。现有同名 tracked JSON 是历史汇总，二者分开记录。
- 消息来源修复、stale confirmation audit 修复和 Native 角色合同已有 CPU 覆盖；
  真实 Qwen 对行为的影响、跨进程 ledger、崩溃恢复和并发窗口仍未验证。
- 凭据、模型、缓存、SQLite 日志、原始轨迹和图谱生成物不进入仓库。

## 文档入口

- 运行命令和环境：[reproduction](reproduction.md)
- 主调用链和接口：[architecture](architecture.md)
- 评分版本和结果 provenance：[evaluation](evaluation.md)
- 实验 manifest、dirty 快照和原始产物索引：[experiments/README](experiments/README.md)
