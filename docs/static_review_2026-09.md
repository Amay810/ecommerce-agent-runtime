# Static review and repair record

审查范围：`ecommerce-agent-runtime` 当前工作树，覆盖核心运行时、Direct 与
MCP 工具边界、原生/旧版模型适配器、SQLite 工具、检索、Harness/评分、Skill
实验脚本、τ³ 可选适配器、配置、测试和复现文档。审查不启动 GPU、不调用真实模型，
也不修改 `skills/return_request/SKILL.md`。

## 执行地图

| 路径 | 当前用途 | 依赖和边界 |
|---|---|---|
| `ecommerce_rag.harness` + `RulePolicy`/`OraclePolicy` | CPU 确定性 smoke、回放、评分 | 不代表模型质量；Oracle 只作上界 |
| `NativeToolPolicy` + `AgentRuntime` | 当前真实模型实验路径 | OpenAI-compatible `chat/completions` wire protocol；模型默认是 Qwen3-4B，服务地址必须显式配置 |
| `LLMPolicy` | 旧版 JSON action envelope 与本地/兼容后端 | 保留兼容路径；不与 Native 路径的 trace 或协议混称 |
| `MCPRetailFacade` | 可选外部 MCP 暴露 | 所有业务调用仍进入 `RetailTools.call`；用户身份由服务端配置 |
| `HybridRetriever` | 可选产品/政策检索 | SentenceTransformers；FAISS 可选，缺失时 NumPy exact；BM25/RRF 在 CPU 可运行 |
| `scripts/run_skill_trial.py` | A/B/C Skill 实验编排 | A 无 Skill，B v0，C 只接受失败驱动候选；Rule 结果标为离线，不是模型结果 |
| `tau3_*`、`nscc/`、Amazon/训练脚本 | 可选或历史/外部评测路径 | 需要单独 checkout、GPU 或外部服务；不属于本轮 CPU 验收 |

### 关键调用链

1. **模型与交互**：`HarnessRunner.run` 构造不含隐藏 gold 的
   `AgentObservation`，`NativeToolPolicy.act` 经过 `AgentRuntime.prepare_messages`
   注入 system/Skill，向兼容端点发起一次生成；原生 tool call 由 `_to_action`
   转成 `AgentAction`，再回到 Harness。`request_user_input` 通过
   `requested_input_type` 驱动 `UserSimulator`；普通 content-only 输出按终止回答处理。
2. **确认与写入**：业务检查通过后，Harness 调用 `issue_confirmation`；用户模拟器的
   回复经过 `ConfirmationLedger.respond`，只有绑定 session、user、operation 和参数哈希
   的授权才会传给 `RetailTools.call` 的写工具。SQLite 条件更新负责幂等版本递增。
3. **证据**：每个成功工具结果经 `convert_tool_call_to_evidence` 进入 evidence ledger；最终
   `verify_answer` 做引用绑定、冲突和高风险事实检查，`GradeResult` 同时保留运营成功与答案诊断。
4. **任务、Harness、评分**：`load_tasks` 读取 `TaskSpec`；gold、允许工具、终态和评分字段
   只留在 Harness，不能进入 `AgentObservation`。`grade` 评估工具集合、禁止工具、状态、检索
   gold、序列、handoff 和答案。当前 `allowed_tools` 的历史语义是“期望工具全集”，不是可选集合；
   这项命名歧义尚未迁移，因为改动会改变已冻结报告。
5. **Skill**：`load_skill` 校验 front matter 并计算内容哈希；Native runtime 将内容放入
   system prompt，trace 只记录版本/哈希等 provenance。A/B 脚本每个 task/arm 重置数据库；
   `propose_skill_patch` 只有在 B 存在真实失败时才生成候选，`compare_skill_trials` 只在真实
   validation B/C 都执行时评价晋级。
6. **Direct 与 MCP**：Harness/诊断可直接调用 `RetailTools.call`；MCP facade 注入服务端
   `user_id` 和 session 后也走同一个执行边界，MCP 不是旁路实现。
7. **检索**：`retrieval_index.build_index` 生成 embeddings/chunks/parents 和 manifest；
   `HybridRetriever` 先校验 manifest，再加载或重建 BM25/FAISS，之后执行 dense + BM25 + RRF。
   检索索引是可复现工件，不应提交模型文件、缓存或凭据。

## 修复记录

| ID | 严重性 | 位置/触发 | 实际问题与证据 | 修复状态 |
|---|---|---|---|---|
| RV-01 | High | `RetailTools.call`；已取得授权后传 `confirmed="false"` | 政策层会校验 schema，但 Direct、诊断和 MCP 共用的最终 dispatch 边界此前没有校验；Python 的 `bool("false")` 会令写操作继续。新增 `test_direct_dispatch_rejects_string_confirmation_before_authorized_write` 证明修复前可穿透。 | Fixed：dispatch 前统一 `validate_arguments`，失败记录为 ToolCall 错误且不写库 |
| RV-02 | High | `MCPRetailFacade.issue_confirmation`；callback 参数含伪造 `user_id` | `{"user_id": self.user_id, **arguments}` 的后者覆盖前者，确认记录可能绑定错误用户。新增 MCP 单测直接检查 ledger。 | Fixed：服务端身份最后合并，并保留回归测试 |
| RV-03 | Medium | Native `request_user_input`；message 语义不含关键词或 input_type 非法 | 声明的 `input_type` 被丢弃，Harness 只能猜测；非法枚举也可能被接受。新增 typed action、枚举/额外参数校验，并把 `reason` 映射到内部 `return_reason`。 | Fixed |
| RV-04 | Medium | `freeze_return_closure.py` 读取 `ERAG_SIMULATED_TODAY`，但工具构造器使用硬编码日期 | 修改环境日期只改变 manifest，不改变 `check_return_eligibility` 的实际窗口，实验不可复现。新增跨日期单测。 | Fixed：`RetailTools` 从环境读取冻结日期，显式构造 `today` 仍可覆盖 |
| RV-05 | Medium | `HybridRetriever` 加载旧 BM25/FAISS 工件 | 同名缓存此前只按路径加载；BM25 文档数错位会静默漏召回，FAISS 内容/维度/数量不符可能延后失败。新增 manifest（模型、chunk、parent、embedding hash）、结构检查和 BM25 重建，并在 FAISS 内容、维度或数量错位时重建。 | Fixed；没有 manifest 的旧索引需重建 |
| RV-06 | Low | `scripts/run_skill_trial.py --split smoke` | 数据有 `smoke` split，但 CLI choices 缺少它；实际 smoke 命令只能报参数/筛选错误。 | Fixed：加入 `smoke`，并用四任务 Rule smoke 验证 |
| CFG-01 | Medium | Native 环境缺省值 | 代码原先缺省 `api.openai.com`/`gpt-4o-mini`，但项目冻结模型是 Qwen3-4B-Instruct-2507；“OpenAI”在这里是 wire protocol，不应成为隐式模型选择。 | Fixed：Native 缺省模型改为 Qwen，base URL 必须显式设置；freeze manifest 记录兼容协议和实际配置。旧 `LLMPolicy` 仍是兼容/历史路径，不能拿它作为 Native 实验配置 |

## 有意未改与当前实验限制

- 真实 Qwen 轨迹中，模型在确认前成功调用了身份验证和资格工具，但随后用普通文本询问确认，
  没有调用 `request_user_input(input_type=confirmation)`。当前协议把 content-only action 当作最终回答，
  因而 Harness 不应擅自把普通文本升级为授权交互；该轨迹应归为模型策略/协议遵循失败，而不是静默修复成合法写入。
  这解释了此前 0/4，而不证明 Skill 有效性。
- `TaskSpec.allowed_tools` 实际按“全量期望工具集合”计算 recall；若要支持“get_order 或
  check_return_eligibility 二选一”等替代组，需要单独迁移字段、任务文件和 scorer 版本，不能在本轮悄然放宽评分。
- `evidence.py` 将引用缺失和部分 unsupported facts 作为诊断项，hard verification 主要拦截冲突/非法引用。
  这是一项已冻结的评分设计选择，不在本轮改动；报告必须同时看 `answer_fact_pass` 与 citation diagnostics。
- 本轮没有运行真实模型、Skill A/B、validation 或 locked；CPU 279 passed 只证明工程契约和离线夹具。

## 验证命令与结果

```text
python -m compileall -q ecommerce_rag scripts tests       PASS
python -m pytest -q                                      279 passed
python -m scripts.run_skill_trial --policy rule \
  --tasks ecommerce_rag/data/return_closure_smoke.jsonl \
  --split smoke --arm A ...                              4/4 offline pass
```

`mcp` 已在当前 CPU 虚拟环境安装，MCP 集成测试不再跳过。没有启动模型服务，没有读取或写入真实凭据，
也没有把 `/tmp` 中的 smoke 工件纳入仓库。
