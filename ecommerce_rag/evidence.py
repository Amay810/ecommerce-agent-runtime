"""Evidence conversion and deterministic verification for executable trajectories."""

from __future__ import annotations

import json
import re
from fnmatch import fnmatchcase
from typing import Any, Iterable

from . import freshness
from .tool_schema import TOOL_SCHEMAS


EVIDENCE_CITATION = re.compile(r"\[E([1-9][0-9]*)\]")
EVIDENCE_RANGE = re.compile(r"\[E[1-9][0-9]*\s*[-–—]\s*E?[1-9][0-9]*\]")
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？?\n])")
_LEGACY_SOURCE_TAG = re.compile(r"\[资料\d+\]")
_ID_FACT = re.compile(r"\b(?:P[0-9]{3,5}|O[0-9]{6}|POL[0-9]{3})\b", re.I)
_ISO_DATE = re.compile(r"\b[0-9]{4}-[0-9]{2}-[0-9]{2}\b")
_ZH_DATE = re.compile(r"(?<![0-9])[0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日")
_EN_DATE = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+[0-9]{1,2},\s+[0-9]{4}\b", re.I)
_NUMERIC_FACT = re.compile(
    r"(?<![A-Za-z0-9])[0-9]+(?:\.[0-9]+)?\s*(?:元|个工作日|工作日|天|日|小时|分钟|件|个|%|mAh|Pa|kg|g|ml|pounds?|inches?)",
    re.I,
)

CONVERTER_TOOLS = frozenset({
    "search_catalog", "get_product", "compare_products", "get_policy",
    "get_order", "check_return_eligibility", "create_return_request",
    "cancel_pending_order", "modify_pending_order_address",
    "modify_pending_order_items", "modify_pending_order_payment",
    "modify_user_address", "return_delivered_order_items",
    "exchange_delivered_order_items",
})
EVIDENCE_BEARING_TOOLS = frozenset(schema["name"] for schema in TOOL_SCHEMAS
                                   if schema.get("evidence_bearing"))

_WRITE_TRANSITION_TOOLS = frozenset({
    "create_return_request",
    "cancel_pending_order",
    "modify_pending_order_address",
    "modify_pending_order_items",
    "modify_pending_order_payment",
    "modify_user_address",
    "return_delivered_order_items",
    "exchange_delivered_order_items",
})


def split_sentences(text: str) -> list[str]:
    """Split customer-facing text without depending on the archived verifier."""
    sentences: list[str] = []
    for raw in _SENTENCE_SPLIT.split(text):
        sentence = _LEGACY_SOURCE_TAG.sub("", raw).strip(" \n\t-•")
        if len(sentence) >= 6:
            sentences.append(sentence)
    return sentences


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _source(prefix: str, row: dict[str, Any], fallback: str) -> str:
    if row.get("doc_id"):
        return str(row["doc_id"])
    preferred = {
        "product": ("product_id", "order_id", "handoff_id"),
        "order": ("order_id", "product_id", "handoff_id"),
        "policy": ("policy_id", "product_id", "order_id"),
    }.get(prefix, ("product_id", "order_id", "handoff_id"))
    for key in preferred:
        if row.get(key):
            return f"{prefix}:{row[key]}"
    return fallback


def _source_item_count(tool_name: str, result: dict[str, Any]) -> int:
    if tool_name == "search_catalog":
        return len(result.get("items") or [])
    if tool_name == "get_policy":
        return len(result.get("policies") or [])
    if tool_name == "compare_products":
        return len(result.get("products") or [])
    if tool_name in {"get_product", "get_order"}:
        key = "product" if tool_name == "get_product" else "order"
        return int(bool(result.get(key)))
    if tool_name in {"check_return_eligibility", "create_return_request"} | _WRITE_TRANSITION_TOOLS:
        return int(any(key not in {"ok"} for key in result))
    return 0


def evidence_from_tool_call(
    tool_name: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
    call_id: str,
    *,
    start_index: int = 1,
) -> list[dict[str, Any]]:
    """Normalize one successful tool result into stable, atomic evidence rows."""
    if tool_name not in CONVERTER_TOOLS or not isinstance(result, dict) or not result.get("ok", False):
        return []
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(source_id: str, kind: str, field: str, value: Any, *, text: str | None = None,
            updated_at: str | None = None) -> None:
        if value is None:
            return
        key = (source_id, field, _text(value))
        if key in seen:
            return
        seen.add(key)
        rows.append({
            "evidence_id": f"E{start_index + len(rows)}", "source_id": source_id,
            "tool_call_id": call_id, "tool_name": tool_name, "kind": kind,
            "field": field, "value": value, "text": text if text is not None else _text(value),
            "confidence": 1.0, "updated_at": updated_at,
        })

    def add_product(product: dict[str, Any], *, prefix: str = "product") -> None:
        source_id = _source("product", product, f"tool:{call_id}")
        for field in ("product_id", "title", "category", "price", "inventory", "discontinued", "updated_at"):
            if field in product:
                add(source_id, f"{prefix}_field", f"product.{field}", product.get(field),
                    updated_at=product.get("updated_at"))

    def add_order(order: dict[str, Any]) -> None:
        source_id = _source("order", order, f"tool:{call_id}")
        for field in ("order_id", "product_id", "status", "ordered_at", "delivered_at",
                      "opened", "quality_issue", "inventory_status", "return_status", "version"):
            if field in order:
                add(source_id, "order_field", f"order.{field}", order.get(field))

    if tool_name == "search_catalog":
        for item in result.get("items") or []:
            if isinstance(item, dict):
                add_product(item, prefix="catalog_candidate")
    elif tool_name == "get_product":
        product = result.get("product") or {}
        if isinstance(product, dict):
            add_product(product)
            source_id = _source("product", product, f"tool:{call_id}")
            for index, passage in enumerate(result.get("evidence") or [], 1):
                add(source_id, "product_passage", f"product.evidence.{index}", passage, text=str(passage))
    elif tool_name == "compare_products":
        for wrapped in result.get("products") or []:
            product = wrapped.get("product") or {} if isinstance(wrapped, dict) else {}
            if isinstance(product, dict):
                add_product(product, prefix="comparison_field")
                source_id = _source("product", product, f"tool:{call_id}")
                for index, passage in enumerate(wrapped.get("evidence") or [], 1):
                    add(source_id, "product_passage", f"product.evidence.{index}", passage, text=str(passage))
    elif tool_name == "get_policy":
        canonical = arguments.get("policy_type")
        for policy in result.get("policies") or []:
            if isinstance(policy, dict):
                source_id = _source("policy", policy, f"tool:{call_id}")
                add(source_id, "policy_field", "policy.category", canonical)
                add(source_id, "policy_field", "policy.title", policy.get("title"))
                add(source_id, "policy_clause", "policy.text", policy.get("text"), text=policy.get("text"),
                    updated_at=policy.get("updated_at"))
    elif tool_name == "get_order":
        order = result.get("order") or {}
        if isinstance(order, dict):
            add_order(order)
    elif tool_name == "check_return_eligibility":
        order = result.get("order") or {}
        source_id = _source("order", order if isinstance(order, dict) else {},
                            f"order:{arguments.get('order_id', call_id)}")
        for field in ("eligible", "reason", "days_since_delivery"):
            if field in result:
                add(source_id, "return_eligibility", f"return.{field}", result.get(field))
        if isinstance(order, dict):
            add_order(order)
    elif tool_name == "create_return_request":
        source_id = f"order:{result.get('order_id') or arguments.get('order_id') or call_id}"
        for field in ("changed", "order_id", "return_status", "request_id",
                      "idempotent_replay", "status"):
            if field in result:
                add(source_id, "return_transition", f"return.{field}", result.get(field))
    elif tool_name in _WRITE_TRANSITION_TOOLS:
        source_id = (
            f"order:{result.get('order_id') or arguments.get('order_id') or call_id}"
            if tool_name != "modify_user_address"
            else f"user:{result.get('user_id') or arguments.get('user_id') or call_id}"
        )
        kind = "user_transition" if tool_name == "modify_user_address" else "order_transition"
        for field in (
            "changed", "order_id", "user_id", "status", "return_status", "request_id",
            "idempotent_replay", "cancel_reason", "shipping_address", "item_ids",
            "product_id", "payment_method_id", "exchange_status", "address",
        ):
            if field in result:
                add(source_id, kind, f"{kind.split('_')[0]}.{field}", result.get(field))
    return rows


def convert_tool_call_to_evidence(tool_name: str, arguments: dict[str, Any], result: dict[str, Any],
                                  call_id: str, *, start_index: int = 1) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Return evidence plus an auditable conversion span for evidence-bearing calls."""
    if tool_name not in EVIDENCE_BEARING_TOOLS:
        return [], None
    if tool_name not in CONVERTER_TOOLS:
        return [], {"tool_call_id": call_id, "tool_name": tool_name, "status": "converter_missing",
                    "evidence_ids": [], "source_item_count": 0, "evidence_item_count": 0}
    source_count = _source_item_count(tool_name, result) if isinstance(result, dict) else 0
    rows = evidence_from_tool_call(tool_name, arguments, result, call_id, start_index=start_index)
    if not isinstance(result, dict) or not result.get("ok", False):
        status = "tool_failed"
    elif source_count == 0:
        status = "valid_empty"
    else:
        status = "converted"
    return rows, {
        "tool_call_id": call_id, "tool_name": tool_name, "status": status,
        "evidence_ids": [row["evidence_id"] for row in rows],
        "source_item_count": source_count, "evidence_item_count": len(rows),
    }


def render_evidence_ledger(ledger: list[dict[str, Any]], *, max_chars: int = 8000) -> str:
    lines = []
    for row in ledger:
        text = str(row.get("text", "")).replace("\n", " ")
        lines.append(f"[{row.get('evidence_id')}] source={row.get('source_id')} "
                     f"field={row.get('field')} value={_text(row.get('value'))} text={text}")
    rendered = "\n".join(lines)
    return rendered if len(rendered) <= max_chars else rendered[:max_chars] + "…[evidence truncated]"


def freshness_from_ledger(answer: str, ledger: list[dict[str, Any]]) -> dict[str, Any]:
    products: dict[str, dict[str, Any]] = {}
    policies: dict[str, dict[str, Any]] = {}
    for row in ledger:
        source_id = str(row.get("source_id", ""))
        target = policies if source_id.startswith("policy:") else products if source_id.startswith("product:") else None
        if target is None:
            continue
        item = target.setdefault(source_id, {"source_id": source_id, "default_updated_at": None})
        if row.get("updated_at"):
            item["default_updated_at"] = row["updated_at"]
    return freshness.assess({"products": list(products.values()), "policies": list(policies.values())},
                            intent="evidence_grounded_agent", answer=answer)


def extract_user_context(messages: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Extract only user-provided constraints/identifiers, never claimed business facts."""
    texts = [str(row.get("content", "")) for row in (messages or []) if row.get("role") == "user"]
    joined = "\n".join(texts)
    budgets: list[float] = []
    for pattern in (
        r"(?:预算|上限)[^0-9]{0,8}([0-9]+(?:\.[0-9]+)?)\s*元?",
        r"(?:不超过|不高于|以内|最多)[^0-9]{0,8}([0-9]+(?:\.[0-9]+)?)\s*元",
        r"(?:只能花|只能用|最多花)[^0-9]{0,8}([0-9]+(?:\.[0-9]+)?)\s*元",
        r"([0-9]+(?:\.[0-9]+)?)\s*元(?:以内|以下|封顶)",
    ):
        budgets.extend(float(value) for value in re.findall(pattern, joined, re.I))
    identifiers = sorted(set(re.findall(r"\b(?:P[0-9]{3,5}|O[0-9]{6})\b", joined, re.I)))
    codes = sorted(set(match.group(0) for match in re.finditer(r"(?<![0-9])[0-9]{6}(?![0-9])", joined)
                       if re.search(r"验证|校验|code", joined[max(0, match.start()-12):match.end()+12], re.I)))
    confirmations = [text for text in texts if re.search(r"(?:确认|同意|拒绝|不同意|confirm|yes|no)", text, re.I)]
    return {"budgets": sorted(set(budgets)), "identifiers": identifiers,
            "verification_codes": codes, "confirmations": confirmations}


def _sentences(answer: str) -> list[str]:
    normalized = re.sub(r"([。！？.!?])(\s*(?:\[E[1-9][0-9]*\])+)", r"\2\1", answer)
    return split_sentences(normalized) or ([answer.strip()] if answer.strip() else [])


def _normalized_dates(text: str) -> list[str]:
    values = _ISO_DATE.findall(text) + _ZH_DATE.findall(text) + _EN_DATE.findall(text)
    normalized = []
    months = {name.lower(): index for index, names in enumerate((
        ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
        ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
        ("sep", "september"), ("oct", "october"), ("nov", "november"), ("dec", "december")), 1)
              for name in names}
    for value in values:
        if "年" in value:
            y, m, d = map(int, re.findall(r"[0-9]+", value))
            normalized.append(f"{y:04d}-{m:02d}-{d:02d}")
        elif re.match(r"[A-Za-z]", value):
            name, day, year = re.match(r"([A-Za-z]+)\s+([0-9]+),\s*([0-9]+)", value).groups()
            normalized.append(f"{int(year):04d}-{months[name.lower()]:02d}-{int(day):02d}")
        else:
            normalized.append(value)
    return normalized


def _facts(text: str) -> list[str]:
    dates = _normalized_dates(text)
    masked = _ISO_DATE.sub(" ", _ZH_DATE.sub(" ", _EN_DATE.sub(" ", text)))
    facts = _ID_FACT.findall(masked) + dates + _NUMERIC_FACT.findall(masked)
    lowered = masked.lower()
    terms = {
        "pending", "processed", "delivered", "cancelled", "requested", "out_of_stock",
        "已交付", "已送达", "待处理", "处理中", "已取消", "缺货", "有货", "可退货", "不可退货",
        "符合退货条件", "不符合退货条件", "退货申请已提交", "已成功提交", "已申请退货", "已停产", "未停产",
    }
    if re.search(r"(?:库存|存货|缺货|有货)[^。！？.!?]{0,12}\bavailable\b|\bavailable\b[^。！？.!?]{0,12}(?:库存|存货)", lowered):
        terms.add("available")
    occupied: list[tuple[int, int]] = []
    for term in sorted(terms, key=len, reverse=True):
        for match in re.finditer(re.escape(term.lower()), lowered):
            span = match.span()
            if any(not (span[1] <= used[0] or span[0] >= used[1]) for used in occupied):
                continue
            facts.append(term)
            occupied.append(span)
    return list(dict.fromkeys(x.strip() for x in facts if x.strip()))


def _corpus(rows: Iterable[dict[str, Any]]) -> str:
    return "\n".join(f"{row.get('source_id', '')} {row.get('field', '')} {_text(row.get('value'))} {row.get('text', '')}"
                     for row in rows).lower()


def _fact_supported(fact: str, rows: Iterable[dict[str, Any]], user_context: dict[str, Any] | None = None) -> bool:
    rows = list(rows)
    fact_l = re.sub(r"\s+", "", fact.lower())
    corpus = re.sub(r"\s+", "", _corpus(rows))
    if fact_l in corpus:
        return True
    if fact in _normalized_dates(corpus):
        return True
    amount = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)元", fact_l)
    if amount:
        expected = float(amount.group(1))
        if expected in (user_context or {}).get("budgets", []):
            return True
        return any(row.get("field") == "product.price" and isinstance(row.get("value"), (int, float))
                   and float(row["value"]) == expected for row in rows)
    days = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(?:天|日)", fact_l)
    if days:
        expected = float(days.group(1))
        if any(row.get("field") == "return.days_since_delivery" and isinstance(row.get("value"), (int, float))
               and float(row["value"]) == expected for row in rows):
            return True
    if fact.upper() in {x.upper() for x in (user_context or {}).get("identifiers", [])}:
        return True
    aliases = {
        "已送达": "delivered", "已交付": "delivered", "待处理": "pending", "处理中": "processed",
        "已取消": "cancelled", "缺货": "out_of_stock", "有货": "available", "可退货": "true",
        "符合退货条件": "true", "不可退货": "false", "不符合退货条件": "false",
        "退货申请已提交": "requested", "已成功提交": "requested", "已申请退货": "requested",
    }
    return aliases.get(fact_l, "\0") in corpus


def _explicit_contradiction(sentence: str, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    lowered = sentence.lower()
    price_claims = []
    for match in re.finditer(r"([0-9]+(?:\.[0-9]+)?)\s*元", sentence):
        nearby = sentence[max(0, match.start() - 12):match.end() + 4]
        if re.search(r"预算|上限|不超过|以内|最多|封顶", nearby):
            continue
        price_claims.append(float(match.group(1)))
    prices = [float(row["value"]) for row in rows if row.get("field") == "product.price"
              and isinstance(row.get("value"), (int, float))]
    for claimed in price_claims:
        if prices and claimed not in prices:
            return {"sentence": sentence, "claim": f"{claimed:g}元", "field": "product.price",
                    "evidence_values": prices, "reason": "explicit_contradiction"}
    day_claims = [float(value) for value in re.findall(r"([0-9]+(?:\.[0-9]+)?)\s*(?:天|日)",
                                                       _ZH_DATE.sub("", sentence))]
    elapsed_days = [float(row["value"]) for row in rows if row.get("field") == "return.days_since_delivery"
                    and isinstance(row.get("value"), (int, float))]
    for claimed in day_claims:
        if elapsed_days and claimed not in elapsed_days:
            return {"sentence": sentence, "claim": f"{claimed:g}天", "field": "return.days_since_delivery",
                    "evidence_values": elapsed_days, "reason": "explicit_contradiction"}
    claimed_dates = _normalized_dates(sentence)
    date_rows = [str(row.get("value")) for row in rows if str(row.get("field", "")).endswith("_at")]
    normalized_evidence_dates = _normalized_dates(" ".join(date_rows))
    for claimed in claimed_dates:
        if normalized_evidence_dates and claimed not in normalized_evidence_dates:
            return {"sentence": sentence, "claim": claimed, "field": "structured_date",
                    "evidence_values": normalized_evidence_dates, "reason": "explicit_contradiction"}
    checks = (("已停产", "product.discontinued", True), ("未停产", "product.discontinued", False),
              ("不符合退货条件", "return.eligible", False), ("不可退货", "return.eligible", False),
              ("符合退货条件", "return.eligible", True), ("可退货", "return.eligible", True))
    for phrase, field, claimed in checks:
        if phrase not in lowered or (phrase == "符合退货条件" and "不符合退货条件" in lowered) or (
                phrase == "可退货" and "不可退货" in lowered):
            continue
        values = [row.get("value") for row in rows if row.get("field") == field]
        if values and claimed not in values:
            return {"sentence": sentence, "claim": phrase, "field": field,
                    "evidence_values": values, "reason": "explicit_contradiction"}
    aliases = {
        "order.status": {"pending": ("pending", "待处理"), "processed": ("processed", "处理中", "已处理"),
                         "delivered": ("delivered", "已送达", "已交付"), "cancelled": ("cancelled", "已取消")},
        "order.inventory_status": {"available": ("有货",), "out_of_stock": ("out_of_stock", "缺货")},
        "return.return_status": {"requested": ("requested", "退货申请已提交", "已成功提交", "已申请退货"),
                                 "already_requested": ("already_requested", "已提交过退货")},
    }
    for field, vocabulary in aliases.items():
        claimed = [canonical for canonical, phrases in vocabulary.items() if any(p in lowered for p in phrases)]
        evidence = [str(row.get("value")).lower() for row in rows if row.get("field") == field]
        if claimed and evidence and not any(value in evidence for value in claimed):
            return {"sentence": sentence, "claim": claimed[0], "field": field,
                    "evidence_values": evidence, "reason": "explicit_contradiction"}
    durations = re.findall(r"[0-9]+(?:\.[0-9]+)?\s*(?:个工作日|工作日|天|日|小时|分钟)", sentence)
    policy_rows = [row for row in rows if str(row.get("field", "")).startswith("policy.")]
    for duration in durations:
        if policy_rows and not _fact_supported(duration, policy_rows):
            evidence_values = re.findall(r"[0-9]+(?:\.[0-9]+)?\s*(?:个工作日|工作日|天|日|小时|分钟)",
                                         _corpus(policy_rows))
            if evidence_values:
                return {"sentence": sentence, "claim": duration, "field": "policy.text",
                        "evidence_values": evidence_values, "reason": "explicit_contradiction"}
    return None


def _value_mentioned(value: Any, answer: str) -> bool:
    rendered, lowered = _text(value).lower(), answer.lower()
    if rendered and rendered in lowered:
        return True
    aliases = {
        "delivered": ("已送达", "已交付", "已签收"), "pending": ("待处理", "待付款"),
        "processed": ("处理中", "已处理"), "cancelled": ("已取消",),
        "requested": ("已提交退货", "退货申请已提交", "已成功提交", "已申请退货"),
        "true": ("符合退货条件", "可以退货", "可退货"),
        "false": ("不符合退货条件", "不能退货", "不可退货"),
    }
    return any(alias in lowered for alias in aliases.get(rendered, ()))


def _english_overlap(left: str, right: str) -> float:
    tokens = lambda value: set(re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", value.lower()))
    a, b = tokens(left), tokens(right)
    return len(a & b) / len(a) if a else 0.0


def _row_supports_sentence(sentence: str, row: dict[str, Any]) -> bool:
    clean = EVIDENCE_CITATION.sub("", sentence)
    if _value_mentioned(row.get("value"), clean):
        return True
    facts = _facts(clean)
    if facts and all(_fact_supported(fact, [row]) for fact in facts):
        return True
    evidence_text = str(row.get("text", ""))
    compact = re.sub(r"\s+", "", evidence_text.lower())
    if any(sequence[index:index + 4] in compact for sequence in re.findall(r"[\u4e00-\u9fff]{4,}", clean)
           for index in range(len(sequence) - 3)):
        return True
    return _english_overlap(clean, f"{row.get('field', '')} {row.get('value', '')} {evidence_text}") >= 0.5


def _required_matches(key: str, ledger: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if "*" in key:
        return [row for row in ledger if fnmatchcase(str(row.get("field", "")), key)]
    return [row for row in ledger if row.get("field") == key]


def verify_answer(answer: str, ledger: list[dict[str, Any]], *, expectations: dict[str, Any] | None = None,
                  require_citations: bool = False, user_messages: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Separate hard factual contradictions from diagnostic evidence quality."""
    expectations = expectations or {}
    user_context = extract_user_context(user_messages)
    by_id = {str(row.get("evidence_id")): row for row in ledger}
    invalid: list[str] = []
    unsupported: list[dict[str, Any]] = []
    contradicted: list[dict[str, Any]] = []
    citation_failures: list[dict[str, Any]] = []
    citation_oppositions: list[dict[str, Any]] = []
    cited_any = False
    for sentence in _sentences(answer):
        ids = [f"E{x}" for x in EVIDENCE_CITATION.findall(sentence)]
        cited_any = cited_any or bool(ids)
        missing = [eid for eid in ids if eid not in by_id]
        invalid.extend(missing)
        cited_rows = [by_id[eid] for eid in ids if eid in by_id]
        contradiction = _explicit_contradiction(sentence, ledger)
        if contradiction:
            contradicted.append(contradiction)
        cited_contradiction = _explicit_contradiction(sentence, cited_rows) if cited_rows else None
        if cited_contradiction:
            citation_oppositions.append({**cited_contradiction, "evidence_ids": ids,
                                         "reason": "cited_evidence_supports_opposite"})
        facts = _facts(sentence)
        for fact in facts:
            if not _fact_supported(fact, ledger, user_context):
                unsupported.append({"sentence": sentence, "claim": fact, "reason": "not_in_evidence"})
            if ids and not _fact_supported(fact, cited_rows, user_context):
                citation_failures.append({"sentence": sentence, "claim": fact,
                                          "evidence_ids": ids, "reason": "citation_does_not_support_claim"})
        if ids and not facts and cited_rows and not any(_row_supports_sentence(sentence, row) for row in cited_rows):
            citation_failures.append({"sentence": sentence, "claim": None, "evidence_ids": ids,
                                      "reason": "citation_has_no_lexical_support"})
    required = list(expectations.get("required_fact_keys", []))
    omitted: list[str] = []
    covered = 0
    sentences = _sentences(answer)
    for key in required:
        matching = _required_matches(key, ledger)
        ok = any(_value_mentioned(row.get("value"), answer) or any(
            f"[{row.get('evidence_id')}]" in sentence and _row_supports_sentence(sentence, row)
            for sentence in sentences) for row in matching)
        if ok:
            covered += 1
        else:
            omitted.append(key)
    for field, values in expectations.get("forbidden_values", {}).items():
        for value in values:
            if _text(value).lower() in answer.lower():
                contradicted.append({"sentence": answer, "claim": _text(value), "field": field,
                                     "reason": "forbidden_expected_value"})
    applicable = bool(ledger or required or expectations.get("forbidden_values"))
    hard_pass = not invalid and not contradicted and not citation_oppositions
    citation_format = ([{"reason": "citation_range_not_allowed", "text": match.group(0)}
                        for match in EVIDENCE_RANGE.finditer(answer)])
    if require_citations and applicable and not cited_any:
        citation_format.append({"reason": "citation_missing", "sentence": answer})
    citation_ok = not invalid and not citation_failures and not citation_format
    coverage = covered / len(required) if required else None
    diagnostics = [*citation_format, *citation_failures]
    return {
        "applicable": applicable, "hard_verification_pass": hard_pass,
        "answer_fact_pass": hard_pass, "citation_binding_pass": citation_ok,
        "required_evidence_coverage": coverage, "unsupported_high_risk_claims": unsupported,
        "contradicted_claims": contradicted, "omitted_required_facts": omitted,
        "invalid_citations": sorted(set(invalid)), "citation_failures": citation_failures,
        "citation_oppositions": citation_oppositions, "citation_diagnostics": diagnostics,
        "cited_evidence_ids": sorted({f"E{x}" for x in EVIDENCE_CITATION.findall(answer)}),
        "require_citations": require_citations, "user_context": user_context,
        "freshness": freshness_from_ledger(answer, ledger),
    }
