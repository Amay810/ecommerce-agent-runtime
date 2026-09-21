# 当前状态

这是仓库的短状态入口。调用链、接口和逐文件地图见
`docs/architecture.md`；环境身份、命令和验证出处见 `docs/reproduction.md`。
当前 HEAD 不在文档中硬编码，进入项目时以 `git rev-parse HEAD` 为准。

## 代码与主路径

- 本轮从本机 `main` 工作树开始；本轮新增的运行时代码、任务和测试已整理为
  范围清楚的本机提交，当前 HEAD 以 `git rev-parse HEAD` 为准。AutoDL 尚未同步。
- 退货评分冻结基线仍是 `0bb1952` 及其 v2 contract 历史；整理提交没有改写
  旧评分或实验产物。
- 主路径是 `HarnessRunner` + `NativeToolPolicy` + `RetailTools.call`，连接
  SQLite、可选 hybrid retrieval、evidence 和 typed confirmation。复杂咨询的
  B 路径在每次决策前从现有 history/evidence 派生 `ResearchState`，不另建业务
  状态库。
- CPU 验证路径是 `RulePolicy`；它验证 wiring，不是模型或 Skill 效果。
- `LLMPolicy` 是旧 JSON 兼容路径；MCP 和 Tau3 是可选适配路径。
- 下一轮只安排两条 exploration 的 Native A/B 小试跑：
  `research_exp_01_earbuds_policy` 与 `research_exp_05_preorder_shipping`，共四条
  轨迹；validation 暂保留。

## 本轮与历史验证的区别

| 记录 | 是否本轮重新执行 | 具体命令/范围 | 结果与用途 |
|---|---|---|---|
| 本机完整回归 | 是 | `.venv/bin/python -m pytest -q` | `299 passed in 6.48s`；覆盖新增补证状态以及受影响的共享 harness/domain/schema。 |
| AutoDL 完整回归 | 否，复用更早记录 | 旧 AutoDL checkout + 当时两处未提交消息来源修改 | `291 passed, 2 warnings`；历史 CPU 证据，不等同于当前 HEAD 的重新回归。 |
| 本机窄测试 | 是 | `.venv/bin/python -m pytest -q tests/test_research_state.py tests/test_native_tool_policy.py tests/test_harness_tools.py tests/test_tool_schema.py tests/test_evidence_grounding.py` | `81 passed`；补证状态、Native 投影、Harness 预算、schema 和 evidence。 |
| 本机导入检查 | 是 | `from ecommerce_rag.harness ... NativeToolPolicy ... RetailTools` | `imports-ok`；主入口和关键模块可导入。 |
| 本机 CPU smoke | 是 | `ecommerce_rag.harness run --tasks ecommerce_rag/data/harness_contract_smoke.jsonl --policy rule --repeats 1 --seed-db` | 4 条；task success、policy compliance、terminal-state accuracy 均 `1.0`；只证明 RulePolicy wiring。 |
| 本机复杂咨询 A/B | 是 | `.venv/bin/python -m scripts.run_research_trial --policy rule --fixture ...` | exploration 6 条、validation 2 条；B 能连续执行检索并记录 research spans；规则结果不是模型收益证据。 |
| AutoDL 窄测试 | 否；执行于 `bd65a71`，后续纯文档提交复用 | `/root/autodl-tmp/venvs/qwen-vllm/bin/python -m pytest` 加上述 3 个测试文件，`CUDA_VISIBLE_DEVICES=` | `28 passed in 4.69s`；证明 AutoDL CPU 解释器能执行相关合同。 |
| AutoDL 导入检查 | 是 | 同一 qwen-vllm 解释器导入 `HarnessRunner`、`NativeToolPolicy`、`RetailTools` | `autodl-imports-ok`。 |

完整回归为 `299 passed in 6.48s`；复杂咨询 validation 也已用 CPU fixture
执行。不会把 deterministic A/B 的 task score 当作真实模型或泛化收益。

## 环境现状

- 本机：uv `.venv` 可用；本机 `/usr/bin/python3` 缺少 `ensurepip`，Docker
  已安装但当前用户无 Docker socket 权限。
- AutoDL：本轮未连接、未同步、未启动；上一轮环境记录中的无 GPU/
  `127.0.0.1:8123` 未启动状态仍只作背景，不作为本轮执行证据。
- GitHub Actions：当前 checkout 没有 `.github/workflows`，没有 CI 执行证据。

## 消息来源修复与 stash

`7b52e80` 的 `NativeToolPolicy.act` 和
`tests/test_native_tool_policy.py` 已进入当前主线。相关合同是：非空
`history` 为唯一消息来源，工具结果保持 `tool`，真实用户重复文本保留，
synthetic tool-call/result id 配对。

AutoDL 的 `stash@{0}` 是对齐前保存的两文件版本，当前工作树没有使用它。静态
对比确认：测试增量与 `7b52e80` 对应；唯一独有实现是旧版在非空 history 且最新
事件为 user、但 `current_message` 不在序列化消息中时追加 user 的兼容分支。该
分支不符合当前“非空 history 唯一来源”的约束，因此被当前提交完整取代；stash
仅作为可恢复记录保留。

## 实验边界

Skill v0 仍 active；Skill v1 历史上已拒绝。历史 Qwen exploration/validation
发生在消息来源修复之前，不能重新标为修复后结果。本轮没有运行真实 Qwen、
AutoDL、托管模型或 locked；没有生成新 Skill，也没有改变评分、交互协议、
confirmation 授权和写入边界。新任务分为 exploration 与 validation，validation
仅准备并做 CPU 接线检查，真实模型验证留到后续。
