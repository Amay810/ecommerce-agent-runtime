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
原因；locked 尚未运行，本轮没有做任何改进。
