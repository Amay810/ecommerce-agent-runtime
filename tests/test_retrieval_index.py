from __future__ import annotations

import json
import pickle
import sys
import types
from pathlib import Path

import numpy as np

from ecommerce_rag.hybrid_retriever import FastBM25Index, HybridRetriever
from ecommerce_rag.retrieval_index import build_index, build_product_chunks
from ecommerce_rag.tools import RetailTools


class _FakeEncoder:
    def encode(self, texts, **_kwargs):
        rows = []
        for text in texts:
            rows.append([1.0, 0.0] if "Air Pro" in text else [0.0, 1.0])
        return np.asarray(rows, dtype="float32")


def _write_sources(root: Path) -> tuple[Path, Path]:
    products = root / "products.jsonl"
    policies = root / "policies.jsonl"
    products.write_text(
        json.dumps(
            {
                "id": "P001",
                "title": "Air Pro",
                "category": "耳机",
                "price": 499,
                "inventory": "现货",
                "attributes": {"降噪": "主动"},
                "description": "通勤耳机",
                "reviews": [],
                "qa": [],
                "updated_at": "2026-06-12",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    policies.write_text(
        json.dumps(
            {
                "id": "POL001",
                "title": "退货政策",
                "policy_type": "退换货",
                "scope": "商品",
                "content": "七天内可申请",
                "updated_at": "2026-06-01",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return products, policies


def test_product_chunk_contract_matches_retriever_fields():
    chunks, parents = build_product_chunks(
        [{"id": "P001", "title": "Air Pro", "description": "耳机"}]
    )
    assert chunks[0]["chunk_id"] == "product:P001:desc:0"
    assert chunks[0]["doc_id"] == "product:P001"
    assert chunks[0]["source_type"] == "product"
    assert parents["product:P001"]


def test_build_index_loads_and_serves_retrieval_and_tool(tmp_path, monkeypatch):
    products, policies = _write_sources(tmp_path)
    index_dir = tmp_path / "index"
    stats = build_index(index_dir, products, policies, embed_model="fake", encoder=_FakeEncoder())

    assert stats["embedding_shape"] == [3, 2]
    assert all((index_dir / name).exists() for name in (
        "embeddings.npy", "chunks.jsonl", "parents.json", "retrieval_manifest.json"))

    monkeypatch.setitem(sys.modules, "sentence_transformers", types.SimpleNamespace(SentenceTransformer=lambda _name: _FakeEncoder()))
    retriever = HybridRetriever(index_dir, embed_model="fake")
    hits = retriever.search("Air Pro", top_k=1, source_type="product")
    assert hits and hits[0]["product_id"] == "P001"

    result = RetailTools(tmp_path / "unused.sqlite", retriever=retriever).search_catalog("Air Pro")
    assert result["ok"] is True
    assert result["items"] and result["items"][0]["product_id"] == "P001"


def test_stale_bm25_cache_is_rebuilt_to_match_current_chunks(tmp_path, monkeypatch):
    products, policies = _write_sources(tmp_path)
    index_dir = tmp_path / "index"
    build_index(index_dir, products, policies, embed_model="fake", encoder=_FakeEncoder())
    with (index_dir / "bm25_fast_v1.pkl").open("wb") as handle:
        pickle.dump(FastBM25Index([["stale"]]), handle)

    monkeypatch.setitem(sys.modules, "sentence_transformers", types.SimpleNamespace(SentenceTransformer=lambda _name: _FakeEncoder()))
    retriever = HybridRetriever(index_dir, embed_model="fake")

    assert retriever.bm25.n == len(retriever.chunks)
