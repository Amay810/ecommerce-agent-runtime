# 当前状态

这是仓库的短状态入口。调用链、接口和逐文件地图见
`docs/architecture.md`；环境身份、命令和验证出处见 `docs/reproduction.md`。
当前 HEAD 不在文档中硬编码，进入项目时以 `git rev-parse HEAD` 为准。

## 代码与主路径

- 本轮整理开始时的本机与 AutoDL 主线均为 `1379ec9`；随后若只修改文档，
  不改变运行时代码结论。
- 退货评分冻结基线仍是 `0bb1952` 及其 v2 contract 历史；整理提交没有改写
  旧评分或实验产物。
- 主路径是 `HarnessRunner` + `NativeToolPolicy` + `RetailTools.call`，连接
  SQLite、可选 hybrid retrieval、evidence 和 typed confirmation。
- CPU 验证路径是 `RulePolicy`；它验证 wiring，不是模型或 Skill 效果。
- `LLMPolicy` 是旧 JSON 兼容路径；MCP 和 Tau3 是可选适配路径。

## 本轮与历史验证的区别

| 记录 | 是否本轮重新执行 | 具体命令/范围 | 结果与用途 |
|---|---|---|---|
| 本机完整回归 | 否，复用历史记录 | `.venv/bin/python -m pytest -q`，代码树 `ecb3e3d` | `291 passed in 6.95s`；完整回归历史证据，不是本轮文档修改后的新执行。 |
| AutoDL 完整回归 | 否，复用更早记录 | 旧 AutoDL checkout + 当时两处未提交消息来源修改 | `291 passed, 2 warnings`；历史 CPU 证据，不等同于当前 HEAD 的重新回归。 |
| 本机窄测试 | 是 | `tests/test_native_tool_policy.py tests/test_agent_runtime.py tests/test_harness_tools.py`，`.venv`，`bd65a71` | `28 passed in 0.19s`；消息来源、Native/Runtime、Harness/评分边界。 |
| 本机导入检查 | 是 | `from ecommerce_rag.harness ... NativeToolPolicy ... RetailTools` | `imports-ok`；主入口和关键模块可导入。 |
| 本机 CPU smoke | 是 | `ecommerce_rag.harness run --tasks ecommerce_rag/data/harness_contract_smoke.jsonl --policy rule --repeats 1 --seed-db` | 4 条；task success、policy compliance、terminal-state accuracy 均 `1.0`；只证明 RulePolicy wiring。 |
| AutoDL 窄测试 | 是 | `/root/autodl-tmp/venvs/qwen-vllm/bin/python -m pytest` 加上述 3 个测试文件，`CUDA_VISIBLE_DEVICES=` | `28 passed in 4.69s`；证明 AutoDL CPU 解释器能执行相关合同。 |
| AutoDL 导入检查 | 是 | 同一 qwen-vllm 解释器导入 `HarnessRunner`、`NativeToolPolicy`、`RetailTools` | `autodl-imports-ok`。 |

最后一个提交 `1379ec9` 只补全了文件地图中的路径名称；没有运行时代码变化，
所以复用 `bd65a71` 的窄测试是有边界的。以上命令和结果集中记录在
`docs/reproduction.md`，不是把“291 passed”误写成本轮重新执行。

## 环境现状

- 本机：uv `.venv` 可用；本机 `/usr/bin/python3` 缺少 `ensurepip`，Docker
  已安装但当前用户无 Docker socket 权限。
- AutoDL：仓库可达，`main` 与本机当前提交一致；无 GPU，`127.0.0.1:8123`
  vLLM 未启动，属于本轮预期状态。Qwen 模型文件和启动脚本仍在机器上。
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
validation 或 locked，也没有改变评分、交互协议和业务代码。
