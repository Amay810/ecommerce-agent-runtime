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


def find_unsatisfiable(catalog: Catalog, product: Product, rng: random.Random,
                       keys: set[str] | None = None) -> list[Constraint] | None:
    brand, attrs, _ = candidate_constraints(product)
    if keys is not None:
        attrs = [a for a in attrs if a.key in keys]
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


def constraint_signature(constraints: list[dict[str, Any]] | list[Constraint]) -> tuple[str, ...]:
    """Structure of a task's non-type constraints, e.g. ('attribute:Color', 'brand')."""
    parts = []
    for item in constraints:
        kind, key = (item.kind, item.key) if isinstance(item, Constraint) else (item["kind"], item["key"])
        if kind != "type":
            parts.append(f"attribute:{key}" if kind == "attribute" else kind)
    return tuple(sorted(parts))


def find_for_signature(catalog: Catalog, product: Product, task_type: str, signature: tuple[str, ...],
                       rng: random.Random) -> list[Constraint] | None:
    """Build a task of ``task_type`` whose constraints have exactly ``signature``."""
    brand, attrs, price = candidate_constraints(product)
    by_key = {a.key: a for a in attrs}
    wanted_keys = {part.split(":", 1)[1] for part in signature if part.startswith("attribute:")}
    if task_type == "no_answer":
        if "brand" not in signature or len(wanted_keys) != 1 or len(signature) != 2:
            return None
        found = find_unsatisfiable(catalog, product, rng, keys=wanted_keys)
        return found if found and constraint_signature(found) == signature else None
    parts: list[Constraint] = []
    if "brand" in signature:
        if brand is None:
            return None
        parts.append(brand)
    for key in sorted(wanted_keys):
        if key not in by_key:
            return None
        parts.append(by_key[key])
    if "price" in signature:
        if price is None:
            return None
        parts.append(price)
    constraints = [Constraint("type", "type", product.leaf), *parts]
    if constraint_signature(constraints) != signature:
        return None
    return constraints if minimal_unique(catalog, product, constraints) else None


def group_key(product: Product) -> tuple[str, str]:
    return norm(product.leaf), norm(product.brand)


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


def build_task(catalog: Catalog, product: Product, constraints: list[Constraint], *, task_type: str,
               split: str, index: int, seed: int, rng: random.Random, brand_text: str | None,
               round_tag: str, extra_contract: dict[str, Any] | None = None) -> dict[str, Any]:
    answer_id = None if task_type == "no_answer" else product.product_id
    return {
        "task_id": f"rf{round_tag}_{split[:3]}_{task_type}_{index:03d}",
        "category": "research_find",
        "user_id": "U0001",
        "user_goal": render(str(product.leaf), constraints, rng, brand_text=brand_text),
        "seed": seed,
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
            **(extra_contract or {}),
        },
    }


def generate(products: list[Product], *, per_type_split: int, seed: int, excluded: set[str],
             only_split: str | None = None, excluded_groups: set[tuple[str, str]] | None = None,
             round_tag: str = "1", extra_contract: dict[str, Any] | None = None,
             balance_types: bool = False) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    catalog = Catalog(products)
    excluded_groups = excluded_groups or set()
    eligible = [p for p in products if p.leaf and p.product_id not in excluded and group_key(p) not in excluded_groups]
    rng.shuffle(eligible)
    used: set[str] = set()
    counts: Counter = Counter()
    tasks: list[dict[str, Any]] = []
    brands = {norm(p.brand) for p in products if p.brand}
    splits = (only_split,) if only_split else ("exploration", "locked")

    def full(task_type: str, split: str) -> bool:
        return counts[(task_type, split)] >= per_type_split

    for product in eligible:
        if all(full(t, s) for t in TASK_TYPES for s in splits):
            break
        if product.product_id in used:
            continue
        split = only_split or split_for(product, seed)
        local = random.Random(f"{seed}:{product.product_id}")
        order = TASK_TYPES
        if balance_types:
            # Types that share a product structure (near_sku/typo_alias) would
            # otherwise starve whichever is tried later once products run short.
            order = sorted(TASK_TYPES, key=lambda t: (counts[(t, split)], TASK_TYPES.index(t)))
        for task_type in order:
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
            tasks.append(build_task(
                catalog, product, constraints, task_type=task_type, split=split, index=index,
                seed=seed + len(tasks), rng=local, brand_text=brand_text, round_tag=round_tag,
                extra_contract=extra_contract,
            ))
            break
    return tasks


def generate_failure_driven(products: list[Product], failures: list[dict[str, Any]], *, variants_per_failure: int,
                            seed: int, excluded: set[str], round_tag: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """New exploration tasks that repeat the type and constraint structure of observed failures.

    Each failure asks for ``variants_per_failure`` tasks with the same task type
    and constraint signature, built from products never used before.
    """
    rng = random.Random(seed)
    catalog = Catalog(products)
    eligible = [p for p in products if p.leaf and p.product_id not in excluded]
    rng.shuffle(eligible)
    brands = {norm(p.brand) for p in products if p.brand}
    quotas: Counter = Counter()
    drivers: dict[tuple[str, tuple[str, ...]], list[dict[str, str]]] = defaultdict(list)
    for failure in failures:
        key = (failure["task_type"], tuple(failure["signature"]))
        quotas[key] += variants_per_failure
        drivers[key].append({"task_id": failure["task_id"], "failure_type": failure["failure_type"]})
    requested = dict(quotas)
    counts: Counter = Counter()
    tasks: list[dict[str, Any]] = []
    used: set[str] = set()
    for product in eligible:
        if not +quotas:
            break
        local = random.Random(f"{seed}:{product.product_id}")
        for key in sorted(k for k, n in quotas.items() if n > 0):
            task_type, signature = key
            constraints = find_for_signature(catalog, product, task_type, signature, local)
            if not constraints:
                continue
            brand_text = None
            if task_type == "typo_alias":
                brand = next(c for c in constraints if c.kind == "brand")
                brand_text = typo(str(brand.value), local)
                if not brand_text or norm(brand_text) in brands:
                    continue
            quotas[key] -= 1
            counts[task_type] += 1
            used.add(product.product_id)
            tasks.append(build_task(
                catalog, product, constraints, task_type=task_type, split="exploration",
                index=counts[task_type], seed=seed + len(tasks), rng=local, brand_text=brand_text,
                round_tag=round_tag, extra_contract={
                    "generation_mode": "failure_driven",
                    "constraint_signature": list(signature),
                    "driven_by": drivers[key],
                },
            ))
            break
    report = {
        "requested": {f"{t}|{'+'.join(sig)}": n for (t, sig), n in sorted(requested.items())},
        "shortfall": {f"{t}|{'+'.join(sig)}": n for (t, sig), n in sorted(quotas.items()) if n > 0},
    }
    return tasks, report


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
    parser.add_argument("--evolve-from-report", type=Path,
                        help="Harness report whose exploration failures drive a new round")
    parser.add_argument("--parent-tasks", type=Path, default=Path("ecommerce_rag/data/research_find_v1.jsonl"))
    parser.add_argument("--variants-per-failure", type=int, default=2)
    parser.add_argument("--round-tag", default="2")
    parser.add_argument("--locked-seed", type=int, default=20261006)
    parser.add_argument("--mine-candidates", action="store_true",
                        help="Write an untargeted exploration candidate pool for model-in-the-loop mining")
    parser.add_argument("--exclude-tasks", type=Path, nargs="*", default=[],
                        help="Task files whose source products are excluded")
    parser.add_argument("--reserve-locked-from", type=Path, nargs="*", default=[],
                        help="Task files whose locked (leaf, brand) groups stay reserved (the active held-out)")
    args = parser.parse_args()

    products = load_products(args.products, args.category_paths)
    if args.evolve_from_report:
        evolve(args, products)
        return
    if args.mine_candidates:
        mining_candidates(args, products)
        return
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


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def mining_candidates(args: argparse.Namespace, products: list[Product]) -> None:
    """Untargeted exploration candidates; the current system later selects hard cases."""
    by_id = {p.product_id: p for p in products}
    def read(paths: list[Path]) -> list[dict[str, Any]]:
        return [json.loads(line) for path in paths
                for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    used = {t["evaluation_contract"]["source_product_id"] for t in read(args.exclude_tasks)}
    reserved_groups = {group_key(by_id[t["evaluation_contract"]["source_product_id"]])
                       for t in read(args.reserve_locked_from) if t["split"] == "locked"}
    tasks = generate(products, per_type_split=args.per_type_split, seed=args.seed,
                     excluded=excluded_products(args.exclude) | used, only_split="exploration",
                     excluded_groups=reserved_groups, round_tag=args.round_tag,
                     extra_contract={"generation_mode": "mining_candidate"}, balance_types=True)
    errors = validate_tasks(tasks, products)
    if errors:
        raise SystemExit("mining candidates failed validation:\n" + "\n".join(errors))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in tasks), encoding="utf-8")
    manifest = {
        "generator_version": GENERATOR_VERSION,
        "mode": "mining_candidates",
        "round_tag": args.round_tag,
        "seed": args.seed,
        "per_type": args.per_type_split,
        "tasks": len(tasks),
        "by_type": dict(Counter(t["evaluation_contract"]["task_type"] for t in tasks)),
        "excluded_task_files": [{"path": str(p), "sha256": sha256_file(p)} for p in args.exclude_tasks],
        "reserved_locked_files": [{"path": str(p), "sha256": sha256_file(p)} for p in args.reserve_locked_from],
        "reserved_locked_groups": len(reserved_groups),
        "inputs": {
            "products": {"path": str(args.products), "sha256": sha256_file(args.products)},
            "category_paths": {"path": str(args.category_paths), "sha256": sha256_file(args.category_paths)},
        },
        "output_sha256": sha256_file(args.output),
        "rules": [
            "exploration only; excludes every source product used by the excluded task files",
            "excludes every (leaf, brand) group of a locked task in the reserved files (active held-out)",
            "types assigned by remaining quota; all round-1 invariants re-validated",
        ],
    }
    args.output.with_suffix(".manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: manifest[k] for k in ("tasks", "by_type", "reserved_locked_groups", "output_sha256")},
                     ensure_ascii=False, indent=2))


def evolve(args: argparse.Namespace, products: list[Product]) -> None:
    """Failure-driven exploration plus a fresh, untargeted locked split."""
    by_id = {p.product_id: p for p in products}
    parent = [json.loads(line) for line in args.parent_tasks.read_text(encoding="utf-8").splitlines() if line.strip()]
    parent_by_id = {t["task_id"]: t for t in parent}
    report = json.loads(args.evolve_from_report.read_text(encoding="utf-8"))
    failures = []
    for row in report["details"]:
        task = parent_by_id.get(row["task_id"])
        if task is None or row.get("split") != "exploration" or row.get("success"):
            continue
        contract = task["evaluation_contract"]
        failures.append({
            "task_id": row["task_id"], "failure_type": row.get("failure_type"),
            "task_type": contract["task_type"], "signature": list(constraint_signature(contract["constraints"])),
        })
    used_sources = {t["evaluation_contract"]["source_product_id"] for t in parent}
    excluded = excluded_products(args.exclude) | used_sources
    explore, driven = generate_failure_driven(
        products, failures, variants_per_failure=args.variants_per_failure, seed=args.seed,
        excluded=excluded, round_tag=args.round_tag)
    explore_sources = {t["evaluation_contract"]["source_product_id"] for t in explore}
    exploration_groups = {group_key(by_id[t["evaluation_contract"]["source_product_id"]])
                          for t in parent + explore if t["split"] == "exploration"}
    locked = generate(products, per_type_split=args.per_type_split, seed=args.locked_seed,
                      excluded=excluded | explore_sources, only_split="locked",
                      excluded_groups=exploration_groups, round_tag=args.round_tag,
                      extra_contract={"generation_mode": "fresh_heldout"}, balance_types=True)
    tasks = explore + locked
    errors = validate_tasks(tasks, products)
    if errors:
        raise SystemExit("evolved tasks failed validation:\n" + "\n".join(errors))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in tasks), encoding="utf-8")
    manifest = {
        "generator_version": GENERATOR_VERSION,
        "scoring_version": SCORING_VERSION_RESEARCH_FIND_V1,
        "round_tag": args.round_tag,
        "parent_tasks": {"path": str(args.parent_tasks), "sha256": sha256_file(args.parent_tasks)},
        "driving_report": {"path": str(args.evolve_from_report), "sha256": sha256_file(args.evolve_from_report),
                           "configuration": report.get("configuration"), "split": "exploration"},
        "driving_failures": dict(Counter(f"{f['task_type']}|{f['failure_type']}" for f in failures).most_common()),
        "variants_per_failure": args.variants_per_failure,
        "exploration_seed": args.seed,
        "locked_seed": args.locked_seed,
        "failure_driven_exploration": {**driven, "tasks": len(explore),
                                       "by_type": dict(Counter(t["evaluation_contract"]["task_type"] for t in explore))},
        "fresh_locked": {"tasks": len(locked), "per_type": args.per_type_split,
                         "by_type": dict(Counter(t["evaluation_contract"]["task_type"] for t in locked))},
        "inputs": {
            "products": {"path": str(args.products), "sha256": sha256_file(args.products)},
            "category_paths": {"path": str(args.category_paths), "sha256": sha256_file(args.category_paths)},
            "excluded_gold_sources": [str(p) for p in args.exclude],
        },
        "output_sha256": sha256_file(args.output),
        "rules": [
            "only exploration failures drive generation; locked results never do",
            "exploration variants repeat a failed task's type and constraint signature on unused products",
            "locked is untargeted: same per-type balance as round 1, fresh seed, types assigned by remaining quota",
            "locked excludes every previously used source product and every (leaf, brand) group seen in exploration",
            "all round-1 invariants are re-validated on the serialized tasks",
        ],
    }
    args.output.with_suffix(".manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: manifest[k] for k in ("driving_failures", "failure_driven_exploration", "fresh_locked",
                                               "output_sha256")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
