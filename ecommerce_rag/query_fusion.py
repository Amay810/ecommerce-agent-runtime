"""Fuse the policy's search query with the latest user message.

A small model often rewrites a detailed request into a short query that drops
brand and attribute words. This retriever wrapper runs the policy query and the
latest user message separately and merges the two parent-document rankings with
reciprocal-rank fusion. The user message is public conversation context, never
hidden task data. When both texts are the same the wrapper is a pass-through.
"""

from __future__ import annotations

import re
from typing import Any

from .hybrid_retriever import reciprocal_rank_fusion


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().casefold()


class ContextFusionRetriever:
    """Wrap a retriever; the harness sets ``context_query`` before each search."""

    def __init__(self, retriever: Any):
        self.retriever = retriever
        self.context_query: str | None = None
        self.last_fusion: dict[str, Any] = {}

    @property
    def chunks(self) -> list[dict[str, Any]]:
        return self.retriever.chunks

    def search(self, query: str, top_k: int = 5, source_type: str | None = None,
               category: str | None = None) -> list[dict[str, Any]]:
        primary = self.retriever.search(query, top_k=top_k, source_type=source_type, category=category)
        context = self.context_query
        if not context or _normalize(context) == _normalize(query):
            self.last_fusion = {"context_fused": False}
            return primary
        secondary = self.retriever.search(context, top_k=top_k, source_type=source_type, category=category)
        representative: dict[str, dict[str, Any]] = {}
        rankings = []
        for results in (primary, secondary):
            ranking = []
            for chunk in results:
                doc_id = chunk.get("doc_id")
                if doc_id and doc_id not in ranking:
                    ranking.append(doc_id)
                    representative.setdefault(doc_id, chunk)
            rankings.append(ranking)
        fused = reciprocal_rank_fusion(rankings)
        primary_rank = {doc_id: i for i, doc_id in enumerate(rankings[0])}
        order = sorted(fused, key=lambda d: (-fused[d], primary_rank.get(d, len(primary_rank))))
        self.last_fusion = {
            "context_fused": True,
            "added_by_context": [d for d in order[:top_k] if d not in primary_rank],
        }
        return [{**representative[d], "fusion_score": fused[d]} for d in order[:top_k]]
