# 复现与环境身份

以下命令默认在仓库根目录执行。本轮实现了本地复杂咨询补证 wiring、任务集和
CPU 对照入口；不下载模型、不启动 GPU、
不调用付费服务。`Executed` 表示已有明确命令和结果；`Historical` 表示复用
旧记录；`Static` 表示代码或配置核对；`Pending` 表示尚未执行。

## 环境职责

| 环境 | 实际身份与解释器 | 适合执行的任务 | 当前状态 |
|---|---|---|---|
| 本机宿主机 | `/home/may/Code/repos/ecommerce-agent-runtime`；Ubuntu 24.04.4、x86_64；`/usr/bin/python3` 3.12.3；`uv` 0.12.10；`.venv/bin/python` 是 uv 管理的 CPython 3.12.3。 | 文档、静态检查、CPU 测试、deterministic harness、可选本机 retrieval。 | `Executed`：本轮使用现有 `.venv`；窄回归与复杂咨询 CPU wiring 已执行；未重建环境。 |
| 本机 Docker | Docker 29.8.0 已安装，但当前用户无 `/var/run/docker.sock` 权限。 | 仅适合可选的 `Dockerfile` core import smoke。 | `Static/unavailable`：本轮未检查或启动容器。 |
| AutoDL CPU | `/root/autodl-tmp/src/ecommerce-agent-runtime`；解释器是 `/root/autodl-tmp/venvs/qwen-vllm/bin/python` 3.12.3。 | CPU 相关检查、仓库同步、无模型的最小验证。 | `Executed`：相关 28 项测试和 import 通过；当前无 GPU、8123 未启动。 |
| AutoDL Qwen | 同一 AutoDL 仓库；服务脚本 `/root/autodl-tmp/config/start-qwen-vllm.sh`；服务解释器 `/root/autodl-tmp/venvs/qwen-vllm/bin/vllm`；本地模型目录由 `runtime.env` 的 `QWEN_MODEL` 指定。 | 有 GPU 后启动 Qwen、native smoke、固定模型实验。 | `Pending`：模型文件存在，但本轮没有启动 GPU 或 vLLM。 |
| GitHub Actions | remote 为 `https://github.com/Amay810/ecommerce-agent-runtime.git`。 | 只有存在 `.github/workflows` 时才承担 CI。 | `Static`：当前 checkout 没有 workflow，因此没有 CI 成功证据。 |

Codex 的临时执行环境不是本机宿主机，也不是 AutoDL。每条验证都要同时记录
路径、解释器、命令、代码版本和结果。

## 本机 CPU 安装

本机推荐使用已经验证过的 uv 路径；本机 `/usr/bin/python3` 缺少 `ensurepip`，
不要把标准 `venv` 作为本机首选：

```bash
cd /home/may/Code/repos/ecommerce-agent-runtime
uv venv .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
UV_CACHE_DIR=/tmp/ecommerce-uv-cache uv pip check --python .venv/bin/python
UV_CACHE_DIR=/tmp/ecommerce-uv-cache uv pip install --dry-run --no-deps \
  --python .venv/bin/python -r requirements-dev.txt
```

这两个 `uv` 检查针对本机已有环境和安装计划，不等价于全新隔离环境安装证明。
当前记录是 `uv pip check` 通过 35 个包，dev requirements dry-run 不需变更。
其他确实提供 `venv` 和 `pip` 的 Python/conda 环境才使用通用方式：

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
```

核心依赖在 `requirements.txt`；只有需要对应能力时才安装：

- `requirements-retrieval.txt`：SentenceTransformers、jieba、FAISS；
- `requirements-llm.txt`：旧 JSON/Transformers 兼容路径；
- `requirements-data.txt`：Amazon 数据准备脚本。

## 本机 CPU 运行与验证

代表性的确定性 smoke 使用本机 `.venv` 和 `RulePolicy`：

```bash
.venv/bin/python -m ecommerce_rag.harness run \
  --tasks ecommerce_rag/data/harness_contract_smoke.jsonl \
  --db /tmp/ecommerce-runtime-cpu-smoke-handoff.db \
  --store /tmp/ecommerce-runtime-cpu-smoke-handoff.sqlite \
  --output /tmp/ecommerce-runtime-cpu-smoke-handoff.json \
  --policy rule --repeats 1 --seed-db
```

`Executed`：4 条 trajectory，task success、policy compliance、terminal-state
accuracy 均为 `1.0`。这是工具 dispatch、状态、confirmation 和评分 wiring 验证，
不是 Qwen 或 Skill 效果证据。

复杂咨询补证的本地对照入口如下。`--fixture` 使用仓库内商品/政策文件生成的
确定性 lexical substitute，只验证工具返回、evidence 投影、连续决策、任务隔离
和预算停止；生产检索仍应使用固定 manifest 的 `HybridRetriever`。

```bash
.venv/bin/python -m pytest -q \
  tests/test_research_state.py tests/test_native_tool_policy.py \
  tests/test_harness_tools.py tests/test_tool_schema.py tests/test_evidence_grounding.py

.venv/bin/python -m scripts.run_research_trial \
  --policy rule --fixture --arm all \
  --tasks ecommerce_rag/data/complex_research_exploration.jsonl \
  --db /tmp/complex_research_rule_cpu.db \
  --store /tmp/complex_research_rule_cpu.sqlite \
  --output /tmp/complex_research_rule_cpu.json \
  --max-steps 8

.venv/bin/python -m scripts.run_research_trial \
  --policy rule --fixture --arm all \
  --tasks ecommerce_rag/data/complex_research_validation.jsonl \
  --db /tmp/complex_research_validation_rule_cpu.db \
  --store /tmp/complex_research_validation_rule_cpu.sqlite \
  --output /tmp/complex_research_validation_rule_cpu.json \
  --max-steps 8
```

本轮窄回归在最终新增边界测试后为 `81 passed`，随后完整回归为
`299 passed in 6.48s`。
exploration 对照实际执行了 A/B 两臂：
A 为当前 `RulePolicy` 检索行为，B 为同一 harness 下按派生 `ResearchState`
继续补证的 CPU 规则替身；报告会记录每任务的 `research_spans`、工具序列、
预算、必要事实覆盖、自动 citation binding、人工支持状态、token 和耗时。
其中 `manual_answer_support` 固定为 `pending`，因为引用编号本身不等于回答受到
事实支持。该结果只证明接线和 fail-closed 规则，不证明 Qwen 会选对查询。

完整回归命令仍保留，但不是每次接手的默认动作：

```bash
.venv/bin/python -m pytest -q
```

`Historical/Executed, 本机`：上一轮在 `ecb3e3d` 执行并记录 `291 passed in
6.95s`；本轮收尾会按共享 runtime diff 重新执行完整回归。`Historical/Executed, AutoDL`
的旧记录是 `291 passed, 2 warnings`，发生在旧 checkout 加上当时两处未提交的
消息来源修改上，也不是文件地图提交 `1379ec9` 的重新回归结果。

## 已记录的窄范围验证

以下是已记录的窄范围验证，均执行于 `bd65a71`；后续提交只改文档，因此继续复用
这些代码验证：

| 环境与代码树 | 命令 | 结果与覆盖 |
|---|---|---|
| 本机，`bd65a71` 代码树 | `.venv/bin/python -m pytest -q tests/test_native_tool_policy.py tests/test_agent_runtime.py tests/test_harness_tools.py` | `28 passed in 0.19s`；覆盖 Native 消息/动作转换、Runtime 合同、Harness/评分/模拟器边界。 |
| 本机，`bd65a71` 代码树 | `.venv/bin/python -c 'from ecommerce_rag.harness import HarnessRunner, RulePolicy; from ecommerce_rag.native_tool_policy import NativeToolPolicy; from ecommerce_rag.tools import RetailTools; print("imports-ok")'` | `imports-ok`；确认主入口、Native policy、工具模块可导入。 |
| AutoDL CPU，`bd65a71` 代码树 | `PYTHONPATH=$PWD CUDA_VISIBLE_DEVICES= /root/autodl-tmp/venvs/qwen-vllm/bin/python -m pytest -q tests/test_native_tool_policy.py tests/test_agent_runtime.py tests/test_harness_tools.py` | `28 passed in 4.69s`；同一窄范围在另一解释器可用，未启动 GPU。 |
| AutoDL CPU，`bd65a71` 代码树 | `/root/autodl-tmp/venvs/qwen-vllm/bin/python -c '...'` | `autodl-imports-ok`；确认 AutoDL 主入口可导入。 |

文件地图提交 `1379ec9` 只补了文档中的完整路径，没有改变运行时代码；因此不重复执行上述
代码验证。当前 HEAD 以 `git rev-parse HEAD` 为准。

## AutoDL Qwen 路径

真实 Qwen 的服务和客户端都在 AutoDL，不使用本机 `.venv`。OpenAI-compatible
只是 HTTP/tool-call 协议名称，模型仍是本地 `Qwen3-4B-Instruct-2507`，不是
OpenAI 托管模型。

启动已有脚本：

```bash
bash /root/autodl-tmp/config/start-qwen-vllm.sh
```

已核对脚本会读取 `/root/autodl-tmp/config/runtime.env`，检查 `QWEN_MODEL` 下的
模型文件，并调用：

```text
/root/autodl-tmp/venvs/qwen-vllm/bin/vllm serve
```

启动服务后，在 AutoDL 的另一个 shell 中运行客户端：

```bash
cd /root/autodl-tmp/src/ecommerce-agent-runtime
export ARAG_LLM_BASE_URL=http://127.0.0.1:8123/v1
export ARAG_LLM_MODEL=Qwen3-4B-Instruct-2507
export ARAG_LLM_API_KEY=local-vllm
/root/autodl-tmp/venvs/qwen-vllm/bin/python -m ecommerce_rag.harness run \
  --tasks ecommerce_rag/data/return_closure_smoke.jsonl \
  --db /tmp/native_smoke.db --store /tmp/native_smoke.sqlite \
  --output /tmp/native_smoke.json --policy native --repeats 1 --seed-db
```

以上是基础 Native smoke，**不加载 Skill**。若要运行 Skill v0，需在同一命令末尾
显式追加 `--skill skills/return_request/SKILL.md`。

查看 Skill trial 脚本参数时（这不是 retrieval 安装或索引构建命令），使用 AutoDL
解释器：

```bash
/root/autodl-tmp/venvs/qwen-vllm/bin/python -m scripts.run_skill_trial --help
```

真实实验前仍需固定模型、工具、任务 seed、simulator、Skill、runtime、decoding
参数和检索 manifest，并按任务重置 DB/session。服务缺失记为 `not_executed`，
不能伪装成模型结果；本轮不运行 validation 或 locked。

## 检索与依赖

本机若要构建 retrieval index，使用 uv 对应的解释器，不使用未确认存在的 pip：

```bash
uv pip install --python .venv/bin/python -r requirements-retrieval.txt
.venv/bin/python -m scripts.build_retrieval_index --output-dir ecommerce_rag/index
```

`HybridRetriever` 在 `__init__` 校验 `retrieval_manifest.json`、embedding shape/hash、
chunks/parents fingerprint；随后使用 FAISS（可用时）或 NumPy dense fallback，加
BM25/RRF。生成的 index、embedding 和模型缓存不进 Git；配对实验必须固定同一
manifest 和 backend。

## 下一次 AutoDL 最小同步与实验命令

本轮没有同步 AutoDL。下一次只需同步本轮范围清楚的本机提交中的运行时代码、任务、
测试、脚本和文档（不带 `/tmp` 报告、不带模型/索引），并在 AutoDL checkout 确认
`git status --short --branch` 后，用已有 qwen-vllm 解释器先跑窄回归。服务可用后，
固定同一 task file、seed、DB reset、Skill、decoding、`--max-steps` 和检索 manifest，
再运行：

```bash
cd /root/autodl-tmp/src/ecommerce-agent-runtime
export ARAG_LLM_BASE_URL=http://127.0.0.1:8123/v1
export ARAG_LLM_MODEL=Qwen3-4B-Instruct-2507
export ARAG_LLM_API_KEY=local-vllm
/root/autodl-tmp/venvs/qwen-vllm/bin/python -m pytest -q \
  tests/test_research_state.py tests/test_native_tool_policy.py \
  tests/test_harness_tools.py tests/test_tool_schema.py tests/test_evidence_grounding.py
/root/autodl-tmp/venvs/qwen-vllm/bin/python -m scripts.run_research_trial \
  --policy native --arm all \
  --tasks ecommerce_rag/data/complex_research_exploration.jsonl \
  --task-id research_exp_01_earbuds_policy \
  --task-id research_exp_05_preorder_shipping \
  --skill skills/return_request/SKILL.md \
  --index /path/to/frozen/retrieval_index \
  --db /tmp/complex_research_native.db \
  --store /tmp/complex_research_native.sqlite \
  --output /tmp/complex_research_native.json \
  --max-steps 8
```

`/path/to/frozen/retrieval_index` 是占位符，必须替换为已核验 manifest 的同一索引；
若服务、索引或依赖缺失，记录为 `not_executed`，不能填成模型分数。该命令固定两条
exploration、两臂共四条轨迹；validation 暂不运行。报告和 trajectory store 保留
完整 request、raw response、工具结果及 research spans，不只保存汇总分数。

## 已知问题与复用边界

- 本机标准 `venv` 失败原因已确认是 `ensurepip` 缺失；不是项目 import 失败。
- 本机 Docker 因 socket 权限不可用；没有容器验证结果。
- AutoDL 当前是无卡、无 vLLM 服务状态；本轮只做 CPU 检查。
- 本轮 AutoDL 尚未同步；真实 Qwen A/B、validation 和 locked 均 Pending。
- 当前 checkout 没有 GitHub Actions workflow，不能把 CI 配置当作执行证据。
- 消息来源修复 `7b52e80` 的真实 Qwen 效果仍 `Pending`；历史 Qwen 轨迹不能标为
  修复后的结果。
- AutoDL `main` 的当前提交已与本机一致。stash `stash@{0}` 只保留了对齐前的
  两文件版本：测试改动与 `7b52e80` 对应；唯一独有行为是旧版在非空 history
  且最新事件为 user 时，可能按 `current_message` 再补一条 user。当前提交明确
  采用“非空 history 唯一来源”，因此该 stash 不在工作树中，也不构成当前代码
  的额外功能。

`docs/experiments/` 下的 JSON 保留原始 commit、task、policy 和 execution class。
其中 Rule/Oracle deterministic 结果只能证明 wiring/guard；不能证明模型质量或
Skill 有效。
