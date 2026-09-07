# Project history

This repository is the runtime half of the e-commerce assistant project.
Retired exploratory code and raw artifacts are kept in a separate archive.

| Stage | Result | Status |
|---|---|---|
| Retrieval baseline | Hybrid dense/BM25 retrieval with reproducible fixtures | retained |
| Agent v2 | Guarded multi-turn tool execution and trajectory recording | retained |
| Return workflow | Protocol and execution-layer checks reached the frozen local contract | retained |
| Transaction audit | Deterministic state-transition and tool-surface audit | retained |
| External Retail wrapper | Pinned environment adapter and split checks | optional, environment-dependent |

Historical model and dataset experiments are not presented as current runtime
capabilities. The checked-in source and `docs/data_source_manifest.json` define
the reproducible boundary for new work.
