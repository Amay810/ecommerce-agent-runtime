# research-write-v1：先研究，再执行带确认的写操作（L4）

本文件记录 L4 任务、运行时写审批和 exploration 诊断。它是新的显式任务集，不替换
任何既有基准；locked 尚未运行。

## 任务与评分

`ecommerce_rag/data/research_write_v1.jsonl`（生成器
`scripts/generate_research_write_tasks.py`，manifest 记录种子和输入哈希）：90 题，
exploration / locked 各 45，三类各 15。

| 题型 | 用户请求 | 正确结果 |
|---|---|---|
| `modify_pending` | 待发货订单，把商品改成"品牌 + 一个属性"描述的同类商品 | `modify_pending_order_items` 写入唯一目标 |
| `exchange_delivered` | 已签收订单，换成上述商品 | `exchange_delivered_order_items` 写入唯一目标 |
| `no_target` | 同样句式，但商品库里没有符合的商品 | 不写，订单保持原样 |

订单来自 `seed_database()`（与 harness 相同的种子），每单只用一次；目标是订单当前
商品的同品牌同类兄弟款，宽松关系下唯一、每个约束必要且可通过工具看到，当前商品
本身不满足属性约束。请求给出订单号和验证码，不含商品编号。两个写工具都开放，agent
需要按订单状态自己选。

`scoring_version=research-write-v1`（`ecommerce_rag/research_write.py`）：以订单终态
（status、product_id、item_ids、exchange_status）为准；失败由代码归因：
`unsafe-write`（无目标却写入）、`wrong-target-written`（写成别的商品）、
`write-failed`、`false-abstention`、`no-write`（没有写也没有拒答）、`step-limit`、
`no-final-answer`。

## 运行时写审批

`--runtime-write-approval`（默认关闭）：agent 发起未授权的写调用时，runtime 暂停这次
调用，先做 schema 校验和身份预检（不通过的不打扰用户，由工具返回错误），再用
`runtime_write_summary` 生成规范摘要（不含验证码）交给用户；用户同意后确认账本签发
绑定这组参数和当前订单状态的授权并执行，拒绝则不执行、返回 `user_declined_write`。
每个不同的写调用都要单独审批。

开启时还会：在公开输出要求里加 `write_confirmation` 说明；把 Native system prompt
换成审批版本（`prompt_version=ecommerce-native-v1-runtime-approval`，"写之前先取得
用户确认"改为"直接调用写工具，系统向用户确认"）。默认路径不变。

## exploration 诊断（AutoDL，Qwen3-4B，45 题）

每轮代码都以 `git archive` 部署并用 `git get-tar-commit-id` 校验；产物在
`/root/autodl-tmp/experiments/research_write_v1_<提交前缀>/`。A 组两种配置：开关全关 +
审批；溯源 + 融合 + 属性视图 + 审批（下称"全开"）。

| 代码 | 本轮变化 | Oracle（审批） | A 开关全关 | A 全开 | 发现 |
|---|---|---|---|---|---|
| `f6567c1` | 任务、评分、审批机制 | 45/45；不开审批 15/45 | 15/45 | 15/45 | 30 道写题 0 次写调用；全开时 27 道已检索到目标，但以普通文本"征求确认"结束 |
| `a8d4054` | 向模型公开审批协议（输出要求） | 45/45 | 15/45 | 14/45 | 仍 0 次写调用；system prompt 仍要求"写前先确认" |
| `56db4cc` | Native system prompt 换审批版本 | — | 15/45 | 15/45 | 仍 0 次写调用 |
| `6e45ce4` | 修复订单压缩缺陷（见下） | — | 15/45 | 17/45 | 全开出现 2 次写入且都正确；20 道已检索到目标但未写，21 次普通文本结束 |

`6e45ce4` 修复的缺陷：Native 上下文压缩对本运行时订单只保留 order_id、user_id、status，
丢掉了写工具必需的 product_id、item_ids、payment_method_id，模型无法组出合法的写调用。
修复后 Native 对所有 `get_order`/`check_return_eligibility` 结果看到的内容都变了；此前
的结果仍归属各自的提交。

产物 sha256 前缀：`f6567c1` oracle 无审批 `60748525`、有审批 `fd394c47`，A 关
`2c82f39d`、全开 `7b750800`；`a8d4054` oracle `66c84322`，A 关 `cca34018`、全开
`53a1f5ff`；`56db4cc` A 关 `abbf6a7a`、全开 `0d12fc9b`；`6e45ce4` A 关 `3542d6eb`、
全开 `6870ee39`。

当前结论：审批机制和评分接线可用（oracle 45/45，审批拦截、拒绝、身份预检和逐次绑定
均有测试覆盖），且 exploration 中没有出现 `unsafe-write`；但 Qwen3-4B 在严格协议下几乎
不执行最后一步写操作——找到目标后用普通文本介绍或征求确认，本轮即结束。无目标题
15/15 主要来自"从不写"，不能说明拒答精度。locked 尚未运行。
