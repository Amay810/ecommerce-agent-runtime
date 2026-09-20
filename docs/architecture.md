# Architecture and file map

This is the canonical handoff map for the checkout. Read it after
`AGENTS.md`; use `docs/current_status.md` for the short state and
`docs/reproduction.md` for environment-specific commands.

## Evidence labels

- **Executed**: a command or test was run and its environment is named.
- **Static**: derived from the checked-in implementation, schemas or imports.
- **Historical**: retained experiment or report with its original revision and
  policy attribution.
- **Pending**: deliberately not run in this cleanup.

The map was reviewed against local commit `ecb3e3d` on 2026-09-20. It covers
all tracked project files listed below. Locked task contents are registered by
path only; they were not opened.

## Main call path

```text
ecommerce_rag.harness CLI
  -> HarnessRunner.run(TaskSpec)
  -> AgentObservation(history, session, public tool schemas)
  -> NativeToolPolicy / RulePolicy / compatibility LLMPolicy
  -> AgentAction
  -> RetailTools.call(name, typed arguments, session/confirmation context)
  -> SQLite, policy corpus or HybridRetriever
  -> tool event in history and evidence ledger
  -> typed user input, final answer, handoff, or max-step stop
  -> TrajectoryStore + grade(TaskSpec, Trajectory)
```

The supported Qwen path is `NativeToolPolicy`. Its provider uses an
OpenAI-compatible chat-completions wire format; the configured model is local
Qwen, not an OpenAI-hosted model. `LLMPolicy` is the older JSON-envelope
compatibility path. Tau3 has a separate adapter and is not part of the local
CPU smoke.

## Interfaces and ownership

### Policy input and action conversion

- `domain.py`: `TaskSpec` contains task and hidden scoring expectations;
  `AgentObservation` is the policy-visible projection; `AgentAction` is the
  policy output; `ToolCall`, `Trajectory` and `GradeResult` are serialisable
  trace/score contracts. Hidden fields must not enter an observation.
- `harness.py`: `HarnessRunner.run` starts the session, appends the initial
  user event, calls `policy.act`, and appends assistant/tool/user events. It
  resolves typed input through `UserSimulator`, records confirmation spans,
  snapshots SQLite state, and calls `grade`. `TrajectoryStore` persists the
  JSON trace and grade in SQLite.
- `native_tool_policy.py`: `_history_messages` converts canonical history to
  provider messages. A non-empty history is the only provenance source;
  assistant tool calls receive a synthetic `call_history_*` id and the next
  `role=tool` event uses the same id. Only an entirely empty history uses
  `current_message` as the initial user event. `_to_action` validates one
  provider tool call, injects the harness-owned `user_id`, preserves
  `request_user_input(input_type=...)`, and emits an `AgentAction`.
- `agent_runtime.py`: `AgentRuntime.prepare_messages` builds the system,
  policy and optional Skill blocks, annotates tool names, and optionally
  compacts a provider-only copy. `validate_generation` and `run_turn` are the
  lower-level shared native/Tau3 protocol contract; Native currently performs
  its own adapter loop for historical compatibility.
- `llm_policy.py`: compatibility parser for the older JSON action envelope;
  it is not the current Qwen tool-call path. `llm.py` is its small completion
  helper.
- `skill_loader.py` and `skills/return_request/SKILL.md`: optional versioned
  prompt guidance. Loading a Skill changes prompt context and metadata only;
  it cannot change schemas, permissions, confirmation, scoring or SQLite.

### Trusted execution and state

- `tool_schema.py`: canonical public JSON Schemas, argument validation and
  compact prompt rendering. `user_id` is declared for trusted internal calls
  but hidden and injected on the Native provider boundary.
- `tools.py`: `RetailTools.call` is the final typed dispatch boundary used by
  Direct and MCP. It validates arguments, applies the identity guard, dispatches
  the registry method, records `ToolCall`, and returns structured errors. Read
  tools cover catalog/policy/order/eligibility; write tools perform conditional
  SQLite updates and idempotent replay checks.
- `confirmation.py`: `ConfirmationLedger` is in-memory per runtime boundary.
  The host issues a request after a concrete typed confirmation action, records
  the real user response, and passes only the matching authorization id to a
  write. Session, user, operation and canonical parameter hash must match.
- `orders.py`: SQLite schema, deterministic seed/reset, authenticated order
  reads and snapshots. `tools.py` owns business guards and mutations; this
  module does not decide model actions.
- `retail_protocol.py`: additional Tau3/compiler write method set and contracts;
  `tools.py` remains their execution boundary.
- `mcp_server.py`: optional MCP façade. It injects server-side user/session
  context and delegates every operation to `RetailTools`; it is not a second
  authorization implementation.
- `evidence.py`: converts successful tool results into evidence records and
  verifies final-answer facts/citations. `freshness.py` classifies stale or
  missing `updated_at` data. These modules diagnose answers; they do not alter
  tool state.

### Retrieval and presentation

- `retrieval_index.py`: reads product/policy JSONL, builds chunks, embeddings,
  parent cards and a manifest consumed by the retriever.
- `hybrid_retriever.py`: validates the manifest, then combines dense
  SentenceTransformers/FAISS-or-NumPy ranking with BM25/RRF and optional
  reranking. Generated index files are ignored and must not be committed.
- `catalog.py`: deterministic grouping and human-readable recommendation or
  comparison briefs used by retrieval-facing code/tests.
- `config.py`: environment-driven data/index/model paths, freshness and
  retrieval parameters. Its legacy `LLM_*` defaults are for compatibility;
  Native Qwen requires an explicit local endpoint as documented in
  `docs/reproduction.md`.
- `context_compaction.py`: provider-only loss-aware history compaction. Stored
  trajectory history is not mutated.

### Harness, compatibility and diagnostics

- `legacy_closure.py`: optional return-workflow progress reducer.
- `action_constraint.py`: optional one-step action remapping/fail-closed
  contract derived from that reducer; not enabled by the default CLI.
- `legacy_closure_benchmark.py`: legacy return workflow benchmark helpers,
  database cloning and protocol gates used by compatibility tests.
- `phase1_write_gate.py`: synthetic write-gate probes, scorer and go/no-go
  aggregation; it is a model-free safety experiment, not the main harness.
- `process_audit.py`: audits external simulation message sequences for read,
  confirmation and write ordering.
- `diagnostics/transaction_audit.py`: model-free guarded-vs-unsafe differential
  replay and contract audit. `UnsafeRetailTools` is explicitly diagnostic.
- `tau3_agent_adapter.py`: adapts Tau3-style `generate_next_message` calls to
  `AgentRuntime`; external Tau3 owns the surrounding environment.
- `tau3_retail_v1.py`: verifies the pinned external Tau2 checkout, builds its
  command and validates returned native configuration/results.
- `verified_sft.py`: converts audited external trajectories into leakage-aware
  structure/process splits for optional dataset preparation; it is not a
  training runtime.

## File map

### Root and environment files

| Path | Purpose and disposition |
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

### Runtime package

| Path | Role and evidence |
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

### Tests

Each test file is an executable contract for the named module. The full local
suite at this baseline passed 291 tests. The retained files are:

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

### Scripts and cluster wrappers

| Path/group | Use and status |
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
| `scripts/run_phase1_write_gate.py` | Runs synthetic write-gate probes. |
| `scripts/build_verified_ecommerce_sft.py`, `validate_verified_sft.py` | Optional audited SFT-data build/validation; no training is run here. |
| `scripts/run_tau3_retail_v1.py`, `_tau3_cli_with_frozen_judge.py` | External Tau3/Tau2 evaluation wrappers; not local CPU path. |
| `scripts/__init__.py` | Script package marker. |
| `nscc/README.md` | Cluster job assumptions and guardrails. |
| `nscc/download_models.py` | Cluster-side model asset preparation. |
| `nscc/serve_tau3_agent_v1.pbs` | Cluster Tau3 model-serving job. |
| `nscc/run_tau3_retail_base_v1.pbs` | Cluster Tau3 base evaluation job. |

### Data, Skill and experiment artifacts

| Path/group | Source, consumer and evidence |
|---|---|
| `ecommerce_rag/data/sample_products.jsonl` | Small checked-in product corpus for CPU/retrieval examples; config/index input. |
| `ecommerce_rag/data/policies.jsonl` | Checked-in policy source; direct `get_policy` fallback and retrieval input. |
| `ecommerce_rag/data/amazon_products_5k.stats.json` | Provenance/count summary for generated Amazon data; large source/output is not tracked. |
| `ecommerce_rag/data/harness_contract_smoke.jsonl` | Four-task deterministic contract smoke; executed with RulePolicy. |
| `ecommerce_rag/data/harness_smoke.jsonl` + `harness_smoke_manifest.json` | Eight-task development smoke and scenario manifest; no model claim. |
| `ecommerce_rag/data/harness_tasks_v2.jsonl` | Larger dev/locked harness source; path registered only in this pass, locked contents not read. |
| `ecommerce_rag/data/return_closure_smoke.jsonl` | Four-task return wiring smoke; deterministic/offline. |
| `ecommerce_rag/data/return_closure_tasks.jsonl` | 24-task exploration/validation/locked return source; path registered only, contents not read. |
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

## Not proved by this map

The message-source fix is covered by static inspection and targeted CPU tests;
its effect on real Qwen action selection is still pending. AutoDL currently has
no visible GPU and no vLLM service. No GitHub Actions workflow is present in
this checkout, so no CI result is claimed. Deterministic Rule/Oracle reports
prove wiring and guard contracts only, not Skill effectiveness or model quality.
