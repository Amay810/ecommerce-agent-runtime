"""Recover Amazon category paths for the reproducible 5k corpus.

``build_amazon_5k`` flattens the ``categories`` list into one space-joined
string, which loses the leaf category needed for natural product-type requests.
This script replays the same pinned streaming selection and writes a sidecar of
``id``/``source_asin``/``category_path`` rows. The corpus and any retrieval
index built from it are left unchanged.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from scripts.build_amazon_5k import CATEGORIES, PINNED_REVISION, stream
from scripts.prepare_amazon import clean, normalize


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--products", type=Path, default=Path("ecommerce_rag/data/amazon_products_5k.jsonl"))
    parser.add_argument("--output", type=Path, default=Path("ecommerce_rag/data/amazon_products_5k.category_paths.jsonl"))
    parser.add_argument("--per-category", type=int, default=2500)
    parser.add_argument("--revision", default=PINNED_REVISION)
    args = parser.parse_args()

    products = [json.loads(line) for line in args.products.read_text(encoding="utf-8").splitlines() if line.strip()]
    replayed: list[dict] = []
    for category in CATEGORIES:
        selected = []
        for raw in stream("meta", category, args.revision):
            item = normalize(raw)
            if not item:
                continue
            path = [clean(x) for x in (raw.get("categories") or []) if clean(x)]
            selected.append({
                "source_asin": item["source_asin"],
                "category_path": path,
                "main_category": clean(raw.get("main_category")),
            })
            if len(selected) >= args.per_category:
                break
        replayed.extend(selected)
    if len(replayed) != len(products):
        raise SystemExit(f"replayed {len(replayed)} rows for {len(products)} products")
    rows = []
    for product, row in zip(products, replayed):
        if product["source_asin"] != row["source_asin"]:
            raise SystemExit(f"selection drift at {product['id']}: {product['source_asin']} != {row['source_asin']}")
        rows.append({"id": product["id"], **row})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    leaves = Counter(row["category_path"][-1] for row in rows if row["category_path"])
    print(json.dumps({
        "rows": len(rows),
        "with_category_path": sum(bool(row["category_path"]) for row in rows),
        "distinct_leaf_categories": len(leaves),
        "top_leaf_categories": leaves.most_common(10),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
