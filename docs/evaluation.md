# 评分版本与历史结果

本页只定义评分语义和结果 provenance。CPU/Rule/Oracle 结果不作为模型质量；不同
代码、任务、配置或评分版本的数字不能直接拼接。

## 评分合同

| 版本 | 定义 | 使用方式 |
|---|---|---|
| `harness-v1` | 历史 operational contract；不要求最终回答非空即可判 operational success | 保留为历史默认/可比基线 |
| `harness-v2-terminal` | 保留 v1 的 state/tool 检查，并要求显式 `termination_reason=final_answer` 或成功 handoff；空回答、max steps、budget exhausted、不可用 typed input 不算完成 | 仅显式启用，写入独立报告 |
| `return-closure-v2` | 退货闭环历史合同，包含其自身 tool/answer 规则 | 只读取带该版本的旧任务和报告 |

`joint_success`、`task_success` 和 terminal 规则均按 `scoring_version` 解释。新定义
不能覆盖旧 JSON/SQLite，也不能把 dirty 状态下的重算称成原实验复现。重算必须保留
原始 grade、代码 revision、任务集和新版本结果的并列关系。

当前 runtime 清理候选 `8ae1c0e` 的 CPU 回归为 `262 passed`。清理前 `67566944` 的
`313 passed` 保留为对照；差异是删除 51 个旧 LLM 专属测试，不改变历史评分语义或
`303/360` 的 provenance。

## Tracked 360 summary（历史汇总）

`docs/harness_v2_llm_360_regraded_v2.json` 是当前仓库保留的 machine-readable summary，
不是本轮文档整理新生成的报告。它记录 120 tasks、360 trajectories 和 schema v2：

| 指标 | All | Dev | Locked |
|---|---:|---:|---:|
| operational success | 84.17% (`303/360`) | 83.33% | 85.00% |
| policy compliance | 95.00% | 95.00% | 95.00% |
| terminal-state accuracy | 100% | 100% | 100% |
| forbidden-tool attempt | 5.00% | 5.00% | 5.00% |
| illegal state change | 0% | 0% | 0% |

JSON 中的 `legacy_automatic_operational_success=94.17%` 仅为历史兼容字段，不能替换
当前 `303/360` operational headline。40-row audit 与 operational grader 的 agreement
不是对全部轨迹的外推，也不构成训练或 RL 结论。

文件 provenance：tracked JSON SHA-256 为
`b30e34b6920ab3b61516c0313020232594e53c5b5f134f5b9f2df56ef08cbfba`，Git blob 为
`6376b8a4c1a963e4192bc9d0daaaa630d8cb0e2e`。JSON 声明的 source store 是
`logs/harness_v2_llm_360.sqlite`，source-store hash 为
`37e13a3a19c5780793c4f3e99a7b095eaabf9feae9e7c3f5a54693b668fa1408`；该 SQLite 不在
当前 checkout。

## 用户确认的近期 303/360 复测

用户确认前两天完成了一次真实 `303/360` 复测。本轮只读检查了仓库、当前本地临时目录
和可见的 AutoDL 路径：没有找到该运行独立的 JSON、SQLite、日志或报告目录；当前环境
也没有 `/root/autodl-tmp`。因此：

- 保留 `303/360` 作为用户确认的近期真实数据，不降格为“未经复测”；
- 不把它与上节 tracked historical JSON 视为同一运行；
- 近期任务集、模型/解码配置、代码状态/hash、评分版本和原始产物 hash 标为待补
  provenance；
- 本轮不重跑实验，也不为了补 provenance 覆盖或重写现有 JSON/SQLite。

## 其他结果的解释边界

- `313 passed` 是稳定基线 `67566944` 的本机 CPU 回归记录，证明接口/接线和测试
  合同，不是模型成绩；本轮文档整理不重跑。
- compact observation、selective tools、evidence-answer-v1、retrieval experience
  的结果、配置和正负结论见 [实验索引](experiments/README.md)，不进入本评分 headline。
- 任何新的评分报告必须写 `scoring_version`，并同时保留 `code_revision`、任务集、
  raw artifact path/hash、执行环境和是否真实模型。
