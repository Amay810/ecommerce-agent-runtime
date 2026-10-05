"""Offline counterfactual for research-find-v1 query formulation.

For each answerable task in a stored run, replay the policy's first
``search_catalog`` call three ways against the same index: unchanged, without
the ``category``/``max_price`` filters, and with the raw user request. It also
counts filters that the request never stated. No model is called.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path

from ecommerce_rag.hybrid_retriever import HybridRetriever
from ecommerce_rag.tools import RetailTools


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, default=Path("ecommerce_rag/data/research_find_v1.jsonl"))
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    tasks = {}
    for line in args.tasks.read_text(encoding="utf-8").splitlines():
        if line.strip():
            task = json.loads(line)
            tasks[task["task_id"]] = task
    tools = RetailTools(args.db, HybridRetriever(args.index))
    rows = sqlite3.connect(args.store).execute("SELECT task_id, trajectory_json FROM trajectories").fetchall()

    calls: Counter = Counter()
    first_search: Counter = Counter()
    for task_id, trajectory_json in rows:
        task = tasks[task_id]
        contract = task["evaluation_contract"]
        gold = contract["answer_product_id"]
        price_stated = any(c["kind"] == "price" for c in contract["constraints"])
        searches = [c for c in json.loads(trajectory_json)["tool_calls"] if c["name"] == "search_catalog"]
        for search in searches:
            arguments = search["arguments"]
            calls["search_calls"] += 1
            calls["max_price_not_in_request"] += arguments.get("max_price") is not None and not price_stated
            calls["category_set"] += bool(arguments.get("category"))
        if not gold or not searches:
            continue

        def hit(arguments: dict) -> bool:
            result = tools.search_catalog(**arguments)
            return gold in [item["product_id"] for item in result.get("items", [])]

        first = searches[0]["arguments"]
        first_search["answerable_tasks"] += 1
        first_search["replay_hit"] += hit({k: v for k, v in first.items() if k in ("query", "top_k", "category", "max_price")})
        first_search["without_filters_hit"] += hit({"query": first["query"], "top_k": 5})
        first_search["raw_request_hit"] += hit({"query": task["user_goal"], "top_k": 5})
    report = {"store": str(args.store), "index": str(args.index),
              "search_calls": dict(calls), "first_search_gold_in_top5": dict(first_search)}
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
