# -*- coding: utf-8 -*-
"""Configuration for the standalone e-commerce Agentic RAG project."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DATA_DIR = ROOT / "data"
INDEX_DIR = ROOT / "index"
LOG_DIR = PROJECT_ROOT / "logs"

PRODUCT_DATA_PATH = Path(os.environ.get("ERAG_PRODUCTS", DATA_DIR / "sample_products.jsonl"))
POLICY_DATA_PATH = Path(os.environ.get("ERAG_POLICIES", DATA_DIR / "policies.jsonl"))

# Freshness diagnostic: e-commerce point-in-time. When an answer asserts
# price/inventory/policy, compare the backing item's `updated_at` with this age.
FRESHNESS_MAX_AGE_DAYS = int(os.environ.get("ERAG_FRESHNESS_MAX_AGE_DAYS", "30"))

EMBED_MODEL = os.environ.get(
    "ERAG_EMBED_MODEL",
    os.environ.get("ARAG_EMBED_MODEL", "paraphrase-multilingual-MiniLM-L12-v2"),
)

TOP_K = int(os.environ.get("ERAG_TOP_K", "5"))
DENSE_K = int(os.environ.get("ERAG_DENSE_K", "20"))
BM25_K = int(os.environ.get("ERAG_BM25_K", "20"))
RRF_K = int(os.environ.get("ERAG_RRF_K", "60"))
DENSE_SCORE_WEIGHT = float(os.environ.get("ERAG_DENSE_SCORE_WEIGHT", "0.03"))

# 可选 cross-encoder 重排：用开关控制，方便做 before/after 对照评测。
# 关闭时走纯 hybrid(dense+BM25+RRF)；开启时对融合后的前 RERANK_CANDIDATES 个候选精排再取 TOP_K。
USE_RERANKER = os.environ.get("ERAG_USE_RERANKER", "0") == "1"
RERANKER_MODEL = os.environ.get("ERAG_RERANKER_MODEL", "BAAI/bge-reranker-base")
RERANK_CANDIDATES = int(os.environ.get("ERAG_RERANK_CANDIDATES", "20"))
# 重排打分用的文本粒度：parent=完整商品卡（保留上下文），chunk=召回的子片段。
# 父子结构下子片段会丢上下文（如评价里的关键词），默认用父卡。
RERANK_ON = os.environ.get("ERAG_RERANK_ON", "parent")
# 重排前是否把候选去重到“每个商品一个”。父卡重排下必须开，否则同商品多 chunk 霸占 top_k。
RERANK_DEDUP = os.environ.get("ERAG_RERANK_DEDUP", "1") == "1"

# RRF 分数只保留名次、丢弃了相似度绝对值，不适合做“证据是否充分”的置信判断。
# 改用 dense 余弦相似度（embedding 已归一化）作为独立置信信号来决定是否转人工。
RETRIEVAL_MIN_DENSE_SIM = float(os.environ.get("ERAG_RETRIEVAL_MIN_DENSE_SIM", "0.35"))
# Post-retrieval price constraint filter: parse budget from query, remove products
# where price > budget. Disabled by default so it can be toggled for ablation.
PRICE_FILTER_ENABLED = os.environ.get("ERAG_PRICE_FILTER", "1") == "1"
# Compound query decomposition for compare intent: split "A 和 B" into two sub-queries,
# retrieve each separately, merge at doc level via RRF, guarantee dual coverage.
COMPOUND_DECOMP_ENABLED = os.environ.get("ERAG_COMPOUND_DECOMP", "1") == "1"
MAX_RETRIEVAL_ROUNDS = int(os.environ.get("ERAG_MAX_ROUNDS", "2"))
GROUNDING_SENT_THRESHOLD = float(os.environ.get("ERAG_GROUNDING_SENT_THRESHOLD", "0.42"))
GROUNDING_MIN_RATIO = float(os.environ.get("ERAG_GROUNDING_MIN_RATIO", "0.5"))
