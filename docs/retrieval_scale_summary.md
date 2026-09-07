# Retrieval scale and ablation

## NSCC FAISS scale result

The same 250-query scale set is evaluated while distractors grow from 40 to
5,000 products. It contains 200 programmatic attribute cases and 50 generated
hard candidates; the latter are not human-labelled yet.

| Products | Chunks | Recall@1 | Recall@5 | MRR | nDCG@5 | P50 | P95 | Index |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 40 | 365 | 0.9583 | 1.0000 | 0.9868 | 0.9901 | 9.44ms | 9.96ms | 1.58MB |
| 1,000 | 8,770 | 0.9625 | 0.9958 | 0.9878 | 0.9888 | 20.52ms | 28.93ms | 37.30MB |
| 5,000 | 43,953 | 0.9542 | 0.9917 | 0.9798 | 0.9826 | 79.59ms | 125.73ms | 185.52MB |

FAISS IndexFlatIP is the reported NSCC dense backend. Build time was not
captured in the first run. The historical measurement job referenced in the
original report is not part of this release tree; use the local index-build
command in `docs/retrieval.md` for the current reproducible path.

## 5k reranker decision

| Configuration | Recall@1 | Recall@5 | nDCG@5 | P95 |
|---|---:|---:|---:|---:|
| Hybrid + constraints | 0.9542 | 0.9917 | 0.9826 | 108.37ms |
| + BGE reranker | 0.9750 | 0.9958 | 0.9958 | 219.12ms |

The reranker improves top-rank quality but only adds 0.0041 Recall@5 while
roughly doubling P95. It remains off by default and is suitable for conditional
use on high-value comparison or multi-constraint requests.

## Hard-set boundary

The title-debiased v2 locked set reaches only Recall@5=0.8029. A dense threshold
raises no-answer accuracy to 0.75 but reduces Recall@5 to 0.4964. The fresh v3
holdout reaches Recall@5=0.6889, with typo and attribute-only queries as the main
failures. These results are reported separately from the easier scale set.
