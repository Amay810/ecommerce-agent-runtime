# 复现与环境身份

## 当前清理候选

当前清理候选为 `8ae1c0e4ad6ec62eb73a5f2b72a6a41910c389db`，本机 CPU 完整回归为
`262 passed`。清理前基线 `67566944ad79c03feaf42a9ca72a5d20571cad06` 的
`313 passed` 仍作为对照；减少的 51 个测试属于已删除的旧 LLM 专属路径。本轮不运行
Qwen、GPU、24-task 或 360-task 历史模型实验。

每条执行证据都应同时记录代码 revision、worktree、解释器、任务/配置、命令和结果。
`Executed` 是确实执行过的命令，`Historical` 是复用旧记录，`Static` 是源码/配置核对，
`Pending` 是有意未执行；不可定位的外部产物使用 `Not located`，不补造 hash。

## 当前环境

| 环境 | 用途 | 当前状态 |
|---|---|---|
| 本机 `.venv` | 文档静态检查、CPU smoke、pytest、确定性 harness | 可用；已执行当前候选的 CPU 回归 |
| AutoDL `/root/autodl-tmp` | 本地 Qwen/vLLM 和固定模型实验 | 当前无可见 GPU；本轮不启动服务 |
| Docker | 可选 core import smoke | 当前用户无 Docker socket 验证权限 |
| GitHub Actions | CI | 当前 checkout 没有 `.github/workflows`，无 CI 证据 |

AutoDL 解释器和服务脚本的约定仍是：`/root/autodl-tmp/venvs/qwen-vllm/bin/python`、
`/root/autodl-tmp/venvs/qwen-vllm/bin/vllm` 和
`/root/autodl-tmp/config/start-qwen-vllm.sh`。本机 Codex 环境不等于 AutoDL。

## 本机安装与 CPU smoke

本机 `/usr/bin/python3` 缺少 `ensurepip`，优先使用已验证的 uv 路径：

```bash
uv venv .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
UV_CACHE_DIR=/tmp/ecommerce-uv-cache uv pip check --python .venv/bin/python
```

代表性的确定性 smoke：

```bash
mkdir -p logs
.venv/bin/python -m ecommerce_rag.harness run \
  --tasks ecommerce_rag/data/harness_contract_smoke.jsonl \
  --db logs/contract.db --store logs/contract_trajectories.sqlite \
  --output logs/contract_report.json --policy rule --repeats 1 --seed-db
```

该命令验证工具 dispatch、SQLite 状态、confirmation 和评分 wiring；不证明 Qwen、
Skill 效果或泛化。需要完整 CPU 回归时使用：

```bash
.venv/bin/python -m pytest -q
```

当前候选已执行 `.venv/bin/python -m pytest -q`，结果为 `262 passed`；`67566944` 的
`313 passed` 是清理前对照，不与当前结果相加。

按需安装额外能力：`requirements-retrieval.txt` 提供 SentenceTransformers/jieba/FAISS，
`requirements-data.txt` 提供 Amazon 数据准备。当前运行器不再提供旧 JSON/本地
Transformers policy；历史 SQLite/JSON 仍可通过 `TrajectoryStore` 和 `harness replay`
只读查看。索引、模型缓存、SQLite 和日志不提交。

## Native Qwen 路径（AutoDL）

服务启动后，使用 AutoDL 解释器和本地模型；不要用本机 `.venv` 代替：

```bash
bash /root/autodl-tmp/config/start-qwen-vllm.sh
cd /root/autodl-tmp/src/ecommerce-agent-runtime
export ARAG_LLM_BASE_URL=http://127.0.0.1:8123/v1
export ARAG_LLM_MODEL=Qwen3-4B-Instruct-2507
export ARAG_LLM_API_KEY=local-vllm
/root/autodl-tmp/venvs/qwen-vllm/bin/python -m ecommerce_rag.harness run \
  --tasks ecommerce_rag/data/return_closure_smoke.jsonl \
  --db /tmp/native_smoke.db --store /tmp/native_smoke.sqlite \
  --output /tmp/native_smoke.json --policy native --repeats 1 --seed-db
```

这是基础 Native smoke；Skill、ResearchState、observation view、tool visibility 和
answer contract 都必须显式开启。服务缺失或无 GPU 记为 `not_executed`，不能填成模型
分数。固定实验的完整配置、原始 report/SQLite 路径和结论见
[实验索引](experiments/README.md)，本轮不复制长实验命令。

## 可选本地路径

- 检索 index：安装 `requirements-retrieval.txt` 后运行
  `.venv/bin/python -m scripts.build_retrieval_index --output-dir ecommerce_rag/index`；
  配对实验必须固定 manifest、embedding hash 和 backend。
- transaction audit：使用 `docs/transaction_contracts.md` 的 model-free 命令；审计
  不调用 LLM、不下载权重、不启动模型服务。
- research-find-v1：语料、类目旁路、任务生成、索引和三组运行的输入与产物见
  `docs/research_find.md`；无卡 AutoDL 实例（2 GiB/0.5 核）建 5k 索引会被 OOM kill。
- ResearchState/复杂咨询：使用 `docs/complex_research.md` 和
  `docs/experiments/README.md` 的实验记录；不要把其 CPU fixture 当模型结果。

## 评分与历史产物

评分版本、旧记录读取规则和 `303/360` 的近期复测 provenance 见
[evaluation](evaluation.md)。现有历史 JSON 的 source store 可能只剩路径和 hash；找不到
SQLite 时保留该状态，不用重跑补文档。

不提交凭据、模型权重、索引缓存、SQLite 日志、原始轨迹或 `graphify-out/`。需要恢复
原 dirty 实验时使用本轮保护快照，而不是从当前稳定文档猜测共享代码改动。
