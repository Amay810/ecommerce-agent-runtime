"""Generate research-write-v1 tasks: research a replacement, then change an order.

Each task starts from a real pending or delivered order in the seeded retail
database. The user asks to change the ordered product to one described by
natural constraints. Answers are fixed in code first:

* ``modify_pending`` / ``exchange_delivered``: the target is a same-brand,
  same-type sibling of the ordered product that satisfies the constraints
  exactly, is the only product that could satisfy them (broad relation, every
  constraint necessary, all observable through the tools), and the ordered
  product itself does not satisfy the attribute constraint;
* ``no_target``: no product could satisfy the constraints, the ordered product
  is a near miss, and the order must stay unchanged.

The expected final order row is recorded for state-level scoring.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from ecommerce_rag.domain import SCORING_VERSION_RESEARCH_WRITE_V1
from ecommerce_rag.orders import connect, seed_database
from ecommerce_rag.research_write import RESEARCH_WRITE_TOOLS
from scripts.generate_research_find_tasks import (
    Catalog,
    Constraint,
    Product,
    candidate_constraints,
    exact,
    find_unsatisfiable,
    group_key,
    load_products,
    minimal_unique,
    norm,
    observable,
    possible,
    sha256_file,
)


GENERATOR_VERSION = "research-write-generator-v1"
TASK_TYPES = ("modify_pending", "exchange_delivered", "no_target")
WRITE_TOOL = {"pending": "modify_pending_order_items", "delivered": "exchange_delivered_order_items"}
TEMPLATES = {
    "pending": "我的订单 {order_id} 还没发货，我想把里面的商品改成{constraints}的 {type}。验证码是 {code}。",
    "delivered": "我的订单 {order_id} 已经签收了，我想把商品换成{constraints}的 {type}。验证码是 {code}。",
}
OUTPUT_REQUIREMENTS = {
    "final_answer": (
        "只依据工具返回的资料判断条件是否满足。修改完成后用一句话说明结果；如果商品库里没有明确满足"
        "全部条件的商品，不要修改订单，直接说明没有。"
    )
}


def load_orders(seed: int) -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "orders.db"
        seed_database(path, seed=seed)
        conn = connect(path)
        try:
            rows = conn.execute(
                "SELECT o.*, u.verification_code FROM orders o JOIN users u ON u.user_id = o.user_id "
                "WHERE o.status IN ('pending', 'delivered') AND o.exchange_status IS NULL ORDER BY o.order_id"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()


def find_sibling_target(catalog: Catalog, current: Product, rng: random.Random) -> tuple[Product, list[Constraint]] | None:
    siblings = [p for p in catalog.by_leaf.get(norm(current.leaf), [])
                if p.product_id != current.product_id and p.brand and norm(p.brand) == norm(current.brand)]
    rng.shuffle(siblings)
    for target in siblings:
        brand, attrs, _ = candidate_constraints(target)
        if brand is None:
            continue
        rng.shuffle(attrs)
        for attr in attrs:
            if possible(current, attr):
                continue  # the ordered product must not already satisfy the request
            constraints = [Constraint("type", "type", target.leaf), brand, attr]
            if minimal_unique(catalog, target, constraints):
                return target, constraints
    return None


def split_for(product: Product, seed: int) -> str:
    key = f"{seed}:{norm(product.leaf)}:{norm(product.brand)}"
    return "exploration" if int(hashlib.sha256(key.encode()).hexdigest(), 16) % 2 == 0 else "locked"


def generate(products: list[Product], orders: list[dict[str, Any]], *, per_type_split: int, seed: int) -> list[dict[str, Any]]:
    catalog = Catalog(products)
    by_id = {p.product_id: p for p in products}
    rng = random.Random(seed)
    candidates = [o for o in orders if o["product_id"] in by_id and by_id[o["product_id"]].leaf
                  and by_id[o["product_id"]].brand]
    rng.shuffle(candidates)
    counts: Counter = Counter()
    used_products: set[str] = set()
    tasks: list[dict[str, Any]] = []
    for order in candidates:
        current = by_id[order["product_id"]]
        if current.product_id in used_products:
            continue
        split = split_for(current, seed)
        local = random.Random(f"{seed}:{order['order_id']}")
        status = order["status"]
        write_type = "modify_pending" if status == "pending" else "exchange_delivered"
        for task_type in (write_type, "no_target"):
            if counts[(task_type, split)] >= per_type_split:
                continue
            if task_type == "no_target":
                constraints = find_unsatisfiable(catalog, current, local)
                target = None
                if not constraints:
                    continue
            else:
                found = find_sibling_target(catalog, current, local)
                if not found or found[0].product_id in used_products:
                    continue
                target, constraints = found
            counts[(task_type, split)] += 1
            used_products.update({current.product_id} | ({target.product_id} if target else set()))
            final_product = target.product_id if target else current.product_id
            phrases = "，".join(c.phrase() for c in constraints[1:])
            tasks.append({
                "task_id": f"rw1_{split[:3]}_{task_type}_{counts[(task_type, split)]:03d}",
                "category": "research_write",
                "user_id": order["user_id"],
                "user_goal": TEMPLATES[status].format(order_id=order["order_id"], constraints=phrases,
                                                      type=constraints[0].value, code=order["verification_code"]),
                "seed": seed + len(tasks),
                "gold_doc_ids": [f"product:{target.product_id}"] if target else [],
                "allowed_tools": list(RESEARCH_WRITE_TOOLS),
                "required_tools": [],
                "split": split,
                "scoring_version": SCORING_VERSION_RESEARCH_WRITE_V1,
                "output_requirements": OUTPUT_REQUIREMENTS,
                "expected_state": {order["order_id"]: {
                    "status": status,
                    "product_id": final_product,
                    "item_ids": json.dumps([final_product], ensure_ascii=False) if target else order["item_ids"],
                    "exchange_status": "exchanged" if target and status == "delivered" else None,
                }},
                "metadata": {
                    "order_id": order["order_id"],
                    "verification_code": order["verification_code"],
                    "user_behavior": {"confirmation": True, "confirm_text": "确认执行",
                                      "verification_code": order["verification_code"]},
                },
                "evaluation_contract": {
                    "generator_version": GENERATOR_VERSION,
                    "task_type": task_type,
                    "order_id": order["order_id"],
                    "order_status": status,
                    "current_product_id": current.product_id,
                    "target_product_id": target.product_id if target else None,
                    "write_tool": WRITE_TOOL[status],
                    "payment_method_id": order["payment_method_id"],
                    "constraints": [c.to_dict() for c in constraints],
                    "near_miss_product_ids": catalog.near_misses(constraints)[:20],
                },
            })
            break
        if all(counts[(t, s)] >= per_type_split for t in TASK_TYPES for s in ("exploration", "locked")):
            break
    return tasks


def validate(tasks: list[dict[str, Any]], products: list[Product], orders: list[dict[str, Any]]) -> list[str]:
    catalog = Catalog(products)
    by_id = {p.product_id: p for p in products}
    by_order = {o["order_id"]: o for o in orders}
    errors: list[str] = []
    seen_orders: set[str] = set()
    for task in tasks:
        contract, tid = task["evaluation_contract"], task["task_id"]
        order = by_order.get(contract["order_id"])
        constraints = [Constraint(c["kind"], c["key"], c["value"]) for c in contract["constraints"]]
        if order is None or order["product_id"] != contract["current_product_id"]:
            errors.append(f"{tid}: order does not hold the recorded current product")
            continue
        if contract["order_id"] in seen_orders:
            errors.append(f"{tid}: order reused")
        seen_orders.add(contract["order_id"])
        if contract["write_tool"] != WRITE_TOOL[order["status"]]:
            errors.append(f"{tid}: write tool does not match order status")
        if any(token in task["user_goal"] for token in (contract["current_product_id"], contract.get("target_product_id") or "@")):
            errors.append(f"{tid}: request leaks a product id")
        current = by_id[contract["current_product_id"]]
        target_id = contract.get("target_product_id")
        if target_id:
            target = by_id[target_id]
            if not all(exact(target, c) for c in constraints) or not all(observable(target, c) for c in constraints):
                errors.append(f"{tid}: target does not satisfy or expose every constraint")
            if not minimal_unique(catalog, target, constraints):
                errors.append(f"{tid}: target is not minimal-unique")
            if all(possible(current, c) for c in constraints):
                errors.append(f"{tid}: ordered product already satisfies the request")
        elif catalog.matches(constraints):
            errors.append(f"{tid}: no-target task has a possible match")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--products", type=Path, default=Path("ecommerce_rag/data/amazon_products_5k.jsonl"))
    parser.add_argument("--category-paths", type=Path, default=Path("ecommerce_rag/data/amazon_products_5k.category_paths.jsonl"))
    parser.add_argument("--db-seed", type=int, default=20260720, help="seed_database seed used by the harness")
    parser.add_argument("--per-type-split", type=int, default=15)
    parser.add_argument("--seed", type=int, default=20261008)
    parser.add_argument("--output", type=Path, default=Path("ecommerce_rag/data/research_write_v1.jsonl"))
    args = parser.parse_args()

    products = load_products(args.products, args.category_paths)
    orders = load_orders(args.db_seed)
    tasks = generate(products, orders, per_type_split=args.per_type_split, seed=args.seed)
    errors = validate(tasks, products, orders)
    if errors:
        raise SystemExit("research-write tasks failed validation:\n" + "\n".join(errors))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in tasks), encoding="utf-8")
    manifest = {
        "generator_version": GENERATOR_VERSION,
        "scoring_version": SCORING_VERSION_RESEARCH_WRITE_V1,
        "seed": args.seed,
        "db_seed": args.db_seed,
        "per_type_split": args.per_type_split,
        "tasks": len(tasks),
        "counts": {f"{t}/{s}": sum(x["evaluation_contract"]["task_type"] == t and x["split"] == s for x in tasks)
                   for t in TASK_TYPES for s in ("exploration", "locked")},
        "inputs": {
            "products": {"path": str(args.products), "sha256": sha256_file(args.products)},
            "category_paths": {"path": str(args.category_paths), "sha256": sha256_file(args.category_paths)},
        },
        "output_sha256": sha256_file(args.output),
        "rules": [
            "orders come from seed_database(db_seed); each order is used once",
            "write targets are same-brand, same-type siblings, minimal-unique and observable",
            "the ordered product does not satisfy the requested attribute",
            "no_target: no product could satisfy the constraints; the order must stay unchanged",
            "expected_state is the full scored order row; splits by (leaf, brand) group of the ordered product",
        ],
    }
    args.output.with_suffix(".manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: manifest[k] for k in ("tasks", "counts", "output_sha256")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
