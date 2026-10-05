"""Loss-aware compaction for multi-turn retail tool history.

The compactor removes repeated database payload while preserving identifiers,
state, prices, eligibility, errors, and write results needed for the next
decision. It never mutates the stored trajectory; policies compact only the
provider-facing copy.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any


CANONICAL_COMPACTION_ENV = "ERAG_CONTEXT_COMPACTION"
LEGACY_COMPACTION_ENV = "ARAG_CONTEXT_COMPACTION"


def context_compaction_enabled(*, default: bool) -> bool:
    """Read the canonical switch with a backwards-compatible legacy fallback.

    The callers retain their historical defaults: NativeToolPolicy defaults to
    enabled while the tau3 adapter defaults to disabled.  An explicit canonical
    value always wins over the older ARAG-prefixed variable.
    """
    raw = os.getenv(CANONICAL_COMPACTION_ENV)
    if raw is None:
        raw = os.getenv(LEGACY_COMPACTION_ENV)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


_ORDER_FIELDS = (
    "order_id", "user_id", "status", "return_status", "delivered_at",
    "quality_issue", "opened", "cancel_reason", "return_items",
    "return_payment_method_id", "request_id", "changed", "idempotent_replay",
    # The order-item write tools require the current items and a payment
    # method; without them the compacted view is not decision-equivalent.
    "product_id", "item_ids", "payment_method_id", "exchange_status",
)
_ITEM_FIELDS = ("item_id", "product_id", "name", "title", "price", "options", "available")
_PRODUCT_FIELDS = ("product_id", "item_id", "title", "name", "category", "price", "inventory", "options", "available")
# Present only when the opt-in attribute view is enabled; kept so that view
# actually reaches the model instead of being compacted away.
_ATTRIBUTE_VIEW = ("attributes",)


@dataclass(frozen=True)
class CompactionStats:
    raw_chars: int
    compact_chars: int
    raw_tool_chars: int
    compact_tool_chars: int
    tool_messages: int

    @property
    def reduction_ratio(self) -> float:
        return 0.0 if not self.raw_chars else 1.0 - (self.compact_chars / self.raw_chars)

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_chars": self.raw_chars,
            "compact_chars": self.compact_chars,
            "raw_tool_chars": self.raw_tool_chars,
            "compact_tool_chars": self.compact_tool_chars,
            "tool_messages": self.tool_messages,
            "reduction_ratio": self.reduction_ratio,
        }


def _pick(value: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {field: value[field] for field in fields if field in value and value[field] is not None}


def _items(value: Any, *, limit: int = 12) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [_pick(item, _ITEM_FIELDS) for item in value[:limit] if isinstance(item, dict)]


def _compact_order(order: Any) -> dict[str, Any]:
    compact = _pick(order, _ORDER_FIELDS)
    if isinstance(order, dict):
        if "items" in order:
            compact["items"] = _items(order.get("items"))
        payments = order.get("payment_history")
        if isinstance(payments, list):
            compact["payment_history"] = [
                _pick(row, ("transaction_type", "amount", "payment_method_id"))
                for row in payments[-4:] if isinstance(row, dict)
            ]
        fulfillments = order.get("fulfillments")
        if isinstance(fulfillments, list):
            compact["fulfillments"] = [
                _pick(row, ("tracking_id", "item_ids"))
                for row in fulfillments if isinstance(row, dict)
            ]
    return compact


def _compact_product(value: Any) -> dict[str, Any]:
    compact = _pick(value, _PRODUCT_FIELDS)
    if isinstance(value, dict) and isinstance(value.get("variants"), dict):
        compact["variants"] = {
            key: _pick(variant, _ITEM_FIELDS)
            for key, variant in list(value["variants"].items())[:24]
            if isinstance(variant, dict)
        }
    return compact


def compact_tool_result(name: str | None, result: Any) -> Any:
    """Return a smaller decision-equivalent representation of one tool result."""
    if not isinstance(result, dict):
        return result
    base = _pick(result, (
        "ok", "error", "changed", "eligible", "reason", "days_since_delivery",
        "request_id", "status", "return_status", "handoff_id", "idempotent_replay",
    ))
    if name == "search_catalog":
        base["items"] = [_pick(item, _PRODUCT_FIELDS + _ATTRIBUTE_VIEW) for item in (result.get("items") or [])[:12]]
    elif name == "get_product":
        base["product"] = _compact_product(result.get("product") or result)
        evidence = result.get("evidence")
        if isinstance(evidence, list):
            base["evidence"] = [str(text)[:600] for text in evidence[:3]]
        base.update(_pick(result, _ATTRIBUTE_VIEW))
    elif name == "compare_products":
        base["products"] = [
            {"ok": row.get("ok"), "product": _compact_product(row.get("product") or row),
             **_pick(row, _ATTRIBUTE_VIEW)}
            for row in (result.get("products") or [])[:12] if isinstance(row, dict)
        ]
    elif name == "get_policy":
        base["policies"] = [
            {**_pick(row, ("doc_id", "title")), "text": str(row.get("text", ""))[:800]}
            for row in (result.get("policies") or [])[:3] if isinstance(row, dict)
        ]
    elif name in {"get_order", "check_return_eligibility"}:
        base["order"] = _compact_order(result.get("order"))
    elif name == "get_order_details":
        base.update(_compact_order(result))
    elif name == "get_product_details":
        base.update(_compact_product(result))
    elif name == "get_user_details":
        base.update(_pick(result, ("user_id", "name", "address", "email", "payment_methods", "orders")))
    elif name in {"create_return_request", "return_delivered_order_items",
                  "exchange_delivered_order_items", "modify_pending_order_items",
                  "modify_pending_order_address", "modify_pending_order_payment",
                  "modify_user_address", "cancel_pending_order"}:
        base.update(_compact_order(result))
    else:
        for key in ("order", "product", "items", "policies"):
            if key in result and key not in base:
                base[key] = result[key]
    return base or result


def _entity_key(name: str | None, result: Any) -> tuple[str, str] | None:
    if not isinstance(result, dict):
        return None
    if name in {"get_order", "check_return_eligibility"}:
        value = (result.get("order") or {}).get("order_id")
    elif name == "get_order_details":
        value = result.get("order_id")
    elif name == "get_user_details":
        value = result.get("user_id")
    elif name == "get_product":
        value = (result.get("product") or {}).get("product_id")
    elif name == "get_product_details":
        value = result.get("product_id")
    else:
        value = None
    return (str(name), str(value)) if value else None


def compact_history(history: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], CompactionStats]:
    parsed_results: list[Any] = [None] * len(history)
    entity_keys: list[tuple[str, str] | None] = [None] * len(history)
    last_entity_index: dict[tuple[str, str], int] = {}
    for index, entry in enumerate(history):
        if entry.get("role") != "tool":
            continue
        result = entry.get("result")
        if result is None:
            try:
                result = json.loads(str(entry.get("content", "")))
            except (json.JSONDecodeError, TypeError):
                result = entry.get("content", "")
        parsed_results[index] = result
        key = _entity_key(entry.get("name"), result)
        entity_keys[index] = key
        if key is not None:
            last_entity_index[key] = index

    compacted: list[dict[str, Any]] = []
    raw_chars = compact_chars = raw_tool_chars = compact_tool_chars = tool_messages = 0
    for entry in history:
        copied = dict(entry)
        raw = str(entry.get("content", ""))
        raw_chars += len(raw)
        if entry.get("role") == "tool":
            tool_messages += 1
            raw_tool_chars += len(raw)
            result = parsed_results[len(compacted)]
            key = entity_keys[len(compacted)]
            if key is not None and last_entity_index.get(key) != len(compacted):
                compact = {
                    "ok": result.get("ok", True) if isinstance(result, dict) else True,
                    "superseded_by_later_read": True,
                    key[0].removeprefix("get_").removesuffix("_details") + "_id": key[1],
                }
            else:
                compact = compact_tool_result(entry.get("name"), result)
            copied["result"] = compact
            copied["content"] = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
            compact_tool_chars += len(copied["content"])
        compact_chars += len(str(copied.get("content", "")))
        compacted.append(copied)
    return compacted, CompactionStats(
        raw_chars, compact_chars, raw_tool_chars, compact_tool_chars, tool_messages)
