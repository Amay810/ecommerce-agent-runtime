"""Typed retail tools with verification and state-transition guardrails."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .domain import ToolCall
from .confirmation import ConfirmationLedger, binding_hash
from .retail_protocol import RETAIL_WRITE_TOOLS
from .tool_schema import validate_arguments
from . import config, orders


READ_TOOLS = {
    "search_catalog",
    "get_product",
    "compare_products",
    "get_policy",
    "get_order",
    "check_return_eligibility",
}
#: Local legacy write + compiler/τ³ write surface + handoff.
WRITE_TOOLS = {"create_return_request", "escalate_to_human"} | set(RETAIL_WRITE_TOOLS)

#: Tools that touch one customer's order/profile and therefore must never run
#: without a verification code the user actually supplied.
IDENTITY_GUARDED_TOOLS = {
    "get_order",
    "check_return_eligibility",
    "create_return_request",
    "cancel_pending_order",
    "modify_pending_order_address",
    "modify_pending_order_items",
    "modify_pending_order_payment",
    "modify_user_address",
    "return_delivered_order_items",
    "exchange_delivered_order_items",
}

CANCEL_REASONS = frozenset({"no longer needed", "ordered by mistake"})

#: ``\d`` matches Unicode decimal digits, so it accepts full-width "１２３４５６"
#: and Arabic-Indic forms. An identity guard must be literal ASCII.
_VERIFICATION_CODE = re.compile(r"[0-9]{6}")

POLICY_CATEGORIES = {
    "return": "退换货",
    "warranty": "售后保修",
    "shipping": "物流",
    "invoice": "发票",
    "refund": "退款",
}
POLICY_ALIASES = {
    **POLICY_CATEGORIES,
    "退换货": "退换货",
    "保修": "售后保修",
    "售后保修": "售后保修",
    "物流": "物流",
    "发票": "发票",
    "退款": "退款",
}


def _parse_json_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw]
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _address_payload(
    address1: str,
    address2: str,
    city: str,
    state: str,
    country: str,
    zip: str,
) -> dict[str, str]:
    return {
        "address1": address1,
        "address2": address2,
        "city": city,
        "state": state,
        "country": country,
        "zip": zip,
    }


class RetailTools:
    def __init__(self, db_path: Path | str, retriever: Any | None = None, today: date | None = None):
        self.db_path = Path(db_path)
        self.retriever = retriever
        configured_today = os.getenv("ERAG_SIMULATED_TODAY", "2026-07-20")
        self.today = today or date.fromisoformat(configured_today)
        self.confirmation_ledger = ConfirmationLedger()
        self._active_call_context: dict[str, Any] | None = None
        self.calls: list[ToolCall] = []
        self.guardrails: list[dict[str, Any]] = []
        self._registry: dict[str, Callable[..., dict]] = {
            "search_catalog": self.search_catalog,
            "get_product": self.get_product,
            "compare_products": self.compare_products,
            "get_policy": self.get_policy,
            "get_order": self.get_order,
            "check_return_eligibility": self.check_return_eligibility,
            "create_return_request": self.create_return_request,
            "cancel_pending_order": self.cancel_pending_order,
            "modify_pending_order_address": self.modify_pending_order_address,
            "modify_pending_order_items": self.modify_pending_order_items,
            "modify_pending_order_payment": self.modify_pending_order_payment,
            "modify_user_address": self.modify_user_address,
            "return_delivered_order_items": self.return_delivered_order_items,
            "exchange_delivered_order_items": self.exchange_delivered_order_items,
            "escalate_to_human": self.escalate_to_human,
        }

    def executable_tool_names(self) -> frozenset[str]:
        return frozenset(self._registry)

    def _block(self, tool: str, reason: str, **extra: Any) -> dict[str, Any]:
        payload = {"tool": tool, "blocked": True, "reason": reason, **extra}
        self.guardrails.append(payload)
        return {"ok": False, "changed": False, "error": reason, **extra}

    def issue_confirmation(
        self,
        *,
        session_id: str,
        user_id: str,
        operation: str,
        arguments: dict[str, Any],
        request_text: str,
    ) -> str:
        """Create a pending request from a trusted interaction layer."""

        return self.confirmation_ledger.issue(
            session_id=session_id,
            user_id=user_id,
            operation=operation,
            parameters=arguments,
            request_text=request_text,
            state_binding=self._confirmation_state_binding(arguments),
        )

    def record_user_confirmation(self, *, session_id: str, response_text: str) -> dict[str, Any]:
        """Record the actual user response; model text cannot call this method."""

        return self.confirmation_ledger.respond(
            session_id=session_id,
            response_text=response_text,
        )

    def authorization_for(
        self,
        *,
        session_id: str,
        user_id: str,
        operation: str,
        arguments: dict[str, Any],
    ) -> str | None:
        return self.confirmation_ledger.authorization_for(
            session_id=session_id,
            user_id=user_id,
            operation=operation,
            parameters=arguments,
            state_binding=self._confirmation_state_binding(arguments),
        )

    def _confirmation_state_binding(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Capture only trusted SQLite state relevant to a confirmed write."""

        order_id = arguments.get("order_id")
        conn = orders.connect(self.db_path)
        try:
            if order_id is not None:
                row = conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
                return {
                    "entity": "order",
                    "key": str(order_id),
                    "row": dict(row) if row is not None else None,
                }
            user_id = arguments.get("user_id")
            row = conn.execute(
                "SELECT user_id, name, address, payment_methods FROM users WHERE user_id=?",
                (user_id,),
            ).fetchone()
            return {
                "entity": "user",
                "key": str(user_id),
                "row": dict(row) if row is not None else None,
            }
        finally:
            conn.close()

    def _expected_order_state(self) -> dict[str, Any] | None:
        binding = (self._active_call_context or {}).get("confirmation_binding")
        if isinstance(binding, dict) and binding.get("entity") == "order":
            row = binding.get("row")
            if isinstance(row, dict):
                return row
        return None

    def _expected_user_state(self) -> dict[str, Any] | None:
        binding = (self._active_call_context or {}).get("confirmation_binding")
        if isinstance(binding, dict) and binding.get("entity") == "user":
            row = binding.get("row")
            if isinstance(row, dict):
                return row
        return None

    def _order_write_guard(self, where: str, params: tuple[Any, ...]) -> tuple[str, tuple[Any, ...]]:
        expected = self._expected_order_state()
        if expected is None:
            return where, params
        return (
            f"{where} AND version=? AND status=?",
            (*params, expected.get("version"), expected.get("status")),
        )

    def _stale_or_block(self, name: str, order_id: str, fallback: str, **extra: Any) -> dict[str, Any]:
        expected = self._expected_order_state()
        if expected is not None:
            current = self._confirmation_state_binding({"order_id": order_id})
            if current != {"entity": "order", "key": str(order_id), "row": expected}:
                return self._block(name, "confirmation_stale", order_id=order_id, **extra)
        return self._block(name, fallback, order_id=order_id, **extra)

    def _stale_user_or_block(self, user_id: str, fallback: str) -> dict[str, Any]:
        expected = self._expected_user_state()
        if expected is not None:
            current = self._confirmation_state_binding({"user_id": user_id})
            if current != {"entity": "user", "key": str(user_id), "row": expected}:
                return self._block("modify_user_address", "confirmation_stale", user_id=user_id)
        return self._block("modify_user_address", fallback, user_id=user_id)

    def _require_trusted_confirmation(
        self, name: str, arguments: dict[str, Any], confirmed: bool
    ) -> dict[str, Any] | None:
        if name not in (WRITE_TOOLS - {"escalate_to_human"}):
            return None
        context = self._active_call_context or {}
        state_binding = self._confirmation_state_binding(arguments)
        record = self.confirmation_ledger.record_for_authorization(
            authorization_id=context.get("confirmation_id"),
            session_id=context.get("session_id"),
            user_id=arguments.get("user_id"),
            operation=name,
            parameters=arguments,
        )
        valid = bool(confirmed) and self.confirmation_ledger.validate_authorization(
            authorization_id=context.get("confirmation_id"),
            session_id=context.get("session_id"),
            user_id=arguments.get("user_id"),
            operation=name,
            parameters=arguments,
            state_binding=state_binding,
        )
        if valid:
            context["confirmation_binding"] = record.state_binding if record is not None else None
            return None
        if record is not None and record.state_binding_hash is not None:
            current_hash = binding_hash(state_binding)
            if current_hash not in {record.state_binding_hash, record.completed_state_binding_hash}:
                return self._block(name, "confirmation_stale", order_id=arguments.get("order_id"))
        return self._block(name, "confirmation_required", order_id=arguments.get("order_id"))

    def _identity_guard(self, name: str, arguments: dict[str, Any]) -> dict | None:
        """Refuse an order-scoped tool that arrives without a usable code.

        Enforced here rather than inside each tool so a ninth order-scoped tool
        cannot be added without the protection.
        """
        if name not in IDENTITY_GUARDED_TOOLS:
            return None
        code = arguments.get("verification_code")
        # No str(), no strip(): coercing here would let 123456, " 123456 " and
        # full-width digits through a guard that promises literal six ASCII digits,
        # and no guardrail span would be recorded for them.
        if isinstance(code, str) and _VERIFICATION_CODE.fullmatch(code):
            return None
        self.guardrails.append({"tool": name, "blocked": True, "reason": "verification_code_required",
                                "supplied": code})
        return {"ok": False, "changed": False, "error": "verification_code_required"}

    def call(
        self,
        name: str,
        *,
        _session_id: str | None = None,
        _confirmation_id: str | None = None,
        **arguments: Any,
    ) -> dict:
        started = time.perf_counter()
        stamp = datetime.now(timezone.utc).isoformat()
        call_id = hashlib.sha1(f"{name}:{len(self.calls)}:{arguments}".encode()).hexdigest()[:12]
        error = None
        blocked = self._identity_guard(name, arguments)
        if blocked is not None:
            self.calls.append(ToolCall(name, arguments, call_id, blocked, stamp,
                                       (time.perf_counter() - started) * 1000, blocked["error"]))
            return blocked
        try:
            # Policies validate before dispatch, but Direct callers, diagnostics,
            # and MCP adapters all converge here too.  Keep this boundary typed so
            # a caller cannot bypass the JSON contract by invoking ``call``
            # directly (for example, passing ``confirmed="false"``).
            validate_arguments(name, arguments)
            self._active_call_context = {
                "session_id": _session_id,
                "confirmation_id": _confirmation_id,
            }
            result = self._registry[name](**arguments)
            if name in (WRITE_TOOLS - {"escalate_to_human"}) and result.get("ok"):
                self.confirmation_ledger.mark_completed(
                    authorization_id=_confirmation_id,
                    session_id=_session_id,
                    user_id=arguments.get("user_id"),
                    operation=name,
                    parameters=arguments,
                    state_binding=self._confirmation_state_binding(arguments),
                )
        except Exception as exc:
            error, result = str(exc), {"ok": False, "error": str(exc)}
        finally:
            self._active_call_context = None
        self.calls.append(ToolCall(name, arguments, call_id, result, stamp, (time.perf_counter() - started) * 1000, error))
        return result

    def search_catalog(self, query: str, top_k: int = 5, category: str | None = None, max_price: float | None = None) -> dict:
        if self.retriever is None:
            return {"ok": False, "error": "retriever_not_configured", "items": []}
        chunks = self.retriever.search(query, top_k=max(top_k * 3, top_k), source_type="product", category=category)
        seen, items = set(), []
        for chunk in chunks:
            pid = chunk.get("product_id")
            if not pid or pid in seen or (max_price is not None and (chunk.get("price") or float("inf")) > max_price):
                continue
            seen.add(pid)
            items.append({k: chunk.get(k) for k in ("product_id", "title", "category", "price", "inventory", "doc_id", "score")})
            if len(items) == top_k:
                break
        return {"ok": True, "items": items}

    def _product_chunks(self, product_id: str) -> list[dict]:
        if self.retriever is None:
            return []
        return [c for c in self.retriever.chunks if c.get("product_id") == product_id]

    def get_product(self, product_id: str) -> dict:
        chunks = self._product_chunks(product_id)
        if not chunks:
            return {"ok": False, "error": "product_not_found"}
        first = chunks[0]
        return {"ok": True, "product": {k: first.get(k) for k in ("product_id", "title", "category", "price", "inventory", "doc_id")}, "evidence": [c.get("text", "") for c in chunks[:5]]}

    def compare_products(self, product_ids: list[str]) -> dict:
        if len(product_ids) < 2:
            return {
                "ok": False,
                "error": "at_least_two_products_required",
                "products": [],
            }
        products = [self.get_product(pid) for pid in product_ids]
        return {"ok": all(p["ok"] for p in products), "products": products}

    def get_policy(self, policy_type: str) -> dict:
        category = POLICY_ALIASES.get(policy_type)
        if category is None:
            return {"ok": False, "error": "unknown_policy_type"}
        if self.retriever is None:
            # Keep the closed return workflow runnable on CPU without silently
            # inventing policy text. The checked-in policy JSONL is the frozen
            # source used when no retrieval index is configured.
            try:
                rows = [
                    json.loads(line)
                    for line in config.POLICY_DATA_PATH.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
            except (OSError, json.JSONDecodeError):
                rows = []
            policies = [
                {
                    "doc_id": f"policy:{row.get('id')}",
                    "title": row.get("title", ""),
                    "text": row.get("content", ""),
                }
                for row in rows
                if row.get("policy_type") == category
            ]
            return {"ok": bool(policies), "policies": policies}
        chunks = self.retriever.search(category, top_k=3, source_type="policy", category=category)
        return {"ok": bool(chunks), "policies": [{"doc_id": c["doc_id"], "title": c["title"], "text": c["text"]} for c in chunks]}

    def _verified_order(self, order_id: str, user_id: str, verification_code: str) -> tuple[dict | None, str | None]:
        conn = orders.connect(self.db_path)
        try:
            user = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            order = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
            if not user or user["verification_code"] != verification_code:
                return None, "identity_verification_failed"
            if not order or order["user_id"] != user_id:
                return None, "order_ownership_mismatch"
            return dict(order), None
        finally:
            conn.close()

    def _verified_user(self, user_id: str, verification_code: str) -> tuple[dict | None, str | None]:
        conn = orders.connect(self.db_path)
        try:
            user = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            if not user or user["verification_code"] != verification_code:
                return None, "identity_verification_failed"
            return dict(user), None
        finally:
            conn.close()

    def _user_payment_methods(self, user_id: str) -> list[str]:
        conn = orders.connect(self.db_path)
        try:
            row = conn.execute("SELECT payment_methods FROM users WHERE user_id=?", (user_id,)).fetchone()
            return _parse_json_list(row["payment_methods"] if row else None)
        finally:
            conn.close()

    def get_order(self, order_id: str, user_id: str, verification_code: str) -> dict:
        order, error = self._verified_order(order_id, user_id, verification_code)
        return {"ok": error is None, "order": order, "error": error}

    def check_return_eligibility(self, order_id: str, user_id: str, verification_code: str) -> dict:
        order, error = self._verified_order(order_id, user_id, verification_code)
        if error:
            return {"ok": False, "eligible": False, "error": error}
        if order["status"] != "delivered":
            return {"ok": True, "eligible": False, "reason": "order_not_delivered", "order": order}
        days = (self.today - date.fromisoformat(order["delivered_at"])).days
        eligible = bool(order["quality_issue"] or (days <= 7 and not order["opened"]))
        reason = "quality_issue" if order["quality_issue"] else "seven_day_return" if eligible else "return_window_expired_or_opened"
        return {"ok": True, "eligible": eligible, "reason": reason, "days_since_delivery": days, "order": order}

    @staticmethod
    def _return_request_id(order_id: str) -> str:
        """Stable id for the active return request of an order (no separate request table)."""
        return f"RR-{order_id}"

    def create_return_request(self, order_id: str, user_id: str, verification_code: str, confirmed: bool) -> dict:
        eligibility = self.check_return_eligibility(order_id, user_id, verification_code)
        if not eligibility.get("ok") or not eligibility.get("eligible"):
            reason = eligibility.get("error") or eligibility.get("reason") or "confirmation_required"
            return self._block("create_return_request", reason, order_id=order_id)
        blocked = self._require_trusted_confirmation(
            "create_return_request",
            {"order_id": order_id, "user_id": user_id,
             "verification_code": verification_code, "confirmed": confirmed},
            confirmed,
        )
        if blocked is not None:
            return blocked
        request_id = self._return_request_id(order_id)
        conn = orders.connect(self.db_path)
        try:
            where, params = self._order_write_guard(
                "WHERE order_id=? AND status='delivered' AND return_status IS NULL",
                (order_id,),
            )
            expected = self._expected_order_state()
            if expected is not None:
                where += " AND opened=? AND quality_issue=? AND delivered_at=?"
                params += (expected.get("opened"), expected.get("quality_issue"), expected.get("delivered_at"))
            cur = conn.execute(
                f"UPDATE orders SET return_status='requested', version=version+1 {where}",
                params,
            )
            conn.commit()
            if cur.rowcount == 1:
                return {
                    "ok": True,
                    "changed": True,
                    "idempotent_replay": False,
                    "request_id": request_id,
                    "status": "active",
                    "order_id": order_id,
                    "return_status": "requested",
                }
            row = conn.execute(
                "SELECT return_status FROM orders WHERE order_id=?", (order_id,)
            ).fetchone()
            if row and row["return_status"] == "requested":
                return {
                    "ok": True,
                    "changed": False,
                    "idempotent_replay": True,
                    "request_id": request_id,
                    "status": "active",
                    "order_id": order_id,
                    "return_status": "requested",
                }
            return self._stale_or_block("create_return_request", order_id, "return_status_conflict")
        finally:
            conn.close()

    def cancel_pending_order(
        self,
        order_id: str,
        user_id: str,
        verification_code: str,
        reason: str,
        confirmed: bool,
    ) -> dict:
        order, error = self._verified_order(order_id, user_id, verification_code)
        if error:
            return self._block("cancel_pending_order", error, order_id=order_id)
        if reason not in CANCEL_REASONS:
            return self._block("cancel_pending_order", "invalid_cancel_reason", order_id=order_id)
        blocked = self._require_trusted_confirmation(
            "cancel_pending_order",
            {"order_id": order_id, "user_id": user_id, "verification_code": verification_code,
             "reason": reason, "confirmed": confirmed},
            confirmed,
        )
        if blocked is not None:
            return blocked
        if order["status"] == "cancelled":
            return {
                "ok": True,
                "changed": False,
                "idempotent_replay": True,
                "order_id": order_id,
                "status": "cancelled",
                "cancel_reason": order.get("cancel_reason") or reason,
            }
        if order["status"] != "pending":
            return self._block("cancel_pending_order", "order_not_pending", order_id=order_id, status=order["status"])
        conn = orders.connect(self.db_path)
        try:
            where, params = self._order_write_guard(
                "WHERE order_id=? AND status='pending'", (order_id,)
            )
            cur = conn.execute(
                f"UPDATE orders SET status='cancelled', cancel_reason=?, version=version+1 {where}",
                (reason, *params),
            )
            conn.commit()
            if cur.rowcount != 1:
                return self._stale_or_block("cancel_pending_order", order_id, "order_not_pending")
            return {
                "ok": True,
                "changed": True,
                "idempotent_replay": False,
                "order_id": order_id,
                "status": "cancelled",
                "cancel_reason": reason,
            }
        finally:
            conn.close()

    def modify_pending_order_address(
        self,
        order_id: str,
        user_id: str,
        verification_code: str,
        address1: str,
        address2: str,
        city: str,
        state: str,
        country: str,
        zip: str,
        confirmed: bool,
    ) -> dict:
        order, error = self._verified_order(order_id, user_id, verification_code)
        if error:
            return self._block("modify_pending_order_address", error, order_id=order_id)
        blocked = self._require_trusted_confirmation(
            "modify_pending_order_address",
            {"order_id": order_id, "user_id": user_id, "verification_code": verification_code,
             "address1": address1, "address2": address2, "city": city, "state": state,
             "country": country, "zip": zip, "confirmed": confirmed},
            confirmed,
        )
        if blocked is not None:
            return blocked
        if order["status"] != "pending":
            return self._block(
                "modify_pending_order_address", "order_not_pending", order_id=order_id, status=order["status"]
            )
        address = _address_payload(address1, address2, city, state, country, zip)
        encoded = json.dumps(address, ensure_ascii=False, sort_keys=True)
        if order.get("shipping_address") == encoded:
            return {
                "ok": True,
                "changed": False,
                "idempotent_replay": True,
                "order_id": order_id,
                "status": order["status"],
                "shipping_address": address,
            }
        conn = orders.connect(self.db_path)
        try:
            where, params = self._order_write_guard(
                "WHERE order_id=? AND status='pending'", (order_id,)
            )
            cur = conn.execute(
                f"UPDATE orders SET shipping_address=?, version=version+1 {where}",
                (encoded, *params),
            )
            conn.commit()
            if cur.rowcount != 1:
                return self._stale_or_block("modify_pending_order_address", order_id, "order_not_pending")
            return {
                "ok": True,
                "changed": True,
                "idempotent_replay": False,
                "order_id": order_id,
                "status": "pending",
                "shipping_address": address,
            }
        finally:
            conn.close()

    def modify_pending_order_items(
        self,
        order_id: str,
        user_id: str,
        verification_code: str,
        item_ids: list[str],
        new_item_ids: list[str],
        payment_method_id: str,
        confirmed: bool,
    ) -> dict:
        order, error = self._verified_order(order_id, user_id, verification_code)
        if error:
            return self._block("modify_pending_order_items", error, order_id=order_id)
        blocked = self._require_trusted_confirmation(
            "modify_pending_order_items",
            {"order_id": order_id, "user_id": user_id, "verification_code": verification_code,
             "item_ids": item_ids, "new_item_ids": new_item_ids,
             "payment_method_id": payment_method_id, "confirmed": confirmed},
            confirmed,
        )
        if blocked is not None:
            return blocked
        if order["status"] != "pending":
            return self._block(
                "modify_pending_order_items", "order_not_pending", order_id=order_id, status=order["status"]
            )
        if not item_ids or len(item_ids) != len(new_item_ids):
            return self._block("modify_pending_order_items", "item_length_mismatch", order_id=order_id)
        current_items = _parse_json_list(order.get("item_ids")) or [order["product_id"]]
        for item_id in item_ids:
            if item_ids.count(item_id) > current_items.count(item_id):
                return self._block("modify_pending_order_items", "item_not_found", order_id=order_id, item_id=item_id)
        if payment_method_id not in self._user_payment_methods(user_id):
            return self._block("modify_pending_order_items", "payment_method_not_found", order_id=order_id)
        encoded_items = json.dumps(list(new_item_ids), ensure_ascii=False)
        if current_items == list(new_item_ids) and order.get("payment_method_id") == payment_method_id:
            return {
                "ok": True,
                "changed": False,
                "idempotent_replay": True,
                "order_id": order_id,
                "status": "pending",
                "item_ids": list(new_item_ids),
                "product_id": new_item_ids[0],
                "payment_method_id": payment_method_id,
            }
        conn = orders.connect(self.db_path)
        try:
            where, params = self._order_write_guard(
                "WHERE order_id=? AND status='pending'", (order_id,)
            )
            cur = conn.execute(
                "UPDATE orders SET product_id=?, item_ids=?, payment_method_id=?, version=version+1 "
                + where,
                (new_item_ids[0], encoded_items, payment_method_id, *params),
            )
            conn.commit()
            if cur.rowcount != 1:
                return self._stale_or_block("modify_pending_order_items", order_id, "order_not_pending")
            return {
                "ok": True,
                "changed": True,
                "idempotent_replay": False,
                "order_id": order_id,
                "status": "pending",
                "item_ids": list(new_item_ids),
                "product_id": new_item_ids[0],
                "payment_method_id": payment_method_id,
            }
        finally:
            conn.close()

    def modify_pending_order_payment(
        self,
        order_id: str,
        user_id: str,
        verification_code: str,
        payment_method_id: str,
        confirmed: bool,
    ) -> dict:
        order, error = self._verified_order(order_id, user_id, verification_code)
        if error:
            return self._block("modify_pending_order_payment", error, order_id=order_id)
        blocked = self._require_trusted_confirmation(
            "modify_pending_order_payment",
            {"order_id": order_id, "user_id": user_id, "verification_code": verification_code,
             "payment_method_id": payment_method_id, "confirmed": confirmed},
            confirmed,
        )
        if blocked is not None:
            return blocked
        if order["status"] != "pending":
            return self._block(
                "modify_pending_order_payment", "order_not_pending", order_id=order_id, status=order["status"]
            )
        if payment_method_id not in self._user_payment_methods(user_id):
            return self._block("modify_pending_order_payment", "payment_method_not_found", order_id=order_id)
        if order.get("payment_method_id") == payment_method_id:
            return {
                "ok": True,
                "changed": False,
                "idempotent_replay": True,
                "order_id": order_id,
                "status": "pending",
                "payment_method_id": payment_method_id,
            }
        conn = orders.connect(self.db_path)
        try:
            where, params = self._order_write_guard(
                "WHERE order_id=? AND status='pending'", (order_id,)
            )
            cur = conn.execute(
                f"UPDATE orders SET payment_method_id=?, version=version+1 {where}",
                (payment_method_id, *params),
            )
            conn.commit()
            if cur.rowcount != 1:
                return self._stale_or_block("modify_pending_order_payment", order_id, "order_not_pending")
            return {
                "ok": True,
                "changed": True,
                "idempotent_replay": False,
                "order_id": order_id,
                "status": "pending",
                "payment_method_id": payment_method_id,
            }
        finally:
            conn.close()

    def modify_user_address(
        self,
        user_id: str,
        verification_code: str,
        address1: str,
        address2: str,
        city: str,
        state: str,
        country: str,
        zip: str,
        confirmed: bool,
    ) -> dict:
        user, error = self._verified_user(user_id, verification_code)
        if error:
            return self._block("modify_user_address", error, user_id=user_id)
        blocked = self._require_trusted_confirmation(
            "modify_user_address",
            {"user_id": user_id, "verification_code": verification_code,
             "address1": address1, "address2": address2, "city": city, "state": state,
             "country": country, "zip": zip, "confirmed": confirmed},
            confirmed,
        )
        if blocked is not None:
            return blocked
        address = _address_payload(address1, address2, city, state, country, zip)
        encoded = json.dumps(address, ensure_ascii=False, sort_keys=True)
        if user.get("address") == encoded:
            return {
                "ok": True,
                "changed": False,
                "idempotent_replay": True,
                "user_id": user_id,
                "address": address,
            }
        conn = orders.connect(self.db_path)
        try:
            expected = self._expected_user_state()
            where = "WHERE user_id=?"
            params: tuple[Any, ...] = (user_id,)
            if expected is not None:
                where += " AND name=? AND address IS ? AND payment_methods IS ?"
                params += (expected.get("name"), expected.get("address"), expected.get("payment_methods"))
            cur = conn.execute(f"UPDATE users SET address=? {where}", (encoded, *params))
            conn.commit()
            if cur.rowcount != 1:
                return self._stale_user_or_block(user_id, "user_not_found")
            return {
                "ok": True,
                "changed": True,
                "idempotent_replay": False,
                "user_id": user_id,
                "address": address,
            }
        finally:
            conn.close()

    def return_delivered_order_items(
        self,
        order_id: str,
        user_id: str,
        verification_code: str,
        item_ids: list[str],
        payment_method_id: str,
        confirmed: bool,
    ) -> dict:
        eligibility = self.check_return_eligibility(order_id, user_id, verification_code)
        if not eligibility.get("ok") or not eligibility.get("eligible"):
            reason = eligibility.get("error") or eligibility.get("reason") or "confirmation_required"
            return self._block("return_delivered_order_items", reason, order_id=order_id)
        blocked = self._require_trusted_confirmation(
            "return_delivered_order_items",
            {"order_id": order_id, "user_id": user_id, "verification_code": verification_code,
             "item_ids": item_ids, "payment_method_id": payment_method_id, "confirmed": confirmed},
            confirmed,
        )
        if blocked is not None:
            return blocked
        order = eligibility["order"]
        current_items = _parse_json_list(order.get("item_ids")) or [order["product_id"]]
        if not item_ids:
            return self._block("return_delivered_order_items", "item_not_found", order_id=order_id)
        for item_id in item_ids:
            if item_ids.count(item_id) > current_items.count(item_id):
                return self._block("return_delivered_order_items", "item_not_found", order_id=order_id, item_id=item_id)
        methods = self._user_payment_methods(user_id)
        is_original_payment = payment_method_id == order.get("payment_method_id")
        is_existing_gift_card = payment_method_id in methods and payment_method_id.startswith("gift_card_")
        if not is_original_payment and not is_existing_gift_card:
            return self._block("return_delivered_order_items", "payment_method_not_found", order_id=order_id)
        request_id = self._return_request_id(order_id)
        conn = orders.connect(self.db_path)
        try:
            where, params = self._order_write_guard(
                "WHERE order_id=? AND status='delivered' AND return_status IS NULL",
                (order_id,),
            )
            expected = self._expected_order_state()
            if expected is not None:
                where += " AND opened=? AND quality_issue=? AND delivered_at=?"
                params += (expected.get("opened"), expected.get("quality_issue"), expected.get("delivered_at"))
            cur = conn.execute(
                f"UPDATE orders SET return_status='requested', version=version+1 {where}",
                params,
            )
            conn.commit()
            if cur.rowcount == 1:
                return {
                    "ok": True,
                    "changed": True,
                    "idempotent_replay": False,
                    "request_id": request_id,
                    "order_id": order_id,
                    "status": "delivered",
                    "return_status": "requested",
                    "item_ids": list(item_ids),
                    "payment_method_id": payment_method_id,
                }
            row = conn.execute(
                "SELECT return_status FROM orders WHERE order_id=?", (order_id,)
            ).fetchone()
            if row and row["return_status"] == "requested":
                return {
                    "ok": True,
                    "changed": False,
                    "idempotent_replay": True,
                    "request_id": request_id,
                    "order_id": order_id,
                    "status": "delivered",
                    "return_status": "requested",
                    "item_ids": list(item_ids),
                    "payment_method_id": payment_method_id,
                }
            return self._stale_or_block("return_delivered_order_items", order_id, "return_status_conflict")
        finally:
            conn.close()

    def exchange_delivered_order_items(
        self,
        order_id: str,
        user_id: str,
        verification_code: str,
        item_ids: list[str],
        new_item_ids: list[str],
        payment_method_id: str,
        confirmed: bool,
    ) -> dict:
        order, error = self._verified_order(order_id, user_id, verification_code)
        if error:
            return self._block("exchange_delivered_order_items", error, order_id=order_id)
        blocked = self._require_trusted_confirmation(
            "exchange_delivered_order_items",
            {"order_id": order_id, "user_id": user_id, "verification_code": verification_code,
             "item_ids": item_ids, "new_item_ids": new_item_ids,
             "payment_method_id": payment_method_id, "confirmed": confirmed},
            confirmed,
        )
        if blocked is not None:
            return blocked
        if order["status"] != "delivered":
            return self._block(
                "exchange_delivered_order_items",
                "order_not_delivered",
                order_id=order_id,
                status=order["status"],
            )
        if order.get("exchange_status"):
            current_items = _parse_json_list(order.get("item_ids"))
            if current_items == list(new_item_ids):
                return {
                    "ok": True,
                    "changed": False,
                    "idempotent_replay": True,
                    "order_id": order_id,
                    "status": "delivered",
                    "exchange_status": order["exchange_status"],
                    "item_ids": current_items,
                    "product_id": order["product_id"],
                    "payment_method_id": order.get("payment_method_id"),
                }
            return self._block("exchange_delivered_order_items", "exchange_already_completed", order_id=order_id)
        if not item_ids or len(item_ids) != len(new_item_ids):
            return self._block("exchange_delivered_order_items", "item_length_mismatch", order_id=order_id)
        current_items = _parse_json_list(order.get("item_ids")) or [order["product_id"]]
        for item_id in item_ids:
            if item_ids.count(item_id) > current_items.count(item_id):
                return self._block("exchange_delivered_order_items", "item_not_found", order_id=order_id, item_id=item_id)
        if payment_method_id not in self._user_payment_methods(user_id):
            return self._block("exchange_delivered_order_items", "payment_method_not_found", order_id=order_id)
        encoded_items = json.dumps(list(new_item_ids), ensure_ascii=False)
        conn = orders.connect(self.db_path)
        try:
            where, params = self._order_write_guard(
                "WHERE order_id=? AND status='delivered' AND exchange_status IS NULL",
                (order_id,),
            )
            cur = conn.execute(
                "UPDATE orders SET product_id=?, item_ids=?, payment_method_id=?, "
                "exchange_status='exchanged', version=version+1 "
                + where,
                (new_item_ids[0], encoded_items, payment_method_id, *params),
            )
            conn.commit()
            if cur.rowcount != 1:
                return self._stale_or_block("exchange_delivered_order_items", order_id, "exchange_already_completed")
            return {
                "ok": True,
                "changed": True,
                "idempotent_replay": False,
                "order_id": order_id,
                "status": "delivered",
                "exchange_status": "exchanged",
                "item_ids": list(new_item_ids),
                "product_id": new_item_ids[0],
                "payment_method_id": payment_method_id,
            }
        finally:
            conn.close()

    def escalate_to_human(self, user_id: str, reason: str, order_id: str | None = None) -> dict:
        stamp = datetime.now(timezone.utc).isoformat()
        hid = "H" + hashlib.sha1(f"{user_id}:{order_id}:{reason}:{stamp}".encode()).hexdigest()[:10]
        conn = orders.connect(self.db_path)
        try:
            conn.execute("INSERT INTO handoffs VALUES(?,?,?,?,?)", (hid, user_id, order_id, reason, stamp))
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "handoff_id": hid}
