# 架构与文件地图

这是仓库的 canonical handoff map。先读 `AGENTS.md`；短状态见
`docs/current_status.md`，环境和命令见 `docs/reproduction.md`。

## 证据标签

- **Executed**：实际执行过命令，并记录了环境、版本、范围和结果。
- **Static**：由当前源码、schema、import 或配置核对得出，不等于运行验证。
- **Historical**：保留原始 revision、策略和执行类别的旧实验或报告。
- **Pending**：本轮有意没有执行的项目。

代码职责以本轮开始核对时的本机工作树为基线；本轮新增运行时代码和任务已整理为
范围清楚的本机提交。下面覆盖主路径和本轮新增文件；locked task 只登记路径，没有
打开内容。

## 主调用链

```text
ecommerce_rag.harness CLI
  -> HarnessRunner.run(TaskSpec)
  -> AgentObservation(history, session, public tool schemas)
  -> NativeToolPolicy / RulePolicy / compatibility LLMPolicy
  -> AgentAction
  -> RetailTools.call(name, typed arguments, session/confirmation context)
  -> SQLite, policy corpus or HybridRetriever
  -> tool event in history and evidence ledger
  -> derived ResearchState (optional B context) and next read-only decision
  -> typed user input, final answer, handoff, or max-step stop
  -> TrajectoryStore + grade(TaskSpec, Trajectory)
```

当前支持的 Qwen 路径是 `NativeToolPolicy`。provider 使用
OpenAI-compatible chat-completions wire format，但模型是本地 Qwen，不是
OpenAI 托管模型。`RulePolicy` 只用于 CPU wiring 验证；`LLMPolicy` 是旧的
JSON envelope 兼容路径。Tau3 通过独立 adapter 接入，不属于本机 CPU smoke。

## 模块接口与职责

### Policy 输入与动作转换

`domain.py` 定义 `TaskSpec`、`AgentObservation`、`AgentAction`、`ToolCall`、
`Trajectory` 和 `GradeResult`。`TaskSpec` 保存 hidden scoring expectations 与
可选 research budget；`AgentObservation` 是 policy 能看到的 projection，hidden
字段不能进入其中。`Trajectory.research_spans` 只审计每次研究决策前后的派生状态。

`harness.py` 的 `HarnessRunner.run` 建立 session，追加 user/assistant/tool
事件，调用 `policy.act`，通过 `UserSimulator` 处理 typed input，记录
 confirmation spans，快照 SQLite，并调用 `grade`。启用 research 后，它从已有
 `history + evidence_ledger` 派生一次 `ResearchState`，向具备 capability flag 的
 policy 传递，并在 read-only retrieval 超过预算时 fail closed。`TrajectoryStore` 将 trace
和 grade 持久化到 SQLite。

`native_tool_policy.py` 的 `_history_messages` 将 canonical history 转成
provider messages。非空 `history` 是唯一消息来源；assistant tool call 和后续
`role=tool` 结果通过 `call_history_*` 配对；只有空 history 才用
`current_message` 作为初始 user。`_to_action` 校验单个 provider tool call，
注入 harness-owned `user_id`，保留 `request_user_input(input_type=...)`，并
生成 `AgentAction`。

`agent_runtime.py` 的 `AgentRuntime.prepare_messages` 生成 system/policy/Skill
prompt，补充 tool name 并对 provider copy 做可选 compaction；`validate_generation`
和 `run_turn` 是较低层的 native/Tau3 protocol contract。当前 Native 为保持
历史兼容仍有自己的 adapter loop。

`llm_policy.py` 只解析旧 JSON action envelope，`llm.py` 是其 completion helper；
两者都不是当前 Qwen tool-call 主路径。`skill_loader.py` 和
`skills/return_request/SKILL.md` 只提供可选的 versioned prompt guidance，不能
改变 schema、权限、confirmation、评分或 SQLite。

### 复杂咨询补证

`research_state.py::derive_research_state` 不维护第二份业务事实；它从 canonical
history 与成功工具结果生成用户约束、已取得事实及 source id、缺口、执行过的查询、
retrieval 次数和剩余预算。`render_research_state` 只在 B 的
`NativeToolPolicy(research_context=True)` 中追加 provider-visible context，且不
包含 `TaskSpec.answer_expectations`、`evaluation_contract` 或 `gold_doc_ids`。
`budget_exhausted_answer` 在预算耗尽时列出仍无法确认的缺口。

`research_policy.py::EvidenceStateRulePolicy` 是 CPU-only wiring 替身：它按缺口
决定继续 `search_catalog`、`get_product`、`compare_products`、`get_policy`、
`get_order` 或明确未知，不是模型策略。`research_fixture.py` 只把已提交的商品/政策
JSONL 做确定性 lexical 检索；生产路径仍是 `HybridRetriever`。

A/B 的含义是：A 使用当前 policy-visible retrieval 行为；B 保持同一任务、工具、
模型（Native 时）、seed、session reset、budget 和 max steps，只增加结构化补证
状态及其 trace。B 不修改 Skill、权限、确认协议或写工具。

### 可信执行与状态

`tool_schema.py` 是 canonical JSON Schema、参数校验和 prompt rendering 来源。
`user_id` 对内部 trusted call 存在，但在 Native provider 边界隐藏并注入。

`tools.py` 的 `RetailTools.call` 是 Direct 和 MCP 共用的 final typed dispatch
boundary：校验参数、执行 identity guard、调用 registry、记录 `ToolCall`，并
返回结构化错误。`get_policy`、order、eligibility 是 read tools；写工具负责
条件 SQLite update 和幂等 replay。

`confirmation.py` 的 `ConfirmationLedger` 属于一个 runtime boundary。host 在
typed confirmation action 后 issue request，记录真实 user response，只有匹配
session、user、operation 和 canonical parameter hash 的 authorization id 才能
传给 write。

`orders.py` 管 SQLite schema、seed/reset、authenticated read 和 snapshot；业务
guard 与 mutation 在 `tools.py`。`retail_protocol.py` 提供 Tau3/compiler 的
额外 write surface，但仍由 `tools.py` 执行。

`mcp_server.py` 只注入 server-side user/session 并委托 `RetailTools`，不另造
authorization。`evidence.py` 将成功 tool result 转成 evidence 并核验回答，
`freshness.py` 诊断 `updated_at` 新鲜度；两者不改变工具状态。

### Retrieval 与展示

`retrieval_index.py` 从 product/policy JSONL 构建 chunks、embeddings、parent cards
和 manifest；`hybrid_retriever.py` 校验 manifest 后组合
SentenceTransformers/FAISS 或 NumPy、BM25/RRF 和可选 reranker。生成 index 被
忽略，不应提交。`catalog.py` 负责确定性的推荐/比较展示；`config.py` 负责
环境路径和 retrieval 参数；`context_compaction.py` 只压缩 provider copy，
不修改存储的 trajectory history。

### Harness、兼容路径与诊断

- `legacy_closure.py` 是可选的 return-workflow progress reducer；
  `action_constraint.py` 根据它做单步 remap/fail-closed，默认 CLI 不启用。
- `legacy_closure_benchmark.py` 提供旧 return workflow benchmark、数据库 clone
  和 protocol gate；`phase1_write_gate.py` 是 model-free 的 synthetic write-gate
  probe/scorer，不是主 harness。
- `process_audit.py` 审核外部 simulation 的 read/confirmation/write 顺序；
  `diagnostics/transaction_audit.py` 做 guarded-vs-unsafe differential replay，
  其中 `UnsafeRetailTools` 只用于诊断。
- `tau3_agent_adapter.py` 将 Tau3 的 `generate_next_message` 接到
  `AgentRuntime`；外部 Tau3 负责外围环境。`tau3_retail_v1.py` 校验 pinned
  Tau2 checkout、构造命令并核验返回配置/result。
- `verified_sft.py` 将已审计轨迹转换成 leakage-aware structure/process split，
  供可选数据准备使用，不是训练 runtime。

## 具体依据与核对位置

下面这些是关键职责的具体出处；未列出的文件只在文件地图中说明用途，不把
文件名本身当作验证证据。

| 结论 | 源码位置 | 对应测试或原始结果 | 证据级别 |
|---|---|---|---|
| 非空 `history` 是消息来源，tool result 不补成 user，tool-call/result id 配对 | `native_tool_policy.py::_history_messages`、`NativeToolPolicy.act` | `tests/test_native_tool_policy.py::test_tool_result_continuation_does_not_duplicate_current_user_turn`、`test_real_user_event_is_preserved_when_text_matches_tool_result`；提交 `7b52e80` | Executed/Static |
| `request_user_input` 的 typed `input_type` 转成 `AgentAction.requested_input_type` | `native_tool_policy.py::_to_action`、`harness.py::_requested_input_type` | `test_native_control_tool_preserves_declared_input_type`；`test_control_tools_map_to_internal_actions` | Executed/Static |
| Harness 保存事件、推进 simulator、issue/record confirmation 并评分 | `harness.py::HarnessRunner.run`、`grade`、`TrajectoryStore` | `tests/test_harness_tools.py` 中 `test_return_v2_requires_confirmation_for_a_successful_write`、`test_plain_text_confirmation_request_is_classified_without_auto_correction`、`test_rule_policy_gets_verification_and_confirmation_from_user_simulator` | Static/Executed |
| 工具边界校验 schema、identity、confirmation、dispatch 并记录 `ToolCall` | `tools.py::RetailTools.call`、`_identity_guard`、`_require_trusted_confirmation` | `tests/test_tool_schema.py` 的 schema/signature/identity guard tests；`tests/test_confirmation.py::test_write_confirmed_flag_without_trusted_record_is_blocked`、`test_direct_dispatch_rejects_string_confirmation_before_authorized_write` | Static/Executed |
| confirmation 绑定 session/user/operation/参数哈希，拒绝后不可 replay | `confirmation.py::ConfirmationLedger.issue/respond/authorization_for/validate_authorization` | `tests/test_confirmation.py::test_confirmation_is_bound_to_session_user_operation_and_parameters`、`test_refusal_revokes_pending_confirmation_and_replay_is_idempotent` | Static/Executed |
| Direct 与 MCP 汇合到同一工具 surface，MCP 不能覆盖 server identity | `mcp_server.py::MCPRetailFacade`、`build_server` | `tests/test_mcp_server.py::test_mcp_write_still_requires_confirmation`、`test_mcp_confirmation_callback_cannot_override_server_identity`、`test_mcp_surface_matches_retail_tools_registry` | Static/Executed |
| return v2 需要 policy + eligibility，但不强制重复 `get_order` | `harness.py::_return_closure_v2_contract`、`_return_closure_facts_pass`、`grade` | `test_return_v2_does_not_require_redundant_get_order`、`test_return_v2_requires_policy_and_eligibility_facts` | Static/Executed |
| retrieval index 与运行时 chunks/manifest 对齐 | `retrieval_index.py::build_index`、`hybrid_retriever.py::HybridRetriever.__init__` | `tests/test_retrieval_index.py::test_build_index_loads_and_serves_retrieval_and_tool`、`test_stale_bm25_cache_is_rebuilt_to_match_current_chunks` | Static/Executed |
| evidence 只来自工具结果，citation 缺失与 hard contradiction 分开 | `evidence.py::convert_tool_call_to_evidence`、`verify_answer` | `tests/test_evidence_grounding.py::test_failed_tool_produces_no_evidence`、`test_missing_citation_and_coverage_are_diagnostic_only`、`test_wrong_structured_price_is_a_hard_contradiction` | Static/Executed |
| return 实验数字的 policy/execution class 可追溯 | `docs/experiments/return_closure_deterministic_smoke_v1.json`、`return_closure_exploration_deterministic_v1.json`、`return_closure_validation_deterministic_v1.json`、`return_closure_trial_status_v1.json` | 各 JSON 的 `status`、`policy`、`execution_class`、`summary` 字段；RulePolicy 结果不能当模型结果 | Historical，逐项数字未在本轮重跑 |
| 历史 120-task/360-trajectory 汇总 | `docs/harness_v2_llm_360_regraded_v2.json`、`docs/evaluation.md` | JSON 的 `by_split`/`regraded` 字段；原始轨迹不在本 checkout | Historical，非当前 Native 修复证据 |

因此，“文件被保留”与“文件中的每个数字已重新验证”是两件事；实验文件的
数字只在上述原始 JSON 和其记录的执行类别内成立。

本轮新增补证状态和任务入口已用本机 CPU 替身执行；真实模型对查询选择、回答事实
覆盖、来源支持和泛化收益仍未验证。历史 120-task/360-trajectory 的原始轨迹不在
当前 checkout，相关汇总只能按现有 JSON 和 `docs/evaluation.md` 标为历史资料。

## 文件地图

### 根目录与环境文件

| 路径 | 用途与处理 |
|---|---|
| `README.md` | Short public overview and links to this map, status, reproduction and safety; retained. |
| `AGENTS.md` | New-agent navigation and commands; retained and kept short. |
| `CLAUDE.md` | Compatibility handoff that points to `AGENTS.md`; retained to avoid a second instruction set. |
| `.env.example` | Non-secret examples for local/native Qwen, retrieval and optional Tau3; retained. |
| `.gitignore` | Excludes credentials, databases, logs, caches, virtualenvs, generated indexes and model outputs; retained. |
| `.gitattributes` | Git text/line-ending policy; retained. |
| `Dockerfile` | Minimal core dependency/container import smoke; it does not install retrieval/LLM extras or start Qwen; retained, not executed locally because Docker socket access is unavailable. |
| `pytest.ini` | Test discovery and generated-directory exclusions; retained. |
| `requirements.txt` | Core runtime (`numpy`, MCP contract); retained. |
| `requirements-dev.txt` | Core plus pytest; retained for CPU tests. |
| `requirements-retrieval.txt` | Optional SentenceTransformers/jieba/FAISS layer; retained for indexed retrieval only. |
| `requirements-llm.txt` | Optional legacy Transformers/Accelerate layer; native Qwen client uses the endpoint and stdlib HTTP. |
| `requirements-data.txt` | Optional `datasets` layer for Amazon preparation; retained only for data generation. |
| `LICENSE` | Repository license; retained. |

### Runtime 包

| 路径 | 作用与依据 |
|---|---|
| `ecommerce_rag/__init__.py` | Package/version marker; imported by runtime modules. |
| `ecommerce_rag/domain.py` | Public dataclasses listed above; used across harness, policies and tests. |
| `ecommerce_rag/harness.py` | CLI, policies, simulator, runner, scorer and trajectory store; main CPU/native entry. |
| `ecommerce_rag/native_tool_policy.py` | Current native Qwen adapter, Skill injection, message provenance and action parser; targeted tests cover the fix. |
| `ecommerce_rag/agent_runtime.py` | Shared provider prompt/history/validation/retry contract; `test_agent_runtime.py`. |
| `ecommerce_rag/llm_policy.py` | Legacy JSON policy adapter; compatibility tests and old traces only. |
| `ecommerce_rag/llm.py` | Small legacy completion client; used with the optional LLM dependency path. |
| `ecommerce_rag/tools.py` | Trusted read/write dispatch and SQLite business guards; tool, confirmation, schema, MCP and transaction tests. |
| `ecommerce_rag/tool_schema.py` | Single tool schema source and validator; schema tests compare it with method signatures. |
| `ecommerce_rag/confirmation.py` | Trusted confirmation ledger and explicit response parser; confirmation tests. |
| `ecommerce_rag/orders.py` | SQLite setup/seed/read/snapshot; used by tools and harness fixtures. |
| `ecommerce_rag/evidence.py` | Tool-result evidence conversion and answer verification; evidence tests. |
| `ecommerce_rag/freshness.py` | Date/freshness claim guard; freshness tests. |
| `ecommerce_rag/retrieval_index.py` | Source-to-index builder and fingerprints; retrieval tests. |
| `ecommerce_rag/hybrid_retriever.py` | Dense/BM25/RRF retrieval and optional reranker; retrieval tests. |
| `ecommerce_rag/catalog.py` | Product grouping and recommendation/comparison rendering; retrieval/category tests. |
| `ecommerce_rag/config.py` | Env-controlled paths and retrieval/LLM defaults; imported by tools/indexes. |
| `ecommerce_rag/skill_loader.py` | Parses Skill metadata/content and computes content hash; used by Native. |
| `ecommerce_rag/context_compaction.py` | Provider-history compaction and stats; compaction/native tests. |
| `ecommerce_rag/research_state.py` | Derived constraints/facts/gaps/query history/budget projection and fail-closed answer for multi-round read-only evidence gathering. |
| `ecommerce_rag/research_policy.py` | Deterministic CPU substitute that follows the research-state gaps; not a model baseline. |
| `ecommerce_rag/research_fixture.py` | Checked-in lexical retriever substitute for CPU wiring only; production retrieval remains hybrid/indexed. |
| `ecommerce_rag/mcp_server.py` | Optional MCP server and façade over `RetailTools`; MCP tests. |
| `ecommerce_rag/retail_protocol.py` | External/Tau3 write surface constants; write-tool/schema tests. |
| `ecommerce_rag/legacy_closure.py` | Optional progress state for legacy return workflows; legacy progress tests. |
| `ecommerce_rag/legacy_closure_benchmark.py` | Legacy benchmark/protocol utilities; benchmark tests. |
| `ecommerce_rag/action_constraint.py` | Optional dynamic action contract; constraint tests. |
| `ecommerce_rag/phase1_write_gate.py` | Synthetic write-gate probes/scoring; phase1 tests. |
| `ecommerce_rag/process_audit.py` | External process-order audit; process audit tests. |
| `ecommerce_rag/tau3_agent_adapter.py` | Tau3 provider adapter around `AgentRuntime`; Tau3 runner/tests. |
| `ecommerce_rag/tau3_retail_v1.py` | External Tau2 command/config/result verification; Tau3 tests. |
| `ecommerce_rag/verified_sft.py` | Audited trajectory normalization/split builder; SFT tests and script. |
| `ecommerce_rag/diagnostics/__init__.py` | Diagnostics package marker; retained. |
| `ecommerce_rag/diagnostics/transaction_audit.py` | Model-free transaction contract/differential audit; audit script/tests. |

### 测试

每个测试文件都是对应模块的 executable contract。本轮本机完整回归为
299 passed；保留的测试是：

`test_agent_runtime.py` (provider runtime), `test_native_tool_policy.py`
(native parsing/provenance), `test_llm_policy.py` and
`test_llm_trace_end_to_end.py` (legacy JSON path), `test_harness_tools.py`
(harness/tool loop), `test_user_simulator.py` (typed request recognition),
`test_confirmation.py` (trusted authorization), `test_retail_write_tools.py`
(write guards), `test_tool_schema.py` (schema/signature drift),
`test_mcp_server.py` (MCP convergence), `test_policy_tools.py` (policy
retrieval), `test_retrieval_index.py` and `test_catalog_category_filter.py`
(retrieval), `test_evidence_grounding.py` and `test_freshness.py` (answer
evidence), `test_context_compaction.py` (provider history),
`test_action_constraint.py`, `test_legacy_task_progress.py` and
`test_legacy_closure_benchmark.py` (opt-in legacy workflow),
`test_phase1_write_gate.py` (synthetic gate), `test_process_audit.py` and
`test_transaction_audit.py` (audits), `test_tau3_retail_v1.py` and
`test_tau3_retail_nscc_job.py` (external job contracts), `test_verified_sft.py`
(dataset conversion), and `test_skill_compare.py` (candidate decision rules).
本轮新增 `test_research_state.py`，覆盖状态推导、Native A/B 投影、连续检索、任务
隔离、预算停止和 hidden evaluation contract 不泄漏。

### Scripts 与集群 wrapper

| 路径/分组 | 用途与状态 |
|---|---|
| `scripts/README.md` | Script routing; retained. |
| `scripts/build_retrieval_index.py` | CLI wrapper for the retrieval index builder; optional retrieval. |
| `scripts/build_amazon_5k.py`, `prepare_amazon.py`, `slice_products.py` | Download/normalize/slice external Amazon data; generated large data stays outside Git, with only stats tracked. |
| `scripts/generate_retrieval_eval.py`, `scripts/generate_retrieval_eval_v2.py`, `scripts/generate_retrieval_eval_v3.py` | Generate retrieval evaluation JSONL sets; records retain programmatic/locked labels. |
| `scripts/generate_harness_tasks.py` | Generate seeded harness task JSONL; locked task path is registered but contents were not read here. |
| `scripts/freeze_data_source_manifest.py` | Record source revisions/hashes for data provenance. |
| `scripts/audit_transaction_contracts.py`, `audit_tau3_process.py`, `export_trajectory_audit.py` | Run/export model-free or external-trace audits; outputs stay outside checkout unless frozen. |
| `scripts/measure_context_compaction.py`, `diagnose_llm_trace.py` | Offline trace/context diagnostics; not default CPU smoke. |
| `scripts/run_skill_trial.py`, `compare_skill_trials.py`, `propose_skill_patch.py`, `freeze_return_closure.py` | Historical Skill candidate generation/validation/comparison/freeze tooling; deterministic arms are not model evidence. |
| `scripts/run_research_trial.py` | Paired A/B runner for complex-research exploration/validation; records execution class, spans, tool/token/latency and evidence-support diagnostics. |
| `scripts/run_phase1_write_gate.py` | Runs synthetic write-gate probes. |
| `scripts/build_verified_ecommerce_sft.py`, `validate_verified_sft.py` | Optional audited SFT-data build/validation; no training is run here. |
| `scripts/run_tau3_retail_v1.py`, `_tau3_cli_with_frozen_judge.py` | External Tau3/Tau2 evaluation wrappers; not local CPU path. |
| `scripts/__init__.py` | Script package marker. |
| `nscc/README.md` | Cluster job assumptions and guardrails. |
| `nscc/download_models.py` | Cluster-side model asset preparation. |
| `nscc/serve_tau3_agent_v1.pbs` | Cluster Tau3 model-serving job. |
| `nscc/run_tau3_retail_base_v1.pbs` | Cluster Tau3 base evaluation job. |

### 数据、Skill 与实验产物

| 路径/分组 | 来源、使用方与依据 |
|---|---|
| `ecommerce_rag/data/sample_products.jsonl` | Small checked-in product corpus for CPU/retrieval examples; config/index input. |
| `ecommerce_rag/data/policies.jsonl` | Checked-in policy source; direct `get_policy` fallback and retrieval input. |
| `ecommerce_rag/data/amazon_products_5k.stats.json` | Provenance/count summary for generated Amazon data; large source/output is not tracked. |
| `ecommerce_rag/data/harness_contract_smoke.jsonl` | Four-task deterministic contract smoke; executed with RulePolicy. |
| `ecommerce_rag/data/harness_smoke.jsonl` + `harness_smoke_manifest.json` | Eight-task development smoke and scenario manifest; no model claim. |
| `ecommerce_rag/data/harness_tasks_v2.jsonl` | Larger dev/locked harness source; path registered only in this pass, locked contents not read. |
| `ecommerce_rag/data/return_closure_smoke.jsonl` | Four-task return wiring smoke; deterministic/offline. |
| `ecommerce_rag/data/return_closure_tasks.jsonl` | 24-task exploration/validation/locked return source; path registered only, contents not read. |
| `ecommerce_rag/data/complex_research_exploration.jsonl` | Six locally auditable complex consultations over checked-in products, policies and one read-only seeded order; exploratory engineering set. |
| `ecommerce_rag/data/complex_research_validation.jsonl` | Two separate holdout-style validation consultations; not a rephrased copy of exploration and not yet a model result. |
| `ecommerce_rag/data/retrieval_eval_250.jsonl`, `ecommerce_rag/data/retrieval_eval_v2_300.jsonl`, `ecommerce_rag/data/retrieval_eval_v3_150.jsonl` | Programmatic retrieval evaluation sets; v3 is a difficult/locked-labelled holdout, not a human semantic review. |
| `skills/return_request/SKILL.md` | Active Skill v0 workflow guidance; loaded only when requested by Native. |
| `docs/experiments/*.json` | Frozen experiment metadata/results retained with original commit, policy and execution class. Deterministic return arms are RulePolicy/offline; native-not-executed is explicit; candidate v1 was historically rejected. |
| `docs/harness_v2_llm_360_regraded_v2.json` and `docs/evaluation.md` | Historical external grading summary; not current Native message-fix evidence. |
| `docs/data_source_manifest.json` and `docs/verified_ecommerce_agent_learning_v2_sources.json` | Frozen external source/revision ledgers. |

### Documentation files and frozen JSON, individually

| Path | Use and disposition |
|---|---|
| `docs/architecture.md` | This canonical call-chain/interface/file map; retained. |
| `docs/current_status.md` | Short current code, environment and experiment status; retained as the single status entry. |
| `docs/reproduction.md` | Environment matrix, install/start commands, evidence provenance and known issues; retained as the single reproduction entry. |
| `docs/complex_research.md` | This round's task data audit, A/B contract, trace fields, reference mechanisms and next AutoDL handoff. |
| `docs/tools_and_safety.md` | Tool surface, trusted confirmation and fail-closed interpretation; retained. |
| `docs/transaction_contracts.md` | How to run the model-free transaction audit; retained. |
| `docs/retrieval.md` | Index build command and retrieval result boundaries; retained. |
| `docs/retrieval_scale_summary.md` | Historical scale/ablation measurements and their limitations; retained with historical labels. |
| `docs/evaluation.md` | Historical 120-task/360-trajectory evaluation summary; retained but not current Native evidence. |
| `docs/data_source_manifest.json` | Frozen source/model/external revision ledger; retained. |
| `docs/verified_ecommerce_agent_learning_v2_sources.json` | Detailed source/verification records for historical learning evaluation; retained. |
| `docs/harness_v2_llm_360_regraded_v2.json` | Machine-readable historical regraded aggregate; retained unchanged. |
| `docs/experiments/return_closure_candidate_v1.json` | Candidate provenance/status record; retained, candidate was rejected. |
| `docs/experiments/return_closure_deterministic_smoke_v1.json` | Four-task RulePolicy smoke report; retained as offline wiring evidence. |
| `docs/experiments/return_closure_exploration_deterministic_v1.json` | Deterministic A/B exploration report; retained, not model evidence. |
| `docs/experiments/return_closure_freeze_v1.json` | Historical v1 freeze metadata; retained unchanged. |
| `docs/experiments/return_closure_freeze_v2.json` | Frozen v2 scoring metadata; retained unchanged. |
| `docs/experiments/return_closure_locked_deterministic_v1.json` | Historical deterministic locked-set report; retained with its original policy attribution. |
| `docs/experiments/return_closure_native_not_executed_v1.json` | Explicit missing-service record; retained, not a model result. |
| `docs/experiments/return_closure_trial_status_v1.json` | Historical trial status/attribution summary; retained. |
| `docs/experiments/return_closure_validation_deterministic_v1.json` | Deterministic validation report; retained, not Skill-effect evidence. |
| `docs/experiments/tau3_g0e_context_compaction_offline.json` | Offline counterfactual compaction measurement metadata; retained with its no-model caveat. |

## 本地图不能证明的内容

消息来源修复已做静态核对和窄 CPU 测试，但对真实 Qwen action selection 的影响
仍是 Pending；Skill v0 的“普通文本”诊断也没有在本轮修复。AutoDL 尚未同步，
没有 vLLM/model 执行证据。当前 checkout 没有 GitHub Actions workflow，因此不
声称有 CI 结果。deterministic Rule/Oracle 报告只能证明 wiring、状态传递和 guard
contract，不能证明 Skill 有效、模型质量或泛化收益。
