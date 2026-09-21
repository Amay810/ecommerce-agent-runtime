"""Dependency-free retrieval substitute for CPU wiring checks only.

Production runs continue to use :class:`HybridRetriever`. This fixture reads
the checked-in product/policy JSONL and applies a deterministic lexical score so
the paired research harness can be exercised without downloading an embedding
model.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from . import config
from .hybrid_retriever import tokenize_zh
from .retrieval_index import build_chunks, load_policies, load_products


class LocalResearchFixtureRetriever:
    """Small lexical retriever; never use as retrieval-quality evidence."""

    def __init__(self, *, product_path: Path = config.PRODUCT_DATA_PATH, policy_path: Path = config.POLICY_DATA_PATH):
        self.chunks, self.parents = build_chunks(load_products(product_path), load_policies(policy_path))
        self.last_timing: dict[str, Any] = {"backend": "local_fixture_lexical"}

    def search(self, query: str, top_k: int = 5, source_type: str | None = None, category: str | None = None):
        query_tokens = Counter(tokenize_zh(query))
        scored = []
        for index, chunk in enumerate(self.chunks):
            if source_type and chunk.get("source_type") != source_type:
                continue
            if category and category.casefold() not in str(chunk.get("category", "")).casefold():
                continue
            text_tokens = Counter(tokenize_zh(f"{chunk.get('title', '')} {chunk.get('text', '')}"))
            overlap = sum(min(count, text_tokens.get(token, 0)) for token, count in query_tokens.items())
            scored.append((overlap, -index, chunk))
        scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
        return [dict(chunk, score=float(score)) for score, _index, chunk in scored[:top_k]]
