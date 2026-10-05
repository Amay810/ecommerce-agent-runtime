"""Generate research-find-v1 product-search tasks with code-locked answers.

Every answer is fixed in code before any request text is rendered:

* the target product satisfies every constraint exactly;
* under a deliberately broad matching relation (attribute containment or a
  phrase in title/description/attributes, unknown price counted as possible),
  no other catalog product can satisfy all constraints;
* every non-type constraint is necessary: dropping it admits another product;
* every constraint is observable through the current tools (search result
  fields or the ``get_product`` evidence window), so the task is verifiable.

No-answer tasks swap one constraint to a value seen in the same product type
and require that no product satisfies the result under the broad relation.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import random
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ecommerce_rag.domain import SCORING_VERSION_RESEARCH_FIND_V1
from ecommerce_rag.research_find import RESEARCH_FIND_TOOLS
from ecommerce_rag.retrieval_index import build_product_chunks


GENERATOR_VERSION = "research-find-generator-v1"
TASK_TYPES = ("multi_constraint", "near_sku", "no_answer", "typo_alias")
GET_PRODUCT_EVIDENCE_WINDOW = 5  # RetailTools.get_product returns chunks[:5]
ATTRIBUTE_PHRASES = {
    "Color": "颜色是 {v}",
    "Material": "材质是 {v}",
    "Special Feature": "带有 {v} 特性",
    "Style": "风格是 {v}",
    "Shape": "形状是 {v}",
    "Compatible Devices": "兼容 {v}",
    "Connectivity Technology": "连接方式是 {v}",
    "Form Factor": "外形是 {v}",
    "Mounting Type": "安装方式是 {v}",
    "Pattern": "图案是 {v}",
    "Size": "尺寸规格是 {v}",
    "Capacity": "容量是 {v}",
    "Connector Type": "接口是 {v}",
    "Fabric Type": "面料是 {v}",
    "Finish Type": "表面工艺是 {v}",
    "Closure Type": "闭合方式是 {v}",
    "Frame Material": "框架材质是 {v}",
    "Room Type": "适用于 {v}",
    "Theme": "主题是 {v}",
    "Age Range (Description)": "适用人群是 {v}",
}
BRAND_PHRASE = "品牌是 {v}"
PRICE_PHRASE = "预算 {v} 元以内"
EXCLUDED_VALUES = {"generic", "amazon renewed", "unknown", "n/a", "na", "none", "other", "see description", "not applicable", "occasion"}
TEMPLATES = (
    "我想买一款 {type}，{constraints}。请帮我在商品库里确认具体是哪一款。",
    "请帮我找一个 {type}：{constraints}。",
    "有没有{constraints}的 {type}？帮我确认一下是哪个商品。",
)
OUTPUT_REQUIREMENTS = {
    "final_answer": (
        "只依据工具返回的商品资料判断条件是否满足。找到后只给出一个商品编号（形如 P01234），"
        "回答中不要出现其他商品编号；如果商品资料里没有明确满足全部条件的商品，直接说明没有，"
        "不要给出任何商品编号。"
    )
}


def norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def contains_phrase(text: str, value: str) -> bool:
    phrase = norm(value)
    if not phrase:
        return False
    return re.search(rf"(?<![0-9a-z]){re.escape(phrase)}(?![0-9a-z])", text) is not None


def usable_value(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = norm(value)
    return (2 <= len(text) <= 30 and text not in EXCLUDED_VALUES and ";" not in text
            and text.count(",") <= 1 and not re.fullmatch(r"[\d\s.,x]+", text))


@dataclass(frozen=True)
class Constraint:
    kind: str  # type | brand | attribute | price
    key: str
    value: Any

    def phrase(self) -> str:
        if self.kind == "brand":
            return BRAND_PHRASE.format(v=self.value)
        if self.kind == "price":
            return PRICE_PHRASE.format(v=self.value)
        return ATTRIBUTE_PHRASES[self.key].format(v=self.value)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "key": self.key, "value": self.value}


@dataclass
class Product:
    product_id: str
    title: str
    leaf: str | None
    price: float | None
    brand: str | None
    attributes: dict[str, str]
    full_text: str
    observable_text: str
    type_text: str = field(default="")


def load_products(products_path: Path, category_paths: Path) -> list[Product]:
    paths = {}
    for line in category_paths.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            paths[row["id"]] = row.get("category_path") or []
    products = []
    for line in products_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        attrs = {k: v for k, v in (raw.get("attributes") or {}).items() if isinstance(v, str)}
        brand = attrs.get("Brand") or attrs.get("brand_or_store")
        chunks, _ = build_product_chunks([raw])
        evidence = [c["text"] for c in chunks[:GET_PRODUCT_EVIDENCE_WINDOW]]
        path = paths.get(raw["id"]) or []
        products.append(Product(
            product_id=raw["id"], title=raw["title"], leaf=path[-1] if path else None,
            price=raw.get("price"), brand=brand, attributes=attrs,
            full_text=norm(" ".join([raw["title"], raw.get("description") or "", *attrs.values()])),
            observable_text=norm(" ".join([raw["title"], raw.get("category") or "", *evidence])),
            type_text=norm(" ".join([raw.get("category") or "", raw["title"]])),
        ))
    return products


def exact(product: Product, constraint: Constraint) -> bool:
    if constraint.kind == "type":
        return product.leaf is not None and norm(product.leaf) == norm(constraint.value)
    if constraint.kind == "brand":
        return norm(product.brand) == norm(constraint.value)
    if constraint.kind == "price":
        return product.price is not None and product.price <= constraint.value
    return norm(product.attributes.get(constraint.key)) == norm(constraint.value)


def possible(product: Product, constraint: Constraint) -> bool:
    """Broad relation used for uniqueness: anything an Agent could defend."""
    if constraint.kind == "type":
        return exact(product, constraint) or contains_phrase(product.type_text, constraint.value)
    if constraint.kind == "price":
        return product.price is None or product.price <= constraint.value
    wanted = norm(constraint.value)
    actual = norm(product.brand if constraint.kind == "brand" else product.attributes.get(constraint.key))
    if actual and (wanted in actual or actual in wanted):
        return True
    return contains_phrase(product.full_text, constraint.value)


def observable(product: Product, constraint: Constraint) -> bool:
    if constraint.kind == "type":
        return True  # the category string is in every search result
    if constraint.kind == "price":
        return product.price is not None
    return contains_phrase(product.observable_text, constraint.value)


def nice_ceiling(price: float) -> int:
    step = 5 if price < 50 else 10 if price < 200 else 50
    return int(math.ceil(price * 1.15 / step) * step)


class Catalog:
    def __init__(self, products: list[Product]):
        self.products = products
        self.by_leaf: dict[str, list[Product]] = defaultdict(list)
        self._type_pools: dict[str, list[Product]] = {}
        for product in products:
            if product.leaf:
                self.by_leaf[norm(product.leaf)].append(product)

    def type_pool(self, constraint: Constraint) -> list[Product]:
        key = norm(constraint.value)
        if key not in self._type_pools:
            self._type_pools[key] = [p for p in self.products if possible(p, constraint)]
        return self._type_pools[key]

    def matches(self, constraints: Iterable[Constraint]) -> list[Product]:
        """Products that could satisfy every constraint; ``constraints[0]`` is the type."""
        items = list(constraints)
        return [p for p in self.type_pool(items[0]) if all(possible(p, c) for c in items[1:])]

    def near_misses(self, constraints: list[Constraint]) -> list[str]:
        result = set()
        for index in range(1, len(constraints)):
            rest = constraints[:index] + constraints[index + 1:]
            result.update(p.product_id for p in self.matches(rest))
        return sorted(result)


def candidate_constraints(product: Product) -> tuple[Constraint | None, list[Constraint], Constraint | None]:
    brand = None
    if product.brand and usable_value(product.brand) and observable(product, Constraint("brand", "Brand", product.brand)):
        brand = Constraint("brand", "Brand", product.brand)
    attrs = []
    for key in ATTRIBUTE_PHRASES:
        value = product.attributes.get(key)
        if usable_value(value):
            constraint = Constraint("attribute", key, value.strip())
            if observable(product, constraint):
                attrs.append(constraint)
    price = Constraint("price", "price", nice_ceiling(product.price)) if product.price else None
    return brand, attrs, price


def minimal_unique(catalog: Catalog, target: Product, constraints: list[Constraint]) -> bool:
    if [p.product_id for p in catalog.matches(constraints)] != [target.product_id]:
        return False
    for index in range(1, len(constraints)):  # constraints[0] is the product type
        rest = constraints[:index] + constraints[index + 1:]
        if len(catalog.matches(rest)) < 2:
            return False
    return True


def find_answerable(catalog: Catalog, product: Product, *, with_brand: bool, rng: random.Random) -> list[Constraint] | None:
    type_constraint = Constraint("type", "type", product.leaf)
    brand, attrs, price = candidate_constraints(product)
    extras = list(attrs) + ([price] if price else [])
    rng.shuffle(extras)
    if with_brand:
        if brand is None:
            return None
        pools = [[brand, x] for x in extras] + [[brand, x, y] for x, y in itertools.combinations(extras, 2)]
    else:
        pools = [list(pair) for pair in itertools.combinations(extras, 2)]
        pools += [list(triple) for triple in itertools.combinations(extras, 3)]
    for extra in pools[:60]:
        constraints = [type_constraint, *extra]
        if minimal_unique(catalog, product, constraints):
            return constraints
    return None


def find_unsatisfiable(catalog: Catalog, product: Product, rng: random.Random) -> list[Constraint] | None:
    brand, attrs, _ = candidate_constraints(product)
    if brand is None or not attrs:
        return None
    type_constraint = Constraint("type", "type", product.leaf)
    peers = catalog.by_leaf.get(norm(product.leaf), [])
    rng.shuffle(attrs)
    for attr in attrs:
        values = sorted({p.attributes[attr.key].strip() for p in peers
                         if usable_value(p.attributes.get(attr.key))
                         and norm(p.attributes[attr.key]) not in norm(attr.value)
                         and norm(attr.value) not in norm(p.attributes[attr.key])})
        rng.shuffle(values)
        for value in values[:10]:
            constraints = [type_constraint, brand, Constraint("attribute", attr.key, value)]
            if not catalog.matches(constraints) and catalog.near_misses(constraints):
                return constraints
    return None


def typo(value: str, rng: random.Random) -> str | None:
    letters = [i for i, ch in enumerate(value) if ch.isalpha()]
    if len(value) < 5 or len(letters) < 5:
        return None
    i = rng.choice(letters[1:-1])
    operations = [
        value[:i] + value[i + 1:],                       # drop a letter
        value[:i] + value[i] + value[i:],                # double a letter
    ]
    if value[i + 1:i + 2].isalpha():
        operations.append(value[:i] + value[i + 1] + value[i] + value[i + 2:])  # swap
    candidate = rng.choice(operations)
    return candidate if norm(candidate) != norm(value) else None


def render(product_type: str, constraints: list[Constraint], rng: random.Random, *, brand_text: str | None = None) -> str:
    phrases = []
    for constraint in constraints[1:]:
        if constraint.kind == "brand" and brand_text:
            phrases.append(BRAND_PHRASE.format(v=brand_text))
        else:
            phrases.append(constraint.phrase())
    return rng.choice(TEMPLATES).format(type=product_type, constraints="，".join(phrases))


def split_for(product: Product, seed: int) -> str:
    key = f"{seed}:{norm(product.leaf)}:{norm(product.brand)}"
    return "exploration" if int(hashlib.sha256(key.encode()).hexdigest(), 16) % 2 == 0 else "locked"


def excluded_products(paths: list[Path]) -> set[str]:
    excluded = set()
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                for doc_id in json.loads(line).get("gold_doc_ids", []):
                    if doc_id.startswith("product:"):
                        excluded.add(doc_id.split(":", 1)[1])
    return excluded


def generate(products: list[Product], *, per_type_split: int, seed: int, excluded: set[str]) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    catalog = Catalog(products)
    eligible = [p for p in products if p.leaf and p.product_id not in excluded]
    rng.shuffle(eligible)
    used: set[str] = set()
    counts: Counter = Counter()
    tasks: list[dict[str, Any]] = []
    brands = {norm(p.brand) for p in products if p.brand}

    def full(task_type: str, split: str) -> bool:
        return counts[(task_type, split)] >= per_type_split

    for product in eligible:
        if all(full(t, s) for t in TASK_TYPES for s in ("exploration", "locked")):
            break
        if product.product_id in used:
            continue
        split = split_for(product, seed)
        local = random.Random(f"{seed}:{product.product_id}")
        for task_type in TASK_TYPES:
            if full(task_type, split):
                continue
            brand_text = None
            if task_type == "no_answer":
                constraints = find_unsatisfiable(catalog, product, local)
            else:
                constraints = find_answerable(catalog, product, with_brand=task_type != "multi_constraint", rng=local)
            if not constraints:
                continue
            if task_type == "typo_alias":
                brand = next(c for c in constraints if c.kind == "brand")
                brand_text = typo(str(brand.value), local)
                if not brand_text or norm(brand_text) in brands:
                    continue
            index = counts[(task_type, split)] + 1
            counts[(task_type, split)] += 1
            used.add(product.product_id)
            answer_id = None if task_type == "no_answer" else product.product_id
            tasks.append({
                "task_id": f"rf1_{split[:3]}_{task_type}_{index:03d}",
                "category": "research_find",
                "user_id": "U0001",
                "user_goal": render(str(product.leaf), constraints, local, brand_text=brand_text),
                "seed": seed + len(tasks),
                "gold_doc_ids": [f"product:{answer_id}"] if answer_id else [],
                "allowed_tools": list(RESEARCH_FIND_TOOLS),
                "required_tools": [],
                "split": split,
                "scoring_version": SCORING_VERSION_RESEARCH_FIND_V1,
                "output_requirements": OUTPUT_REQUIREMENTS,
                "evaluation_contract": {
                    "generator_version": GENERATOR_VERSION,
                    "task_type": task_type,
                    "answer_product_id": answer_id,
                    "source_product_id": product.product_id,
                    "constraints": [c.to_dict() for c in constraints],
                    "rendered_brand": brand_text,
                    "near_miss_product_ids": catalog.near_misses(constraints)[:20],
                },
            })
            break
    return tasks


def validate_tasks(tasks: list[dict[str, Any]], products: list[Product]) -> list[str]:
    """Re-check every locked-answer invariant from the serialized tasks."""
    catalog = Catalog(products)
    by_id = {p.product_id: p for p in products}
    errors: list[str] = []
    for task in tasks:
        contract = task["evaluation_contract"]
        constraints = [Constraint(c["kind"], c["key"], c["value"]) for c in contract["constraints"]]
        answer = contract.get("answer_product_id")
        source = by_id[contract["source_product_id"]]
        tid = task["task_id"]
        if constraints[0].kind != "type":
            errors.append(f"{tid}: first constraint is not the product type")
        if re.search(r"P\d{5}", task["user_goal"]):
            errors.append(f"{tid}: request leaks a product id")
        if answer:
            target = by_id[answer]
            if not all(exact(target, c) for c in constraints):
                errors.append(f"{tid}: target does not satisfy every constraint exactly")
            if not all(observable(target, c) for c in constraints):
                errors.append(f"{tid}: a constraint is not observable through the tools")
            if not minimal_unique(catalog, target, constraints):
                errors.append(f"{tid}: answer is not minimal-unique under the broad relation")
        else:
            if catalog.matches(constraints):
                errors.append(f"{tid}: no-answer task has a possible match")
            if not all(observable(source, c) for c in constraints if c.kind != "attribute"):
                errors.append(f"{tid}: no-answer seed constraints are not observable")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--products", type=Path, default=Path("ecommerce_rag/data/amazon_products_5k.jsonl"))
    parser.add_argument("--category-paths", type=Path, default=Path("ecommerce_rag/data/amazon_products_5k.category_paths.jsonl"))
    parser.add_argument("--exclude", type=Path, nargs="*", default=[
        Path("ecommerce_rag/data/retrieval_eval_250.jsonl"),
        Path("ecommerce_rag/data/retrieval_eval_v2_300.jsonl"),
        Path("ecommerce_rag/data/retrieval_eval_v3_150.jsonl"),
    ])
    parser.add_argument("--per-type-split", type=int, default=25)
    parser.add_argument("--seed", type=int, default=20261005)
    parser.add_argument("--output", type=Path, default=Path("ecommerce_rag/data/research_find_v1.jsonl"))
    parser.add_argument("--validate-only", action="store_true", help="Re-check an existing task file and exit")
    args = parser.parse_args()

    products = load_products(args.products, args.category_paths)
    if args.validate_only:
        existing = [json.loads(line) for line in args.output.read_text(encoding="utf-8").splitlines() if line.strip()]
        errors = validate_tasks(existing, products)
        print(json.dumps({"tasks": len(existing), "errors": errors}, ensure_ascii=False, indent=2))
        raise SystemExit(1 if errors else 0)
    tasks = generate(products, per_type_split=args.per_type_split, seed=args.seed,
                     excluded=excluded_products(args.exclude))
    errors = validate_tasks(tasks, products)
    if errors:
        raise SystemExit("generated tasks failed validation:\n" + "\n".join(errors))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in tasks), encoding="utf-8")

    def sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    manifest = {
        "generator_version": GENERATOR_VERSION,
        "scoring_version": SCORING_VERSION_RESEARCH_FIND_V1,
        "seed": args.seed,
        "per_type_split": args.per_type_split,
        "tasks": len(tasks),
        "counts": {f"{t}/{s}": sum(x["evaluation_contract"]["task_type"] == t and x["split"] == s for x in tasks)
                   for t in TASK_TYPES for s in ("exploration", "locked")},
        "inputs": {
            "products": {"path": str(args.products), "sha256": sha256(args.products)},
            "category_paths": {"path": str(args.category_paths), "sha256": sha256(args.category_paths)},
            "excluded_gold_sources": [str(p) for p in args.exclude],
        },
        "output_sha256": sha256(args.output),
        "rules": [
            "answer fixed in code before text rendering",
            "target satisfies every constraint exactly",
            "no other product satisfies all constraints under the broad relation",
            "every non-type constraint is necessary (dropping it admits another product)",
            "every constraint is observable via search fields or the get_product evidence window",
            "no-answer: no product satisfies the swapped constraint set under the broad relation",
            "splits are assigned by (leaf category, brand) group; retrieval-eval gold products excluded",
        ],
    }
    args.output.with_suffix(".manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: manifest[k] for k in ("tasks", "counts", "output_sha256")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
