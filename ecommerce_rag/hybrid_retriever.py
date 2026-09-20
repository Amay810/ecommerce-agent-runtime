# -*- coding: utf-8 -*-
"""Hybrid retrieval: dense semantic search + BM25 lexical search + RRF fusion."""

import json
import hashlib
import pickle
import re
import time
import math
import heapq
from collections import Counter, defaultdict
from pathlib import Path

from . import config
from .retrieval_index import chunks_fingerprint, parents_fingerprint


class FastBM25Index:
    """Compact inverted BM25 index; scoring cost depends on matched postings."""

    VERSION = 1

    def __init__(self, tokenized_documents: list[list[str]], k1: float = 1.5, b: float = .75):
        self.k1, self.b, self.n = k1, b, len(tokenized_documents)
        self.doc_len = [len(x) for x in tokenized_documents]
        self.avgdl = sum(self.doc_len) / max(1, self.n)
        frequencies: dict[str, int] = defaultdict(int)
        postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for doc_id, tokens in enumerate(tokenized_documents):
            counts = Counter(tokens)
            for token, tf in counts.items():
                frequencies[token] += 1
                postings[token].append((doc_id, tf))
        self.idf = {token: math.log(1 + (self.n - df + .5) / (df + .5)) for token, df in frequencies.items()}
        self.postings = dict(postings)

    def ranking(self, query_tokens: list[str], k: int) -> list[int]:
        scores: dict[int, float] = defaultdict(float)
        for token in set(query_tokens):
            idf = self.idf.get(token)
            if idf is None: continue
            for doc_id, tf in self.postings[token]:
                norm = tf + self.k1 * (1 - self.b + self.b * self.doc_len[doc_id] / max(self.avgdl, 1e-9))
                scores[doc_id] += idf * tf * (self.k1 + 1) / norm
        return [doc_id for doc_id, _ in heapq.nlargest(k, scores.items(), key=lambda pair: pair[1])]


def tokenize_zh(text: str) -> list[str]:
    try:
        import jieba

        return [t.lower() for t in jieba.lcut(text) if t.strip()]
    except Exception:
        words = re.findall(r"[a-zA-Z0-9]+", text)
        chars = re.findall(r"[\u4e00-\u9fff]", text)
        return [t.lower() for t in words + chars if t.strip()]


def reciprocal_rank_fusion(rankings: list[list[str]], k: int = config.RRF_K) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return scores


class HybridRetriever:
    def __init__(self, index_dir: Path = config.INDEX_DIR, embed_model: str = config.EMBED_MODEL):
        import numpy as np
        from sentence_transformers import SentenceTransformer

        self.index_dir = Path(index_dir)
        self.embeddings = np.load(index_dir / "embeddings.npy", mmap_mode="r")
        with open(index_dir / "chunks.jsonl", encoding="utf-8") as f:
            self.chunks = [json.loads(line) for line in f if line.strip()]
        with open(index_dir / "parents.json", encoding="utf-8") as f:
            self.parents = json.load(f)
        manifest_path = index_dir / "retrieval_manifest.json"
        if not manifest_path.exists():
            raise ValueError(
                f"retrieval index manifest is missing: {manifest_path}; rebuild the index"
            )
        with manifest_path.open(encoding="utf-8") as f:
            manifest = json.load(f)
        matrix = np.asarray(self.embeddings, dtype="float32")
        expected = {
            "manifest_version": "retrieval-index-v1",
            "embed_model": embed_model,
            "chunk_count": len(self.chunks),
            "embedding_shape": list(matrix.shape),
            "embedding_sha256": hashlib.sha256(matrix.tobytes(order="C")).hexdigest(),
            "chunks_sha256": chunks_fingerprint(self.chunks),
            "parents_sha256": parents_fingerprint(self.parents),
        }
        mismatches = {
            key: {"expected": value, "actual": manifest.get(key)}
            for key, value in expected.items()
            if manifest.get(key) != value
        }
        if mismatches:
            raise ValueError(f"retrieval index manifest mismatch: {mismatches}")
        if matrix.ndim != 2 or matrix.shape[0] != len(self.chunks) or not self.chunks:
            raise ValueError(
                f"retrieval index shape does not align with chunks: embeddings={matrix.shape}, "
                f"chunks={len(self.chunks)}"
            )
        self.by_id = {c["chunk_id"]: c for c in self.chunks}
        self.by_doc: dict[str, list[dict]] = defaultdict(list)
        for chunk in self.chunks:
            self.by_doc[chunk["doc_id"]].append(chunk)
        bm25_path = index_dir / "bm25_fast_v1.pkl"
        tokenized = [tokenize_zh(c["text"]) for c in self.chunks]
        if bm25_path.exists():
            try:
                with open(bm25_path, "rb") as f:
                    loaded_bm25 = pickle.load(f)
            except (AttributeError, EOFError, IndexError, OSError, pickle.PickleError, TypeError, ValueError):
                loaded_bm25 = None
            compatible = (
                isinstance(loaded_bm25, FastBM25Index)
                and loaded_bm25.n == len(self.chunks)
                and len(loaded_bm25.doc_len) == len(self.chunks)
                and all(
                    0 <= doc_id < len(self.chunks)
                    for postings in loaded_bm25.postings.values()
                    for doc_id, _tf in postings
                )
            ) if loaded_bm25 is not None else False
            self.bm25 = loaded_bm25 if compatible else FastBM25Index(tokenized)
        else:
            self.bm25 = FastBM25Index(tokenized)
        if not bm25_path.exists() or not compatible:
            try:
                with open(bm25_path, "wb") as f:
                    pickle.dump(self.bm25, f, protocol=pickle.HIGHEST_PROTOCOL)
            except OSError:
                pass
        self.model = SentenceTransformer(embed_model)
        self.faiss_index = None
        self.dense_backend = "numpy_exact"
        try:
            import faiss

            faiss_path = index_dir / "dense_flatip.faiss"
            if faiss_path.exists():
                self.faiss_index = faiss.read_index(str(faiss_path))
                faiss_matches = (
                    getattr(self.faiss_index, "d", None) == matrix.shape[1]
                    and getattr(self.faiss_index, "ntotal", None) == matrix.shape[0]
                )
                reconstruct_n = getattr(self.faiss_index, "reconstruct_n", None)
                if faiss_matches and callable(reconstruct_n):
                    try:
                        indexed = np.asarray(
                            reconstruct_n(0, matrix.shape[0]), dtype="float32"
                        )
                        faiss_matches = indexed.shape == matrix.shape and np.allclose(
                            indexed, matrix, rtol=1e-5, atol=1e-6
                        )
                    except (AttributeError, RuntimeError, TypeError, ValueError):
                        faiss_matches = False
                if not faiss_matches:
                    self.faiss_index = None
            if self.faiss_index is None:
                self.faiss_index = faiss.IndexFlatIP(matrix.shape[1])
                self.faiss_index.add(matrix)
                faiss.write_index(self.faiss_index, str(faiss_path))
            self.dense_backend = "faiss_indexflatip"
        except (ImportError, OSError, RuntimeError):
            self.faiss_index = None
        self.last_timing: dict[str, float] = {}
        # cross-encoder 重排器仅在开启时加载，保持纯 hybrid 路径轻量
        self.reranker = None
        if config.USE_RERANKER:
            from sentence_transformers import CrossEncoder

            self.reranker = CrossEncoder(config.RERANKER_MODEL)

    def _dense_scores(self, query: str):
        import numpy as np

        q = self.model.encode([query], normalize_embeddings=True, convert_to_numpy=True)[0]
        return self.embeddings @ q  # 已归一化，等于余弦相似度，与 self.chunks 对齐

    def _dense_ranking(self, query: str, k: int) -> list[str]:
        import numpy as np
        if self.faiss_index is not None:
            q = self.model.encode([query], normalize_embeddings=True, convert_to_numpy=True).astype("float32")
            _, indexes = self.faiss_index.search(q, k)
            order = indexes[0]
        else:
            order = np.argsort(-self._dense_scores(query))[:k]
        return [self.chunks[int(i)]["chunk_id"] for i in order]

    def _bm25_ranking(self, query: str, k: int) -> list[str]:
        order = self.bm25.ranking(tokenize_zh(query), k)
        return [self.chunks[int(i)]["chunk_id"] for i in order]

    def search(
        self,
        query: str,
        top_k: int = config.TOP_K,
        source_type: str | None = None,
        category: str | None = None,
    ) -> list[dict]:
        import numpy as np

        total_started=time.perf_counter(); dense_started=time.perf_counter()
        q = self.model.encode([query], normalize_embeddings=True, convert_to_numpy=True).astype("float32")
        if self.faiss_index is not None:
            dense_scores, dense_indexes = self.faiss_index.search(q, config.DENSE_K)
            dense = [self.chunks[int(i)]["chunk_id"] for i in dense_indexes[0] if i >= 0]
            sim_by_id = {self.chunks[int(i)]["chunk_id"]: float(score) for i,score in zip(dense_indexes[0],dense_scores[0]) if i >= 0}
        else:
            sims = np.asarray(self.embeddings) @ q[0]
            order = np.argsort(-sims)[:config.DENSE_K]
            dense = [self.chunks[int(i)]["chunk_id"] for i in order]
            sim_by_id = {self.chunks[int(i)]["chunk_id"]: float(sims[int(i)]) for i in order}
        dense_ms=(time.perf_counter()-dense_started)*1000; bm25_started=time.perf_counter()
        bm25 = self._bm25_ranking(query, config.BM25_K)
        bm25_ms=(time.perf_counter()-bm25_started)*1000; fusion_started=time.perf_counter()
        def parent_ranking(chunk_ids: list[str]) -> list[str]:
            seen, result = set(), []
            for chunk_id in chunk_ids:
                doc_id = self.by_id[chunk_id]["doc_id"]
                if doc_id not in seen:
                    seen.add(doc_id); result.append(doc_id)
            return result
        dense_docs, bm25_docs = parent_ranking(dense), parent_ranking(bm25)
        # Lexical-first tie breaking preserves exact model/attribute matches while
        # RRF still gives equal rank weight to the dense and sparse channels.
        fused = reciprocal_rank_fusion([bm25_docs, dense_docs])
        dense_rank={doc_id:i+1 for i,doc_id in enumerate(dense_docs)}; bm25_rank={doc_id:i+1 for i,doc_id in enumerate(bm25_docs)}
        representative: dict[str,str] = {}
        for chunk_id in dense + bm25:
            representative.setdefault(self.by_id[chunk_id]["doc_id"], chunk_id)
        sim_by_doc: dict[str,float] = {}
        for chunk_id, score in sim_by_id.items():
            doc_id = self.by_id[chunk_id]["doc_id"]
            sim_by_doc[doc_id] = max(sim_by_doc.get(doc_id, -1.0), score)

        # 开启重排时多取候选（RERANK_CANDIDATES），精排后再截断到 top_k
        limit = config.RERANK_CANDIDATES if self.reranker else top_k
        candidates = []
        seen_docs: set[str] = set()
        # A small semantic tie-break helps natural-language Chinese requests. For
        # model numbers/attributes, exact sparse evidence must remain dominant.
        dense_weight = config.DENSE_SCORE_WEIGHT if not re.search(r"[A-Za-z0-9]", query) else 0.0
        hybrid_scores = {doc_id: score + dense_weight * max(0.0, sim_by_doc.get(doc_id, 0.0)) for doc_id, score in fused.items()}
        for doc_id, score in sorted(hybrid_scores.items(), key=lambda x: -x[1]):
            cid = representative[doc_id]
            chunk = dict(self.by_id[cid])
            if source_type and chunk.get("source_type") != source_type:
                continue
            if category and category.casefold() not in str(chunk.get("category", "")).casefold():
                continue
            # Evaluation and tools operate on parent products/policies. Returning
            # five sibling chunks from one product would make Recall@5 misleading.
            if chunk.get("doc_id") in seen_docs:
                continue
            seen_docs.add(chunk.get("doc_id"))
            chunk["score"] = score
            chunk["dense_sim"] = sim_by_doc.get(doc_id, 0.0)
            chunk["dense_rank"] = dense_rank.get(doc_id)
            chunk["bm25_rank"] = bm25_rank.get(doc_id)
            candidates.append(chunk)
            if len(candidates) >= limit:
                break

        fusion_ms=(time.perf_counter()-fusion_started)*1000; rerank_started=time.perf_counter()
        if self.reranker and candidates:
            # 商品级去重：每个 doc 只保留融合分最高的 chunk，避免同一商品的兄弟 chunk
            # 在父卡重排下因得分相同而霸占 top_k、挤掉其他商品（破坏多样性）。
            if config.RERANK_DEDUP:
                seen, deduped = set(), []
                for c in candidates:  # candidates 已按融合分降序
                    if c["doc_id"] in seen:
                        continue
                    seen.add(c["doc_id"])
                    deduped.append(c)
                candidates = deduped

            # 默认对完整父卡精排（保留上下文）；chunk 模式仅看召回的子片段
            def rerank_text(c: dict) -> str:
                if config.RERANK_ON == "chunk":
                    return c["text"]
                return self.parents.get(c["doc_id"], c["text"])

            scores = self.reranker.predict([(query, rerank_text(c)) for c in candidates])
            for chunk, rs in zip(candidates, scores):
                chunk["rerank_score"] = float(rs)
            candidates.sort(key=lambda c: -c["rerank_score"])

        rerank_ms=(time.perf_counter()-rerank_started)*1000 if self.reranker else 0.0
        self.last_timing={"dense_ms":dense_ms,"bm25_ms":bm25_ms,"fusion_filter_ms":fusion_ms,"rerank_ms":rerank_ms,"total_ms":(time.perf_counter()-total_started)*1000,"dense_backend":self.dense_backend}
        return candidates[:top_k]

    def format_context(self, chunks: list[dict]) -> str:
        seen, blocks = set(), []
        for c in chunks:
            doc_id = c["doc_id"]
            if doc_id in seen:
                continue
            seen.add(doc_id)
            blocks.append(f"[资料{len(blocks) + 1}]\n{self.parents[doc_id]}")
        return "\n\n".join(blocks)
