# 实验记录索引

本页只做索引，不把实验代码、SQLite 或原始轨迹拼进 Markdown。当前清理候选是
`8ae1c0e4ad6ec62eb73a5f2b72a6a41910c389db`；清理前 `67566944` / `313 passed` 保留为
对照。主路径保持 legacy observation、full tools、answer contract off、retrieval
experience off。下表中的 compact observation、
selective tools、evidence-answer-v1、retrieval experience 都是隔离实验，不能当作默认
能力或已验证的模型收益。

## 读法与恢复边界

- `Executed` 表示有报告和执行配置；`Historical` 表示只保留旧结果；`Rejected` 表示
  候选未过门槛；`Not located` 表示路径或独立 provenance 尚未在当前环境找到。
- 原 dirty `main` 的共享实验不能拆成可独立运行的小 patch：它们共同修改
  `harness.py`、`native_tool_policy.py`、`domain.py`、`evidence.py`、`tool_schema.py`
  和相关测试。恢复依据是完整快照，而不是下面的文件名清单。
- 本轮保护快照在 `/tmp/ecommerce-agent-runtime-cleanup-protection-20260928/`：
  `tracked.diff` 的 SHA-256 为
  `834758f13831ca49a1ea3ec667688a090800c5c6c94c164270960fdc54ad46c7`；未跟踪源文件
  的 tar、清单和哈希位于同一目录。该快照已在干净 `4ca6525` worktree 中实际应用并
  逐文件校验，`tracked.diff` 和未跟踪源文件哈希均一致。
- AutoDL 目录是历史产物位置，不在本地仓库；本轮不重跑 Qwen、24-task 或 360-task。
  缺少独立文件哈希时明确标为待补 provenance。

## 隔离实验

| 实验 | 代码/测试/任务索引 | 原始产物与配置 | 结果与当前结论 |
|---|---|---|---|
| compact observation + selective tools | 共享 dirty 快照；`observation.py` `ac3dec863479ffb385e7ed7af81498a072127d24b9b0d8d45f06df973a675113`；`tool_selection.py` `9d078a5cc048cfe2407b050ce40e811c52bb05cfeef463d78cfde3d7a576407c`；`tests/test_observation_tools.py` `70921444298d1191f7ddf40d2d7ab2c2b0c4214fef2ae352fdaeaaa5b2df43bf`；任务 `complex_research_native_exploration_24.jsonl` `e8932061dfd149015dc584f5b6f403eb9def7a187d855e10b1671016f200a365`；矩阵 `native_observation_tool_ablation_matrix.json` `08f949a36b46630ca8d8c5339b8fa498648559dffee1716c53f191bd61318637` | AutoDL `/root/autodl-tmp/experiments/native_observation_pair_20260924_final/`；代码指纹 `de1e26b4b882c607d3acd2c24a5e073f78c16dd8b48715153ee93ba68cbba8e8`；四配置均固定 Qwen、24 条配对任务、index、Skill、seed、DB/session、解码和停止逻辑 | `baseline_legacy_full` 为 `16/24`、`6891` token、`2.47s`；`compact_full` `11/24`、`11928`；`legacy_selective` `14/24`、`3700`；组合 `12/24`、`8601`、`3.42s`。compact 对 comparison 有局部改善，但整体任务成功和成本不支持默认开启；selective 降 token 但降低任务/joint 结果。隔离保留。 |
| evidence-answer-v1 | 共享 dirty 快照中的 `evidence.py`、`harness.py`、`native_tool_policy.py`；`scripts/validate_evidence_answer_contract.py` `fe4a53eedeaebc59a7393b2d61a947366ef5c620a3fded90fadcf0b9b74788cd`；`tests/test_task_scoring_contract.py` `f1dbc7098792b5d216ec8724d54633d62a1df9a962d761ecbaba85e2aa6de27a`；任务同上 | AutoDL `/root/autodl-tmp/experiments/native_answer_contract_20260924_final_retry/`；候选代码版本 `ac89679c3839a0c90a66b2816ae6011279f45cb156229a105b0567f823db79e2`；四个旧 store 的离线复评未覆盖原始 JSON/SQLite | 离线 96 条：`fact_correctness=95/96`、`condition_evidence_coverage=0/92`、`source_support=0/96`、`workflow_completion=53/96`。Native 候选相对历史 baseline：task `15/24` vs `16/24`，joint `3/24` vs `1/24`，source binding `4/24` vs `0/24`，token `8433.8` vs `6890.8`。来源绑定有改善但 task/成本门槛未过，隔离保留。 |
| retrieval experience | `retrieval_experience.py` `97e9027d2f4fffe2fc5b9f50bb8bbd780dfd49374fd0e6be55f2e60544a79d22`；`tests/test_retrieval_experience.py` `c1c1f7595d70dc371b414cf52b1fad193015dd5702fd278ef1451ead22b43191`；holdout `complex_research_experience_holdout.jsonl` `7f70178d86e36e589ab9619e3bea21495c01b6168edf52e8c128d80e5ce8d80b` | 候选入口是 `scripts/run_research_trial.py --retrieval-experience`；原始验证目录未在当前本地环境定位 | `real-model-validation-v1` 状态为 `rejected`，原因和原始报告/SQLite provenance 未在本地复核；默认关闭，不移入稳定路径。 |
| CPU complex-research contract | 共享 dirty 快照；`complex_research_cpu_contract.jsonl` `53bed78adfba6af573196cbfd717992cc1a422e75d9ea56aac8dc07faf51878b`；`tests/test_research_state.py` 与 modified harness 测试 | `/tmp/complex_research_cpu_contract.*` 曾作为本机临时产物；不纳入稳定基线 | dirty worktree 记录为 RulePolicy wiring：5/5 task success、policy compliance 1.0；只证明接线和 fail-closed 边界，不能证明模型收益，且本轮不重跑。 |

实验原始 SQLite/JSON 不进入仓库，也不把共享 dirty diff 宣称为独立可运行 patch。需要
恢复时先使用完整 tracked diff 和未跟踪源归档，再按各实验 manifest 的代码、任务、配置、
评分版本和产物路径逐项重建。

## 外部机制背景（非依赖）

- Open Deep Research 的参考点是“工具结果回到状态，再决定是否继续研究”；本项目只
  保留 history/evidence 循环和预算边界，没有移植其 LangGraph 或多 Agent runner。
- Recuris 的参考点是 frozen agent、结构化轨迹和 paired validation gate；本项目只借鉴
  可审计轨迹与 exploration/validation 分离，没有生成 Skill、自动 patch 策略或运行训练。

这些参考用于解释实验设计，不是本仓库的外部调用者、运行时依赖或模型效果证据。

## 已提交的历史 manifest

这些 JSON 是机器可读记录，内容和路径保持不变；下面的哈希用于区分同名重建文件。

| 文件 | 状态 | SHA-256 |
|---|---|---|
| `return_closure_candidate_v1.json` | `no_candidate` | `b7f345a9f48f5b3fb4650f5fab4a0fa7e0fa53dae0f94ad9f288a4fe5e383758` |
| `return_closure_deterministic_smoke_v1.json` | deterministic offline | `e002a1c0bdfced1b010b6f242d086be4f8519d70e7a8365146d8e5ecd868d1d5` |
| `return_closure_exploration_deterministic_v1.json` | deterministic offline | `a24b521bc411becfee93c318828c0f777d23888f749e0d64a225d1010693fc60` |
| `return_closure_validation_deterministic_v1.json` | deterministic offline | `3314cf8f4dd04f65a4c9759a5b93c4847a06b00f445b65e6feeee65a39e3f4ab` |
| `return_closure_locked_deterministic_v1.json` | deterministic offline | `fa0b24bd7c875a860621092908fa4f16d5fa0ae6d2d61f1b90d935ca1be62f49` |
| `return_closure_native_not_executed_v1.json` | not executed | `aaa9f2b16f3cbce8809e4662d4aa0f51dd5fd4bad4fd04b6452af31bfd26a1c6` |
| `return_closure_trial_status_v1.json` | status/attribution index | `8fc1c14602e39c9e7e49230dbf519479b87384673fa3c20e7dab0f4d43a116aa` |
| `return_closure_freeze_v1.json` | not run until endpoint | `7ec5f8b62efebe9cba3e36b7d47388baa2827c82c85729d3df00acf39d47cd4a` |
| `return_closure_freeze_v2.json` | not run until endpoint | `00e80f93404bb3ad4c7f12e6297db2eef35fecf1d01a7b5a717a38b86591c531` |
| `tau3_g0e_context_compaction_offline.json` | offline counterfactual | `f422d105e280a12c15657ecccd18e5449d251ec1b1c642fd54492fa669ab69be` |

## 303/360 provenance

用户确认前两天已完成一次真实 `303/360` 复测。本轮只读定位到的同名文件是历史汇总：

- `docs/harness_v2_llm_360_regraded_v2.json`：当前文件 SHA-256
  `b30e34b6920ab3b61516c0313020232594e53c5b5f134f5b9f2df56ef08cbfba`，Git blob 为
  `6376b8a4c1a963e4192bc9d0daaaa630d8cb0e2e`；文件内记录 120 tasks、360 trajectories、
  `operational_success=303/360`，评分 schema v2。
- 该 JSON 声明的 source store 是 `logs/harness_v2_llm_360.sqlite`，hash 为
  `37e13a3a19c5780793c4f3e99a7b095eaabf9feae9e7c3f5a54693b668fa1408`，但该 SQLite
  不在当前本地 checkout。
- 当前环境没有 `/root/autodl-tmp`，因此没有定位到用户所指近期复测的独立 JSON、SQLite、
  日志或 AutoDL 目录。任务集、模型/解码配置、近期代码状态和近期评分版本的独立
  provenance 均待补；本轮不质疑用户确认的数字，也不把历史 JSON 冒称近期复测。
