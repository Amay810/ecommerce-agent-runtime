# 复杂咨询补证与后续轨迹评测

本轮目标是让现有 Agent 在一次检索不足时，能看到“已经知道什么、来自哪里、还
缺什么、已经查过什么、还剩多少预算”，并留下足够的轨迹，供下一阶段从失败轨迹
提炼搜索经验。它不改现有退货 Skill、交互协议、confirmation ledger、写工具或
评分授权。

## 数据审计与任务集

任务只引用当前 checkout 的本地数据：

- `ecommerce_rag/data/sample_products.jsonl` 有 40 个商品，实际字段为
  `id/title/category/price/inventory/attributes/description/qa/reviews/updated_at`；
  当前任务使用 P001、P002、P004、P007、P008、P019 等已存在商品。
- `ecommerce_rag/data/policies.jsonl` 有 POL001–POL005，字段为
  `id/title/policy_type/scope/content/updated_at`。
- `seed_database()` 的只读业务数据用于一条订单咨询，主要字段包括 order/user
  identity、status、ordered/delivered time、quality issue 和 verification code。
  写工具没有加入任务。

任务文件分成两个 split，避免用同一问题换个说法冒充验证集：

| split / task | 用户问题覆盖 | 必要事实与来源 | 完成条件 / 数据限制 |
|---|---|---|---|
| exploration / `research_exp_01_earbuds_policy` | P001 与 P007 的价格、续航、佩戴/防护比较，并问退货 | 两个商品卡与详情：`P001/P007`；退货政策 `POL001` | 两个商品来源、比较字段和政策条款都能追溯；不推断未列属性 |
| exploration / `research_exp_02_juicer_policy` | P002 果汁机能否打冰、清洗及退货限制 | `P002` 商品详情；`POL001` | 取得商品详情和政策；商品未声明的用途保持未知 |
| exploration / `research_exp_03_keyboard_compare` | P004 与 P019 键盘的价格、连接和兼容性比较 | `P004/P019` 商品来源 | 两个独立商品来源覆盖比较字段；没有无线协议证据就不下结论 |
| exploration / `research_exp_04_insufficient_earbuds` | 预算、重量、防护等条件筛选耳机，并询问是否有更合适款 | 搜索结果和候选详情；任务故意要求对近似候选保持未知 | 若本地资料不能证明全部条件，明确缺口，不把相似商品当满足条件 |
| exploration / `research_exp_05_preorder_shipping` | P008 预售/库存与发货政策 | `P008` 商品详情；`POL003` | 只能报告本地卡片和政策内容；没有承诺具体日期 |
| exploration / `research_exp_06_order_readonly` | 用户 U0608 查询 O000003 的订单、退货资格和政策 | 只读订单 `O000003`；资格工具；`POL001` | 通过身份和六位验证码后完成只读查询；不能发起退货写入 |
| validation / `research_val_01_expired_return` | 另一用户询问已过期订单是否能退 | 只读订单 `O000007`；`POL001`；资格结果 | 明确基于订单日期/政策的可确认部分；未知项不能补猜 |
| validation / `research_val_02_refund_arrival_gap` | 取消订单退款何时到账、通过何种渠道 | 订单 `O000004`；`POL005` | 说明本地资料是否有退款条款；实际到账时间和支付渠道未建模时必须保留未知 |

每条 JSON 的 `evaluation_contract`、`answer_expectations` 和 `gold_doc_ids` 只供
`grade()` / 人工核验使用；`HarnessRunner` 不复制到 `AgentObservation`，也不进入
provider prompt。任务中的 `research_budget` 是资源上限，不是答案提示。

## 现有调用链与新增接口

已有 Harness 循环被保留：

```text
TaskSpec -> history -> read-only tool -> tool result/evidence
         -> next AgentObservation -> next action -> final answer/unknown
```

本轮新增的最小接口是：

- `research_state.py::derive_research_state(history, evidence_ledger, budget)`：从
  canonical history 和成功工具结果派生 `ResearchState`，包含
  `user_constraints`、`obtained_facts`、`missing_evidence`、`executed_queries`、
  `retrieval_calls`、`remaining_budget` 和 `status`。
- `NativeToolPolicy(research_context=True)`：把上述状态作为 provider-visible 的
  `<research_state>` context 追加到同一历史消息构造流程；默认值为 `False`，因此
  A 不改变。
- `HarnessRunner(research_enabled=True, research_budget=...)`：按 task 或 override
  预算记录连续检索；超过上限时不调用工具，写入 `research_budget_exhausted` 的
  synthetic read-only call，并以缺口列表 fail closed。
- `Trajectory.research_spans`：记录每次决策的 `state_before`、action 和 outcome。
  canonical `tool_calls` 与 `evidence_ledger` 仍是事实来源，没有重复维护业务状态。
- `scripts/run_research_trial.py`：用同一任务/DB reset/工具/预算运行 A/B，并记录
  `tool_calls_total`、`retrieval_calls`、`budget_exhausted`、必要事实覆盖、自动
  `citation_binding_pass`、`manual_answer_support`、token、latency 和 span 数；每个
  task detail 还保存完整 request messages/tools、原始 provider response、工具参数/结果
  和 research spans。

`EvidenceStateRulePolicy` 和 `LocalResearchFixtureRetriever` 仅用于 CPU 接线：前者
按缺口选择下一次只读工具，后者对提交的商品/政策 JSONL 做确定性 lexical overlap。
它们不是新的生产 Agent，也不代表真实模型的搜索质量。

## A/B 对照

| 项目 | A：当前行为 | B：补证状态行为 |
|---|---|---|
| provider/context | 不传结构化 research state；保留现有 history/tool result | 传用户约束、已取事实/来源、缺口、查询历史、剩余预算 |
| 工具与权限 | 现有 read/write schema 与可信边界 | 完全相同；B 不授予新权限、不改变 confirmation |
| 资源 | 相同 task、seed、DB/session reset、max steps、research budget | 相同 |
| 轨迹 | 原有 history/tool/evidence/grade | 额外记录 research spans，便于解释缺证和停止原因 |
| 结果解释 | 作为当前基线，不人为删减其能力 | CPU 规则只验证状态能推动下一次查询；Native 效果待 AutoDL |

本机 exploration fixture 的一次执行报告在 `/tmp/complex_research_rule_cpu.json`
（不提交 Git）：A 为 `1/6` task success，B 为 `2/6`；B 的工具序列显示了
`search_catalog -> compare_products/get_product -> get_policy` 等连续补证，工具
调用 F1 为 A `0.567`、B `0.876`。这些数字受 deterministic RulePolicy、lexical
fixture 和当前 scorer 影响，只证明接线/状态转移，不能宣称模型收益或泛化提升。
报告中的 `manual_answer_support` 保持 `pending`，因为自动 citation binding 不能
替代人工判断回答是否真的由证据支持。

## 同步前核对结论

### Native A/B 实际差异

Native 两臂都使用同一个 task file、task seed、DB/session reset、retriever instance、
tool schema、research budget、Harness budget guard、`max_steps`、Skill 和 decoding
配置。两臂都由同一个 `HarnessRunner` 执行，所以工具 dispatch、预算停止和最终评分规则
相同；A/B 唯一有意改变的是 `NativeToolPolicy.research_context`：A 为 `False`，B 为
`True`，后者才把派生 `<research_state>` 放入 provider request。

为避免把 raw ledger 当成额外变量，Native 两臂都不再单独把 `evidence_ledger` 传给
policy；B 看到的已有事实和来源只来自 `ResearchState.obtained_facts`。`--policy rule`
的 A/B 则是不同 deterministic policy 的 wiring 对照，不能用于归因 Native 模型收益。

### 约束和缺口来源

`ResearchState` 只从当前用户消息、canonical history 和实际成功/失败工具事件及其
evidence ledger 派生。它不读取 `TaskSpec.evaluation_contract`、`answer_expectations`、
`gold_doc_ids` 或任务 ID；这些字段只留在 `TaskSpec` 给 scorer 使用。`request` 会脱敏
独立验证码，订单/商品标识仍保留为用户约束的一部分。Native 诊断 request 中可以检查
完整的 `<research_state>`，但不会出现答案 key 或 gold source list。

### CPU validation 的 `1/2`

这不是运行时异常，也不是泄漏或非法状态写入。上一轮 validation 报告中，A 的一条
任务因 budget=2 在最后的资格查询上触发 fail-closed，B 的另一条任务则在取得订单后
提前结束并被 scorer 归为 wrong-tool；两臂都存在没有引用绑定、必要事实覆盖偏低的情况。
因此 `1/2` 同时反映 deterministic RulePolicy 的动作能力、lexical fixture/本地字段
限制和当前答案证据评分，不足以判断 ResearchState 对真实模型有害或有效；validation
暂不重跑、不纳入第一轮 AutoDL 小试跑。

## 第一轮 AutoDL 小试跑

选择两条 exploration：

- `research_exp_01_earbuds_policy`：跨两个商品来源再补退货政策，观察是否能根据比较
  结果继续查政策，以及是否出现重复查询/查错来源。
- `research_exp_05_preorder_shipping`：一个商品来源加物流政策，问题较窄，观察模型
  是否能用较少查询完成并明确“没有具体日期”的未知。

每条任务运行 Native A/B，共四条轨迹；validation 保留。使用 `--task-id` 可避免复制
任务文件。报告 JSON 和 TrajectoryStore 都保存逐步 request、raw response、工具结果、
research state、token 和耗时，四条轨迹可直接纳入后续 exploration，不需要重复运行。

```bash
cd /root/autodl-tmp/src/ecommerce-agent-runtime
export ARAG_LLM_BASE_URL=http://127.0.0.1:8123/v1
export ARAG_LLM_MODEL=Qwen3-4B-Instruct-2507
export ARAG_LLM_API_KEY=local-vllm
/root/autodl-tmp/venvs/qwen-vllm/bin/python -m scripts.run_research_trial \
  --policy native --arm all \
  --tasks ecommerce_rag/data/complex_research_exploration.jsonl \
  --task-id research_exp_01_earbuds_policy \
  --task-id research_exp_05_preorder_shipping \
  --skill skills/return_request/SKILL.md \
  --index /path/to/frozen/retrieval_index \
  --db /tmp/complex_research_native_2task.db \
  --store /tmp/complex_research_native_2task.sqlite \
  --output /tmp/complex_research_native_2task.json \
  --max-steps 8
```

`/path/to/frozen/retrieval_index` 必须替换为实际 manifest 已核验的索引。服务、索引或
依赖缺失时记录 `not_executed`，不填成模型分数。

## 参考机制与采用边界

### Open Deep Research

源码 `deep_researcher.py` 的已确认机制是：研究 supervisor 先决定是否调用研究
动作；工具执行结果追加回消息/状态，再回到下一轮决策；研究者循环执行工具调用，
达到工具预算后转入压缩/总结。它解决的是“工具结果如何成为下一次动作输入，以及
何时继续/停止”的状态传递问题。

本项目对应接口是 `HarnessRunner.run` 的既有 history/evidence 循环，加上
`derive_research_state`、read-only budget guard 和 `research_spans`。没有移植其
LangGraph/多 Agent 框架，也没有另起一套 DeepSearch runner。

依据：[`deep_researcher.py` 源码](https://raw.githubusercontent.com/langchain-ai/open_deep_research/main/src/open_deep_research/deep_researcher.py)。

### Recuris

Recuris README 描述的机制是 frozen agent + Skill Memory、结构化轨迹
`(w_t, E_t, a_t, o_t)`、从失败轨迹提炼局部经验，以及用独立 paired held-out
任务决定是否接受候选更新。它解决的是“把失败定位为局部缺证/动作问题，并用独立
配对门控避免把一次成功误当改进”。本轮只采用其可审计轨迹、exploration/validation
分离和未来 gate 所需记录；不生成 Skill、不自动 patch 策略、不执行自进化 gate。

README 是机制说明；源码中的事件/Skill Memory 模块可作为后续实现入口，但本轮没有
移植其训练或记忆包。依据：[`Recuris README`](https://raw.githubusercontent.com/Gen-Verse/Recuris/main/README.md)、
[`events.py`](https://github.com/Gen-Verse/Recuris/blob/main/src/recuris/events.py)、
[`skillmemory.py`](https://github.com/Gen-Verse/Recuris/blob/main/src/recuris/skillmemory.py)。

## 后续实验与自进化前置

下一轮 AutoDL 只需同步本轮范围清楚的提交，使用固定的本地 Qwen、Skill v0、任务 seed、
retrieval manifest、DB reset、decoding 和 `--max-steps`，先跑窄回归，再执行上面的四条
轨迹。服务或索引缺失记为 `not_executed`；validation 暂不运行。

在真实轨迹可用前，不生成新 Skill 或自动改策略。下一阶段输入应至少包含：每次决策
前的缺口、动作与参数、工具返回/来源、预算、最终回答、必要事实覆盖、人工支持判断、
失败类型和耗时/token。只有在固定经验与候选经验使用同一独立 paired validation
并记录接受/拒绝理由后，才讨论经验提炼收益。
