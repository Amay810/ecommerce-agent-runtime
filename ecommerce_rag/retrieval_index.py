"""Build the on-disk corpus consumed by :class:`HybridRetriever`.

The output schema is intentionally the same small contract used by the
retriever: ``embeddings.npy``, ``chunks.jsonl``, ``parents.json`` and a
provenance manifest.  Product
and policy sources remain separate through ``source_type`` and their metadata,
while parent cards preserve the evidence context returned by ``format_context``.
"""

from __future__ import annotations

import json
import hashlib
import time
from pathlib import Path
from typing import Any, Iterable

from . import config


def _sha256_json(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def chunks_fingerprint(chunks: list[dict[str, Any]]) -> str:
    """Return an order-sensitive fingerprint for the retriever's chunk contract."""
    return _sha256_json(chunks)


def parents_fingerprint(parents: dict[str, str]) -> str:
    return _sha256_json(parents)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def load_products(path: Path = config.PRODUCT_DATA_PATH) -> list[dict[str, Any]]:
    return _read_jsonl(path)


def load_policies(path: Path = config.POLICY_DATA_PATH) -> list[dict[str, Any]]:
    return _read_jsonl(path)


def product_card(product: dict[str, Any]) -> str:
    attributes = "；".join(
        f"{key}: {value}" for key, value in product.get("attributes", {}).items()
    )
    parts = [
        f"商品：{product['title']}（{product.get('category', '')}）",
        f"价格：{product.get('price')} 元" if product.get("price") is not None else "",
        f"库存：{product.get('inventory')}" if product.get("inventory") else "",
        f"规格：{attributes}" if attributes else "",
        f"描述：{product.get('description', '')}" if product.get("description") else "",
    ]
    if product.get("reviews"):
        parts.append("用户评价：" + " ".join(product["reviews"]))
    if product.get("qa"):
        parts.append(
            "常见问答："
            + " ".join(f"问：{item['q']} 答：{item['a']}" for item in product["qa"])
        )
    return "\n".join(part for part in parts if part)


def policy_card(policy: dict[str, Any]) -> str:
    return "\n".join(
        part
        for part in (
            f"政策：{policy['title']}（{policy.get('policy_type', '')}）",
            f"适用范围：{policy.get('scope', '')}" if policy.get("scope") else "",
            f"内容：{policy.get('content', '')}",
        )
        if part
    )


def build_product_chunks(
    products: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    chunks: list[dict[str, Any]] = []
    parents: dict[str, str] = {}
    for product in products:
        product_id = product["id"]
        doc_id = f"product:{product_id}"
        parents[doc_id] = product_card(product)
        base = {
            "doc_id": doc_id,
            "source_type": "product",
            "product_id": product_id,
            "title": product["title"],
            "category": product.get("category", ""),
            "price": product.get("price"),
            "inventory": product.get("inventory", ""),
            "updated_at": product.get("updated_at"),
        }

        def add(chunk_type: str, text: str, index: int) -> None:
            value = text.strip()
            if value:
                chunks.append(
                    {
                        **base,
                        "chunk_id": f"{doc_id}:{chunk_type}:{index}",
                        "chunk_type": chunk_type,
                        "text": value,
                    }
                )

        if product.get("description"):
            add("desc", f"{product['title']}：{product['description']}", 0)
        for index, (key, value) in enumerate(list(product.get("attributes", {}).items())[:8]):
            add("attr", f"{product['title']} specification: {key} {value}", index)
        for index, qa in enumerate(product.get("qa", [])):
            add("qa", f"{product['title']} 问：{qa['q']} 答：{qa['a']}", index)
        for index, review in enumerate(product.get("reviews", [])[:5]):
            text = review.get("text", "") if isinstance(review, dict) else str(review)
            add("review", f"{product['title']} user review: {text}", index)
    return chunks, parents


def build_policy_chunks(
    policies: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    chunks: list[dict[str, Any]] = []
    parents: dict[str, str] = {}
    for policy in policies:
        policy_id = policy["id"]
        doc_id = f"policy:{policy_id}"
        text = policy_card(policy)
        parents[doc_id] = text
        chunks.append(
            {
                "chunk_id": f"{doc_id}:body:0",
                "doc_id": doc_id,
                "source_type": "policy",
                "policy_id": policy_id,
                "title": policy["title"],
                "category": policy.get("policy_type", ""),
                "price": None,
                "inventory": "",
                "chunk_type": "policy",
                "text": text,
                "updated_at": policy.get("updated_at"),
            }
        )
    return chunks, parents


def build_chunks(
    products: Iterable[dict[str, Any]], policies: Iterable[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    product_chunks, product_parents = build_product_chunks(products)
    policy_chunks, policy_parents = build_policy_chunks(policies)
    return product_chunks + policy_chunks, {**product_parents, **policy_parents}


def build_index(
    index_dir: Path,
    product_path: Path = config.PRODUCT_DATA_PATH,
    policy_path: Path = config.POLICY_DATA_PATH,
    embed_model: str = config.EMBED_MODEL,
    batch_size: int | None = None,
    encoder: Any | None = None,
) -> dict[str, Any]:
    """Build and persist the exact files that ``HybridRetriever`` loads."""
    import numpy as np

    started = time.perf_counter()
    products = load_products(product_path)
    policies = load_policies(policy_path)
    chunks, parents = build_chunks(products, policies)
    if not chunks:
        raise ValueError("corpus produced no retrievable chunks")
    index_dir.mkdir(parents=True, exist_ok=True)

    model = encoder
    if model is None:
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer(embed_model)
    encode_kwargs: dict[str, Any] = {
        "normalize_embeddings": True,
        "convert_to_numpy": True,
        "show_progress_bar": True,
    }
    if batch_size is not None:
        encode_kwargs["batch_size"] = batch_size
    embeddings = model.encode([chunk["text"] for chunk in chunks], **encode_kwargs)
    embeddings = np.asarray(embeddings, dtype="float32")
    if embeddings.ndim != 2 or embeddings.shape[0] != len(chunks):
        raise ValueError(
            f"embedding shape {embeddings.shape} does not align with {len(chunks)} chunks"
        )

    np.save(index_dir / "embeddings.npy", embeddings)
    with (index_dir / "chunks.jsonl").open("w", encoding="utf-8") as handle:
        for chunk in chunks:
            handle.write(json.dumps(chunk, ensure_ascii=False) + "\n")
    (index_dir / "parents.json").write_text(
        json.dumps(parents, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (index_dir / "retrieval_manifest.json").write_text(
        json.dumps(
            {
                "manifest_version": "retrieval-index-v1",
                "embed_model": embed_model,
                "chunk_count": len(chunks),
                "embedding_shape": list(embeddings.shape),
                "embedding_sha256": hashlib.sha256(embeddings.tobytes(order="C")).hexdigest(),
                "chunks_sha256": chunks_fingerprint(chunks),
                "parents_sha256": parents_fingerprint(parents),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return {
        "products": len(products),
        "policies": len(policies),
        "chunks": len(chunks),
        "parents": len(parents),
        "embed_model": embed_model,
        "embedding_shape": list(embeddings.shape),
        "manifest": "retrieval_manifest.json",
        "build_time_ms": round((time.perf_counter() - started) * 1000, 2),
        "index_dir": str(index_dir),
    }
