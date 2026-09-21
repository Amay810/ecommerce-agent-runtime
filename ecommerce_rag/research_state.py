"""Derived, policy-visible research state for multi-step evidence gathering.

The state is a projection of the existing conversation history and evidence
ledger.  It is intentionally not a second business-state store and it never
reads ``TaskSpec`` expectations, gold documents, or answer keys.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .evidence import extract_user_context


RESEARCH_TOOLS = frozenset(
    {"search_catalog", "get_product", "compare_products", "get_policy", "get_order", "check_return_eligibility"}
)
PRODUCT_TOOLS = frozenset({"search_catalog", "get_product", "compare_products"})
_POLICY_TERMS = ("政策", "规则", "条款", "退货", "退换", "保修", "发货", "物流", "退款", "发票", "policy")
_PRODUCT_TERMS = (
    "商品", "产品", "耳机", "键盘", "榨汁", "杯", "机器人", "吸尘", "显示器", "鼠标", "背包",
    "灯", "咖啡机", "电池", "手表", "推荐", "product", "headphone", "keyboard",
)
_COMPARE_TERMS = ("比较", "对比", "哪个", "哪款", "适合", "compare", "versus", "vs")
_ORDER_TERMS = ("订单", "订单号", "物流状态", "order")
_DETAIL_TERMS = (
    "价格", "预算", "库存", "现货", "续航", "防水", "重量", "连接", "容量", "清洗", "保温",
    "兼容", "降噪", "尺寸", "属性", "参数", "冰", "洗碗机", "拆封",
    "price", "battery", "waterproof",
)


@dataclass(frozen=True)
class ResearchState:
    """The compact decision context exposed to an evidence-aware policy."""

    user_constraints: dict[str, Any] = field(default_factory=dict)
    obtained_facts: list[dict[str, Any]] = field(default_factory=list)
    missing_evidence: list[dict[str, Any]] = field(default_factory=list)
    executed_queries: list[dict[str, Any]] = field(default_factory=list)
    retrieval_calls: int = 0
    remaining_budget: int | None = None
    status: str = "no_evidence"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _user_messages(history: list[dict[str, Any]]) -> list[str]:
    return [str(row.get("content", "")) for row in history if row.get("role") == "user"]


def _source_ids(ledger: list[dict[str, Any]]) -> set[str]:
    return {str(row.get("source_id")) for row in ledger if row.get("source_id")}


def _source_types(ledger: list[dict[str, Any]]) -> set[str]:
    types: set[str] = set()
    for source_id in _source_ids(ledger):
        if source_id.startswith("product:"):
            types.add("product")
        elif source_id.startswith("policy:"):
            types.add("policy")
        elif source_id.startswith("order:"):
            types.add("order")
    return types


def _safe_query_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Keep retrieval parameters while removing identity/authorization values."""
    return {
        key: copy.deepcopy(value)
        for key, value in arguments.items()
        if key not in {"user_id", "verification_code", "confirmed"}
    }


def _result_summary(result: dict[str, Any]) -> dict[str, Any]:
    sources: set[str] = set()
    for item in result.get("items") or result.get("policies") or []:
        if isinstance(item, dict) and item.get("doc_id"):
            sources.add(str(item["doc_id"]))
    product = result.get("product")
    if isinstance(product, dict):
        if product.get("doc_id"):
            sources.add(str(product["doc_id"]))
        elif product.get("product_id"):
            sources.add(f"product:{product['product_id']}")
    for wrapped in result.get("products") or []:
        if isinstance(wrapped, dict):
            product = wrapped.get("product") or {}
            if product.get("doc_id"):
                sources.add(str(product["doc_id"]))
            elif product.get("product_id"):
                sources.add(f"product:{product['product_id']}")
    return {
        "ok": bool(result.get("ok")),
        "source_ids": sorted(sources),
        "result_count": sum(len(result.get(key) or []) for key in ("items", "policies", "products"))
        + int(bool(result.get("product")))
        + int(bool(result.get("order"))),
        "error": result.get("error"),
    }


def _executed_queries(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    queries: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for entry in history:
        if entry.get("role") == "assistant" and entry.get("action") == "tool_call":
            name = entry.get("tool_name")
            if name in RESEARCH_TOOLS:
                item = {
                    "tool_name": name,
                    "arguments": _safe_query_arguments(entry.get("arguments") or {}),
                    "outcome": None,
                }
                queries.append(item)
                pending.append(item)
        elif entry.get("role") == "tool" and entry.get("name") in RESEARCH_TOOLS:
            result = entry.get("result") or {}
            match = next(
                (item for item in reversed(pending) if item["tool_name"] == entry.get("name") and item["outcome"] is None),
                None,
            )
            if match is None:
                match = {
                    "tool_name": entry.get("name"),
                    "arguments": {},
                    "outcome": None,
                }
                queries.append(match)
            match["outcome"] = _result_summary(result)
    return queries


def _requested_facets(text: str) -> list[str]:
    lowered = text.lower()
    facets: list[str] = []
    if any(term in lowered for term in _PRODUCT_TERMS) or re.search(r"\bP\d{3,5}\b", text, re.I):
        facets.append("product")
    if any(term in lowered for term in _COMPARE_TERMS):
        facets.append("comparison")
    if any(term in lowered for term in _POLICY_TERMS):
        facets.append("policy")
    if any(term in lowered for term in _ORDER_TERMS) or re.search(r"\bO\d{6}\b", text, re.I):
        facets.append("order")
    if any(term in lowered for term in _DETAIL_TERMS):
        facets.append("product_details")
    return list(dict.fromkeys(facets))


def _missing_evidence(text: str, ledger: list[dict[str, Any]], queries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lowered = text.lower()
    facets = _requested_facets(text)
    sources = _source_ids(ledger)
    source_types = _source_types(ledger)
    successful = {
        str(item.get("tool_name"))
        for item in queries
        if (item.get("outcome") or {}).get("ok")
    }
    gaps: list[dict[str, Any]] = []

    if "product" in facets and not source_types.intersection({"product"}):
        gaps.append({
            "kind": "product_candidates",
            "description": "尚未取得支持商品结论的商品资料",
            "source_type": "product",
            "next_tool": "search_catalog",
        })
    if "comparison" in facets:
        product_sources = {source for source in sources if source.startswith("product:")}
        detailed_sources = {
            str(row.get("source_id"))
            for row in ledger
            if str(row.get("field", "")).startswith("product.evidence.")
        }
        if len(product_sources) < 2 or len(product_sources & detailed_sources) < 2:
            gaps.append({
                "kind": "comparison_coverage",
                "description": "比较所需的两个独立商品来源尚未齐全",
                "source_type": "product",
                "next_tool": "compare_products" if len(product_sources) >= 2 else "search_catalog",
            })
    if "product_details" in facets:
        detail_rows = [row for row in ledger if str(row.get("field", "")).startswith("product.evidence.")]
        if not detail_rows:
            gaps.append({
                "kind": "product_details",
                "description": "尚未取得支持商品属性/参数结论的详情证据",
                "source_type": "product",
                "next_tool": "get_product",
            })
    if "policy" in facets and "policy" not in source_types:
        gaps.append({
            "kind": "policy_clause",
            "description": "尚未取得支持政策结论的正式政策条款",
            "source_type": "policy",
            "next_tool": "get_policy",
        })
    if "order" in facets and "order" not in source_types:
        gaps.append({
            "kind": "order_record",
            "description": "尚未取得支持订单状态结论的只读订单记录",
            "source_type": "order",
            "next_tool": "get_order",
        })
    if queries and not any((item.get("outcome") or {}).get("ok") for item in queries):
        gaps.append({
            "kind": "empty_or_failed_query",
            "description": "已执行查询没有返回可用证据，需要改写查询或明确保留未知",
            "source_type": "unknown",
            "next_tool": "search_catalog" if "product" in facets else None,
        })
    if not gaps and not ledger and text.strip():
        gaps.append({
            "kind": "request_evidence",
            "description": "用户问题尚未得到任何可引用的业务证据",
            "source_type": "unknown",
            "next_tool": "search_catalog" if "product" in facets else None,
        })
    return gaps


def derive_research_state(
    history: list[dict[str, Any]],
    evidence_ledger: list[dict[str, Any]],
    *,
    budget: int | None = None,
) -> ResearchState:
    """Derive the next-decision context from visible events only."""
    user_messages = _user_messages(history)
    request = "\n".join(user_messages)
    context = extract_user_context(history)
    context.pop("verification_codes", None)
    constraints = {
        # The raw history remains authoritative, but the compact decision
        # state does not need to repeat an identity secret.
        "request": re.sub(r"(?<![A-Za-z0-9])\d{6}(?!\d)", "[redacted-code]", user_messages[-1]) if user_messages else "",
        "budgets": context.get("budgets", []),
        "identifiers": context.get("identifiers", []),
        "requested_facets": _requested_facets(request),
    }
    facts = []
    for row in evidence_ledger:
        fact = {
            key: copy.deepcopy(row.get(key))
            for key in ("evidence_id", "source_id", "tool_name", "field", "value", "updated_at")
            if row.get(key) is not None
        }
        text = str(row.get("text", ""))
        if text:
            fact["text"] = text[:320] + ("…" if len(text) > 320 else "")
        facts.append(fact)
    queries = _executed_queries(history)
    calls = len(queries)
    remaining = max(0, budget - calls) if budget is not None else None
    gaps = _missing_evidence(request, evidence_ledger, queries)
    if remaining == 0 and gaps:
        status = "budget_exhausted"
    elif gaps:
        status = "needs_more_evidence"
    elif facts:
        status = "ready_to_answer"
    else:
        status = "no_evidence"
    return ResearchState(
        user_constraints=constraints,
        obtained_facts=facts,
        missing_evidence=gaps,
        executed_queries=queries,
        retrieval_calls=calls,
        remaining_budget=remaining,
        status=status,
    )


def render_research_state(state: dict[str, Any] | ResearchState, *, max_chars: int = 12000) -> str:
    payload = state.to_dict() if isinstance(state, ResearchState) else state
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    if len(rendered) > max_chars:
        rendered = rendered[:max_chars] + "\n…[research state truncated]"
    return (
        "<research_state>\n"
        "This is a derived view of user constraints and successful/failed read-only calls. "
        "It contains no answer key or gold source list. Use it to decide whether one more "
        "read-only evidence call is useful. If no gap remains, answer from the cited facts; "
        "if the budget is exhausted, explicitly name the unresolved conclusions instead of guessing.\n"
        f"{rendered}\n"
        "</research_state>"
    )


def budget_exhausted_answer(state: ResearchState) -> str:
    gaps = state.missing_evidence
    if gaps:
        unknown = "；".join(str(item.get("description")) for item in gaps)
        return f"检索预算已用尽，以下结论仍无法确认：{unknown}。我不会据此猜测。"
    return "检索预算已用尽；我只能依据已经取得的证据回答，无法确认的部分不会猜测。"
