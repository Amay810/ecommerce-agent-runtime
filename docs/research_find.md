# research-find-v1：按条件找商品

本文件记录最小闭环 v0 的任务、评分和第一轮结果。它是新的显式任务集，不替换
120 题 harness 基准，也不重标任何历史结果。

## 为什么需要它

历史 `harness-v1`/`v2` 读类任务只要求 gold 文档在某次检索结果中出现过
（`harness.py` 的 `retrieval_gold_ok`），不检查最终回答选中了哪个商品；120 题的
84% 因此不能说明任务到顶。复杂咨询的 8 题建在 40 个手写商品上，题面直接点名商品，
也没有检索深度。`research-find-v1` 把"按条件找商品"做成答案级任务。

## 任务

`ecommerce_rag/data/research_find_v1.jsonl`：200 题，exploration / locked 各 100，
每个切分四种题型各 25 题。生成器 `scripts/generate_research_find_tasks.py`，
manifest 记录输入哈希、种子和规则。

| 题型 | 要求 |
|---|---|
| `multi_constraint` | 商品类型 + 2–3 个属性/预算，唯一确定一个商品 |
| `near_sku` | 类型 + 品牌 + 1 个属性；同品牌同类型存在相似款 |
| `no_answer` | 把一个约束换成同类型里出现过的值，使全目录无解，必须拒答 |
| `typo_alias` | 与 `near_sku` 相同，但请求里的品牌带确定性错别字 |

生成规则（`validate_tasks` 对序列化结果逐条复核）：

- 答案在渲染文本前锁定；目标商品精确满足所有约束；
- 用宽松关系检查唯一性（属性包含、标题/描述/属性中出现该短语、价格未知视为可能
  满足），没有第二个商品可能满足全部约束；
- 每个非类型约束都必要：去掉任意一个就会放进别的商品；
- 每个约束都能通过现有工具看到：搜索结果字段，或 `get_product` 返回的前 5 个 chunk；
- 按（末级类目, 品牌）分组切分；排除三个检索评测集的 gold 商品；
- 题面不含商品编号；答案只在 `evaluation_contract`，不进入 `AgentObservation`。

输入是 `scripts/build_amazon_5k.py` 重建的 5k 语料（sha256
`4d5e175c…62a5f76`，统计与仓库登记一致）和 `scripts/build_amazon_category_paths.py`
恢复的类目路径旁路文件（sha256 `a21b43aa…e97764`，4627/5000 有路径）。两者都由
gitignore 排除，不提交。

## 评分合同

`scoring_version=research-find-v1`（`ecommerce_rag/research_find.py`）：

- 有答案：最终回答中**只出现一个**商品编号且等于锁定答案；
- 无答案：不出现商品编号，并有明确拒答表述；
- 只开放 `search_catalog`、`get_product`、`compare_products`；其他工具算不合规；
- 失败由代码归因，不用 LLM 判分：`never-retrieved`、`retrieved-not-selected`、
  `false-abstention`、`multiple-products-answered`、`answered-unsatisfiable`、
  `no-clear-abstention`、`step-limit`、`no-final-answer`。

`scripts/summarize_research_find.py` 按题做 bootstrap 95% CI，两份报告时给出配对差。
沿用的 `tool_call_f1` 在 `required_tools` 为空时恒为 0，不用于本任务。

## Step 0/1 结果

| 项 | 值 |
|---|---|
| 代码 | `a380b908a3b52dc35958775dd0f50644b163cdee`（`git archive` 解压到 AutoDL `/root/autodl-tmp/src/ecommerce-agent-runtime-a380b90`，`git get-tar-commit-id` 校验）；反事实分析脚本在 `c47247cdde5828e8524ae0d563388dabcc1bc3f3` |
| 环境 | AutoDL RTX 4090；客户端 `/root/autodl-tmp/venvs/qwen-vllm/bin/python`（vLLM 0.10.2，sentence-transformers 5.1.2）；Qwen3-4B-Instruct-2507，`start-qwen-vllm.sh`，temperature 0 |
| 索引 | `index_5k`：43,953 chunks，embedding `paraphrase-multilingual-MiniLM-L12-v2`，embedding sha256 `ad3da6d4…e9c89`，chunks sha256 `3ee9ca0d…1ba3`；由 ecommerce-cpu 环境构建 |
| 产物 | `/root/autodl-tmp/experiments/research_find_v1_20261005/`：`oracle.json` `beb5795b…`、`retrieval_top1.json` `f4d92b75…`、`native_exploration.json` `1edbf046…`、`native_exploration.sqlite` `86c52274…`、`native_exploration.query_counterfactual.json` `c41c5bf2…` |

| 臂 | 执行类别 | 范围 | 成功率 |
|---|---|---|---|
| Oracle | 特权接线检查，不是模型或 Agent 结果 | 200 | 200/200 |
| R：`retrieval_top1` | 确定性检索基线（原始请求检索一次，取第一名） | 200 | 25.5%（CI 19.5–32.0）；有答案题 51/150，gold 进前 5 为 88/150；无解题 0/50 |
| R：exploration | 同上 | 100 | 30%（CI 21–40） |
| A：Native Qwen3-4B | 真实模型，AutoDL | exploration 100 | 36%（CI 27–46） |

A 与 R 在 exploration 上配对：+6 个百分点，95% CI [−7, +19]，A 赢 27 题、R 赢 21 题，
**未显示显著差异**。分题型（A / R）：multi_constraint 44% / 60%，near_sku 24% / 40%，
no_answer 72% / 0%，typo_alias 4% / 20%。

A 的失败：`false-abstention` 49、`never-retrieved` 7、`answered-unsatisfiable` 7、
`retrieved-not-selected` 1。行为统计：平均每题 1.15 次搜索（87% 只搜一次），84%
从不调用 `get_product`；49 条误拒答中 43 条 gold 从未被检索到。

反事实（只重放 A 的第一次搜索，75 道有答案题，gold 进前 5）：原样重放 19，去掉
`category`/`max_price` 22，用用户原话 44。115 次搜索中 64 次带了请求没有提到的
`max_price`，87 次带了 `category`；`search_catalog` 的 `max_price` 会丢弃无价格商品
（约占目录 52%）。结论：主要损失来自模型改写后的查询文本本身，编造的过滤参数是次要
原因。

## Step 2：两个检索运行时开关（exploration）

代码 `db3374c23a620306c42c183688074a3236e372c5`（解压到
`/root/autodl-tmp/src/ecommerce-agent-runtime-db3374c`，`git get-tar-commit-id` 校验；
`index_5k` 软链接到上面同一份索引），环境与 Step 1 相同。两个开关默认关闭：

- `--ground-search-filters`（`ecommerce_rag/argument_grounding.py`）：用户消息里没有
  出现的 `max_price` 数值或 `category` 文本不应用，并在工具结果写入
  `argument_grounding.ignored_arguments`；轨迹同时保留模型请求的原始参数；
- `--search-query-fusion`（`ecommerce_rag/query_fusion.py`）：每次搜索另用最近一条
  用户消息检索，按 RRF 融合父文档排名；两段文本相同时直接透传。

| 配置（exploration 100） | 成功率 | 产物 sha256 前缀 |
|---|---|---|
| A，开关全关（`db3374c` 重跑） | 37% | `ad2b5318`（json）/ `8e854462`（sqlite） |
| A + 过滤参数溯源 | 43% | `18ec2398` / `3599200e` |
| A + 过滤参数溯源 + 查询融合 | 57% | `cbad5858` / `ca7bc324` |
| R + 两个开关 | 30% | `9f7bf199` |

配对（按题 bootstrap 95% CI）：

| 比较 | 差值 | CI | 变好 / 变差 |
|---|---|---|---|
| A 关（`a380b90`）→ A 关（`db3374c`） | +1pp | [0, +3] | 1 / 0 |
| A 关 → + 溯源 | +6pp | [−1, +14] | 11 / 5 |
| + 溯源 → + 溯源 + 融合 | +14pp | [+5, +23] | 18 / 4 |
| A 关 → + 溯源 + 融合 | +20pp | [+10, +30] | 25 / 5 |
| R → A + 溯源 + 融合 | +27pp | [+17, +38] | 32 / 5 |
| R → R + 两个开关 | 0 | [0, 0] | 0 / 0 |

同配置重跑只差 1 题。分题型（关 → 溯源 → 溯源+融合）：multi_constraint 44/52/60，
near_sku 28/32/64，no_answer 72/68/80，typo_alias 4/20/24。最终配置的剩余失败：
`false-abstention` 32（typo_alias 占 18），`answered-unsatisfiable` 5，
`never-retrieved` 3，`retrieved-not-selected` 2，`multiple-products-answered` 1。

这些改动是看过 exploration 失败后设计的，exploration 上的提升偏乐观；是否成立以
locked 为准。

## Step 3：locked 留出评估（一次性）

同一代码 `db3374c`、同一索引和服务配置，locked 100 题上"开关全关"和"两个开关
全开"各运行一次；运行后不再调整。R 的 locked 基线来自 Step 1（融合对 R 透传，已在
exploration 上验证不变）。

| 配置（locked 100） | 成功率 | 产物 sha256 前缀 |
|---|---|---|
| R：`retrieval_top1` | 21% | `f4d92b75`（Step 1 全量报告） |
| A，开关全关 | 40% | `de3770e0`（json）/ `66b3f0cf`（sqlite） |
| A + 溯源 + 融合 | 48% | `ca1f70b0` / `e34c2f5d` |

| 比较（按题 bootstrap 95% CI） | 差值 | CI | 变好 / 变差 |
|---|---|---|---|
| A 关 → A + 溯源 + 融合，全部 100 题 | +8pp | [−2, +19] | 19 / 11 |
| 同上，有答案 75 题（20 → 33 题答对） | +17.3pp | [+6.7, +28] | — |
| 同上，无解 25 题（20 → 15 题正确拒答） | −20pp | [−40, 0] | — |
| R → A 关 | +19pp | [+7, +32] | 31 / 12 |
| R → A + 溯源 + 融合 | +27pp | [+17, +37] | 30 / 3 |

分题型（关 → 开）：multi_constraint 20 → 48，near_sku 32 → 48，typo_alias 28 → 36，
no_answer 80 → 60；有答案题的 gold 检索率 30.7% → 58.7%。最终配置的失败：
`false-abstention` 23、`never-retrieved` 13、`answered-unsatisfiable` 10、
`retrieved-not-selected` 5、`multiple-products-answered` 1。

结论：留出集上，两个开关显著提高了有答案题的召回和正确率，但同时让无解题更容易
被近似商品误答；总体提升未达到显著。exploration 的 +20pp 在 locked 上缩小为 +8pp。
两个开关保持默认关闭。本 locked 已经使用，后续改动需要用生成器换种子重新生成
留出集再评估。

## Step 4：失败驱动的第二轮任务（闭环第 ⑤ 环）

`scripts/generate_research_find_tasks.py --evolve-from-report`（代码 `2279390`）读取
exploration 上最终配置的报告（AutoDL
`/root/autodl-tmp/experiments/research_find_v1_20261005/db3374c_native_exploration_grounding_fusion.json`，
sha256 `cbad5858…`），只用 exploration 的 43 个失败生成新任务；locked 结果不参与。
输出 `ecommerce_rag/data/research_find_r2.jsonl`（174 题，sha256 见 manifest），v1 生成结果
逐字节不变。

- **失败驱动 exploration（76 题）**：每个失败按"题型 + 约束结构"（如
  `near_sku | attribute:Color+brand`）在未用过的商品上生成 2 个变体；请求 86 题，10 题因
  结构稀有没有可用商品（manifest 的 `shortfall`）；每题记录 `driven_by`。
- **新 locked（98 题）**：不定向，换种子；排除所有用过的商品，以及 v1/r2 exploration
  出现过的全部（末级类目, 品牌）分组；按剩余配额分配题型。typo_alias 只有 23 题，
  其余三类各 25 题：在这些排除规则下合格商品已经用完，没有为凑数放宽隔离。
  **尚未运行。**

第二轮 exploration 结果（AutoDL，`2279390`，同一索引和服务；产物在
`/root/autodl-tmp/experiments/research_find_r2_20261005/`）：

| 配置（r2 exploration 76） | 成功率 | 产物 sha256 前缀 |
|---|---|---|
| R：`retrieval_top1` | 28.9% | `d11018e4` |
| A，开关全关 | 32.9% | `9a303bd6`（json）/ `af28a88b`（sqlite） |
| A + 溯源 + 融合 | 51.3% | `2b76ad63` / `a2df23d1` |

- 开关全关 → 全开：+18.4pp，CI [+6.6, +30.3]，变好 19 / 变差 5。这批题生成于开关设计
  之后，是对开关效果在新题上的复现，但仍属 exploration，不是留出评估。
- **定向效果有限**：同一最终配置下，变体失败 37/76（49%），其中只有 22/76（29%）复现
  了驱动它的失败类型；整体 51.3%，略低于 v1 exploration 的 57%。分题型（v1 → r2，
  最终配置）：multi_constraint 60 → 25，near_sku 64 → 61，no_answer 80 → 70，
  typo_alias 24 → 53。"题型 + 约束结构"不是失败的主要成因，下一轮应改为按失败机制
  定向，或先生成候选、用当前系统筛出失败样本再作为 exploration。
