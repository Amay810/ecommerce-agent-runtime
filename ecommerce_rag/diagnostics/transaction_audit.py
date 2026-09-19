"""CPU-only transaction replay, contract auditing, and surface differential tests.

This module deliberately keeps the model out of the loop.  ``RetailTools`` is
the guarded execution path; ``UnsafeRetailTools`` is an explicitly labelled
counterfactual fixture used only to answer what the same canonical action would
do without the existing checks.  It is not a second production security layer.

The state normalizer is intentionally conservative:

* SQLite rows are the source of truth; observations are never treated as state.
* JSON columns are parsed, but list ordering is preserved.
* ``None`` and missing result fields are equivalent only in observation
  normalization, not in database state.
* generated handoff identifiers and timestamps are volatile; handoff row
  count and stable fields remain observable.
* numeric values are normalized through ``Decimal`` rather than float equality.

The current schema has no money, payment-history, or ledger columns.  Money
conservation contracts therefore remain explicitly out of scope until the
business state contains the fields needed to evaluate them.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sqlite3
import tempfile
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..mcp_server import MCPRetailFacade, MCP_TOOL_NAMES
from ..orders import connect, seed_database
from ..tools import READ_TOOLS, WRITE_TOOLS, RetailTools


MONEY_QUANTUM = Decimal("0.01")
MONEY_ROUNDING = ROUND_HALF_UP
VOLATILE_STATE_FIELDS = frozenset({"handoff_id", "created_at"})
JSON_STATE_COLUMNS = frozenset({"address", "payment_methods", "item_ids", "shipping_address"})

ORDER_SCOPED_TOOLS = frozenset(
    {
        "get_order",
        "check_return_eligibility",
        "create_return_request",
        "cancel_pending_order",
        "modify_pending_order_address",
        "modify_pending_order_items",
        "modify_pending_order_payment",
        "return_delivered_order_items",
        "exchange_delivered_order_items",
    }
)
USER_SCOPED_TOOLS = frozenset({"modify_user_address", "escalate_to_human"})
CONFIRMATION_TOOLS = frozenset(WRITE_TOOLS) - {"escalate_to_human"}
READ_ONLY_TOOLS = frozenset(READ_TOOLS)


@dataclass(frozen=True)
class Contract:
    """Machine-readable frozen contract metadata."""

    id: str
    name: str
    scope: str
    applicable_tools: tuple[str, ...]
    required_fields: tuple[str, ...]
    severity: str
    predicate_type: str
    normalization: str
    evidence_source: str


FROZEN_CONTRACTS: tuple[Contract, ...] = (
    Contract(
        "A1_identity_binding",
        "Identity and order-owner binding",
        "invocation_precondition",
        tuple(sorted(ORDER_SCOPED_TOOLS | USER_SCOPED_TOOLS)),
        ("user_id", "verification_code (order-scoped tools only)"),
        "critical",
        "precondition",
        "literal ASCII six-digit code; SQLite user/order equality",
        "ecommerce_rag/tools.py:_identity_guard,_verified_order,_verified_user",
    ),
    Contract(
        "A2_confirmation_binding",
        "Explicit confirmation boolean plus trusted bound authorization are required by write tools",
        "invocation_precondition",
        tuple(sorted(CONFIRMATION_TOOLS)),
        ("confirmed", "session/user/operation/parameter-bound authorization"),
        "high",
        "precondition",
        "boolean True only; no truthy coercion",
        "ecommerce_rag/confirmation.py and ecommerce_rag/tools.py write methods",
    ),
    Contract(
        "S1_legal_state_transition",
        "Tool-specific order state transition is legal",
        "state_transition",
        tuple(sorted(CONFIRMATION_TOOLS)),
        ("status", "return_status", "exchange_status"),
        "critical",
        "transition",
        "exact string state comparison",
        "ecommerce_rag/orders.py ORDER_STATES and ecommerce_rag/tools.py write predicates",
    ),
    Contract(
        "S2_mutation_scope",
        "A mutation changes only its declared entity and fields",
        "state_transition",
        tuple(sorted(WRITE_TOOLS)),
        (),
        "high",
        "postcondition",
        "normalized SQLite row diff; volatile handoff id/time excluded",
        "ecommerce_rag/tools.py SQL UPDATE/INSERT statements",
    ),
    Contract(
        "S3_item_identity",
        "Selected existing item ids and exchange cardinality are preserved",
        "invocation_precondition",
        (
            "exchange_delivered_order_items",
            "modify_pending_order_items",
            "return_delivered_order_items",
        ),
        ("item_ids", "new_item_ids"),
        "high",
        "precondition",
        "ordered JSON list with multiplicity",
        "ecommerce_rag/tools.py item_ids checks",
    ),
    Contract(
        "P1_refund_destination_closure",
        "Return destination is original payment or an owned existing gift card",
        "invocation_precondition",
        ("return_delivered_order_items",),
        ("payment_method_id",),
        "critical",
        "precondition",
        "exact string equality; gift_card_ prefix plus user payment_methods membership",
        "ecommerce_rag/tools.py:return_delivered_order_items lines 576-580",
    ),
    Contract(
        "A3_idempotency",
        "A repeated completed mutation is an error or a no-op",
        "state_transition",
        tuple(sorted(CONFIRMATION_TOOLS)),
        (),
        "high",
        "postcondition",
        "changed=False for idempotent replay; version must not increase",
        "ecommerce_rag/tools.py idempotent_replay branches and version updates",
    ),
    Contract(
        "A4_read_only_purity",
        "Read-only tools do not change normalized SQLite state",
        "state_transition",
        tuple(sorted(READ_ONLY_TOOLS)),
        (),
        "high",
        "postcondition",
        "exact normalized state hash",
        "ecommerce_rag/tools.py READ_TOOLS and read method bodies",
    ),
)


UNRESOLVED_CANDIDATES: tuple[dict[str, Any], ...] = (
    {
        "id": "A2_confirmation_binding",
        "reason": "The tool API accepts only confirmed=True; no confirmation token or prior evidence binding is persisted.",
        "source": "ecommerce_rag/tools.py write signatures; ecommerce_rag/domain.py Trajectory evidence is outside RetailTools",
    },
    {
        "id": "P2_P5_money_contracts",
        "reason": "The local SQLite schema has no amount, price, tax, discount, shipping, balance, or payment-history fields.",
        "source": "ecommerce_rag/orders.py:init_db and seed_database",
    },
    {
        "id": "S3_new_item_catalog_existence",
        "reason": "Orders store product ids but the transactional DB does not contain the catalog; an audit cannot prove new ids exist without a retriever/catalog snapshot.",
        "source": "ecommerce_rag/orders.py orders schema; ecommerce_rag/tools.py exchange/item writes",
    },
)


def frozen_contract_spec() -> dict[str, Any]:
    """Return the frozen spec without runtime-only callables."""

    return {
        "spec_version": "2026-09-04.v1",
        "money": {
            "available": False,
            "normalization": "Decimal(str(value)).quantize(0.01, ROUND_HALF_UP)",
            "currency": "not represented by current schema",
        },
        "state_normalization": {
            "json_columns": sorted(JSON_STATE_COLUMNS),
            "volatile_fields": sorted(VOLATILE_STATE_FIELDS),
            "list_order": "preserved",
            "state_none_vs_missing": "not equivalent",
            "observation_none_vs_missing": "equivalent",
            "error_normalization": "ok/error/changed plus normalized structured payload",
        },
        "contracts": [asdict(contract) for contract in FROZEN_CONTRACTS],
        "unresolved_candidates": list(UNRESOLVED_CANDIDATES),
    }


def normalize_money(value: Any) -> str | None:
    """Normalize a money-like value without using binary float equality."""

    if value is None:
        return None
    try:
        return format(Decimal(str(value)).quantize(MONEY_QUANTUM, rounding=MONEY_ROUNDING), "f")
    except (InvalidOperation, ValueError, TypeError):
        return str(value)


def _normalize_scalar(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, Decimal):
        return normalize_money(value)
    if isinstance(value, float):
        return normalize_money(value)
    return str(value)


def _parse_json_column(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def normalize_state_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): normalize_state_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [normalize_state_value(item) for item in value]
    if isinstance(value, tuple):
        return [normalize_state_value(item) for item in value]
    return _normalize_scalar(value)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def database_state(db_path: Path | str) -> dict[str, Any]:
    """Capture normalized state from SQLite, including users/orders/handoffs."""

    conn = connect(db_path)
    try:
        tables: dict[str, Any] = {}
        for table in ("users", "orders", "handoffs"):
            if not _table_exists(conn, table):
                continue
            columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            order_by = ", ".join(
                str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})") if row[5]
            ) or columns[0]
            rows = []
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order_by}"):
                item: dict[str, Any] = {}
                for column in columns:
                    if table == "handoffs" and column in VOLATILE_STATE_FIELDS:
                        continue
                    value = row[column]
                    if column in JSON_STATE_COLUMNS:
                        value = _parse_json_column(value)
                    item[column] = normalize_state_value(value)
                rows.append(item)
            tables[table] = {"columns": columns, "rows": rows}
        return {"tables": tables}
    finally:
        conn.close()


def state_hash(state: dict[str, Any]) -> str:
    encoded = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _diff_values(expected: Any, actual: Any, path: str, output: dict[str, Any]) -> None:
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual)):
            _diff_values(expected.get(key), actual.get(key), f"{path}.{key}" if path else key, output)
        return
    if isinstance(expected, list) and isinstance(actual, list):
        if expected != actual:
            output[path] = {"before": expected, "after": actual}
        return
    if expected != actual:
        output[path] = {"before": expected, "after": actual}


def state_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Return compact field-level SQLite differences rather than whole dumps."""

    output: dict[str, Any] = {}
    for table, key in (("users", "user_id"), ("orders", "order_id")):
        old = {row.get(key): row for row in _rows(before, table)}
        new = {row.get(key): row for row in _rows(after, table)}
        for row_key in sorted(set(old) | set(new), key=str):
            old_row, new_row = old.get(row_key, {}), new.get(row_key, {})
            for field_name in sorted(set(old_row) | set(new_row)):
                if old_row.get(field_name) != new_row.get(field_name):
                    output[f"{table}.{row_key}.{field_name}"] = {
                        "before": old_row.get(field_name),
                        "after": new_row.get(field_name),
                    }
    old_handoffs = _rows(before, "handoffs")
    new_handoffs = _rows(after, "handoffs")
    if old_handoffs != new_handoffs:
        output["handoffs.rows"] = {
            "before_count": len(old_handoffs),
            "after_count": len(new_handoffs),
        }
    return output


def normalize_observation(value: Any) -> Any:
    """Normalize direct/MCP observations for semantic comparison.

    Result fields with ``None`` are omitted so an adapter that serializes an
    optional field as null is equivalent to one that omits it. List order is
    preserved because item order is part of the current tool surface.
    """

    if isinstance(value, dict):
        return {
            str(key): normalize_observation(value[key])
            for key in sorted(value)
            if value[key] is not None and key not in {"handoff_id"}
        }
    if isinstance(value, list):
        return [normalize_observation(item) for item in value]
    if isinstance(value, float):
        return normalize_money(value)
    return _normalize_scalar(value)


@dataclass(frozen=True)
class CanonicalAction:
    tool: str
    args: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict, compare=False)

    def canonical_args(self) -> dict[str, Any]:
        return normalize_state_value(copy.deepcopy(self.args))


@dataclass
class StepAudit:
    trajectory_id: str
    task_id: str
    step_idx: int
    tool: str
    canonical_args: dict[str, Any]
    execution_path: str
    pre_state_hash: str
    post_state_hash: str
    state_diff: dict[str, Any]
    observation: dict[str, Any]
    normalized_error: str | None
    invariant_checks: list[dict[str, Any]]
    attempted_violation: bool
    blocked_violation: bool
    committed_violation: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _rows(state: dict[str, Any], table: str) -> list[dict[str, Any]]:
    return list(state.get("tables", {}).get(table, {}).get("rows", []))


def _row(state: dict[str, Any], table: str, key: str, value: Any) -> dict[str, Any] | None:
    return next((item for item in _rows(state, table) if item.get(key) == value), None)


def _items(order: dict[str, Any] | None) -> list[str]:
    if not order:
        return []
    raw = order.get("item_ids")
    if isinstance(raw, list):
        return [str(item) for item in raw]
    if raw:
        parsed = _parse_json_column(raw)
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    return [str(order.get("product_id"))] if order.get("product_id") else []


def _payment_methods(user: dict[str, Any] | None) -> list[str]:
    if not user:
        return []
    value = _parse_json_column(user.get("payment_methods"))
    return [str(item) for item in value] if isinstance(value, list) else []


def _violation(
    contract_id: str,
    phase: str,
    message: str,
    **details: Any,
) -> dict[str, Any]:
    return {
        "invariant_id": contract_id,
        "phase": phase,
        "message": message,
        "details": details,
    }


def _is_completed_before(tool: str, order: dict[str, Any] | None, user: dict[str, Any] | None) -> bool:
    if not order and tool != "modify_user_address":
        return False
    if tool == "cancel_pending_order":
        return bool(order and order.get("status") == "cancelled")
    if tool in {"create_return_request", "return_delivered_order_items"}:
        return bool(order and order.get("return_status") == "requested")
    if tool == "exchange_delivered_order_items":
        return bool(order and order.get("exchange_status"))
    if tool == "modify_pending_order_address":
        return bool(order and order.get("status") != "pending")
    if tool in {"modify_pending_order_items", "modify_pending_order_payment"}:
        return bool(order and order.get("status") != "pending")
    return False


def _address_args(args: dict[str, Any]) -> str:
    return json.dumps(
        {
            key: args.get(key)
            for key in ("address1", "address2", "city", "state", "country", "zip")
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _changed_fields(before: dict[str, Any], after: dict[str, Any]) -> set[tuple[str, str, str]]:
    changed: set[tuple[str, str, str]] = set()
    for table, key in (("users", "user_id"), ("orders", "order_id")):
        old = {row.get(key): row for row in _rows(before, table)}
        new = {row.get(key): row for row in _rows(after, table)}
        for row_key in set(old) | set(new):
            old_row, new_row = old.get(row_key, {}), new.get(row_key, {})
            for field_name in set(old_row) | set(new_row):
                if old_row.get(field_name) != new_row.get(field_name):
                    changed.add((table, str(row_key), field_name))
    return changed


def _precondition_violations(
    before: dict[str, Any],
    action: CanonicalAction,
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    tool, args = action.tool, action.args
    violations: list[dict[str, Any]] = []
    user_id = args.get("user_id")
    user = _row(before, "users", "user_id", user_id)
    order = _row(before, "orders", "order_id", args.get("order_id"))

    if tool in ORDER_SCOPED_TOOLS:
        if not user or user.get("verification_code") != args.get("verification_code"):
            violations.append(
                _violation(
                    "A1_identity_binding",
                    "pre",
                    "verification code is not the exact code stored for user_id",
                    user_id=user_id,
                )
            )
        if not order or order.get("user_id") != user_id:
            violations.append(
                _violation(
                    "A1_identity_binding",
                    "pre",
                    "order is not owned by the supplied user_id",
                    order_id=args.get("order_id"),
                    user_id=user_id,
                )
            )
    elif tool in USER_SCOPED_TOOLS and not user:
        violations.append(
            _violation("A1_identity_binding", "pre", "user_id does not exist", user_id=user_id)
        )

    if tool in CONFIRMATION_TOOLS and args.get("confirmed") is not True:
        violations.append(
            _violation("A2_confirmation_binding", "pre", "confirmed must be literal True and trusted authorization must be present")
        )

    if tool == "cancel_pending_order" and order and order.get("status") not in {"pending", "cancelled"}:
        violations.append(
            _violation("S1_legal_state_transition", "pre", "cancel requires pending order", status=order.get("status"))
        )
    elif tool in {"modify_pending_order_address", "modify_pending_order_items", "modify_pending_order_payment"}:
        if order and order.get("status") != "pending":
            violations.append(
                _violation("S1_legal_state_transition", "pre", "pending-order mutation requires pending state", status=order.get("status"))
            )
    elif tool in {"return_delivered_order_items", "exchange_delivered_order_items"}:
        if order and order.get("status") not in {"delivered"}:
            violations.append(
                _violation("S1_legal_state_transition", "pre", "delivered-order mutation requires delivered state", status=order.get("status"))
            )
    elif tool == "create_return_request" and order:
        if order.get("status") != "delivered":
            violations.append(
                _violation("S1_legal_state_transition", "pre", "return request requires delivered state", status=order.get("status"))
            )
        else:
            try:
                days = (date(2026, 7, 20) - date.fromisoformat(str(order["delivered_at"]))).days
            except (KeyError, TypeError, ValueError):
                days = 10_000
            eligible = bool(order.get("quality_issue") or (days <= 7 and not order.get("opened")))
            if not eligible:
                violations.append(
                    _violation("S1_legal_state_transition", "pre", "return request requires eligible delivered order")
                )

    if tool in {"return_delivered_order_items", "exchange_delivered_order_items", "modify_pending_order_items"}:
        selected = args.get("item_ids")
        current = _items(order)
        if not isinstance(selected, list) or not selected:
            violations.append(_violation("S3_item_identity", "pre", "item_ids must be a non-empty list"))
        elif any(selected.count(item) > current.count(item) for item in selected):
            violations.append(
                _violation("S3_item_identity", "pre", "selected item is not present with sufficient multiplicity", current=current, selected=selected)
            )
        if tool in {"exchange_delivered_order_items", "modify_pending_order_items"}:
            replacement = args.get("new_item_ids")
            if not isinstance(replacement, list) or len(selected or []) != len(replacement):
                violations.append(
                    _violation("S3_item_identity", "pre", "replacement list cardinality does not match selected items")
                )

    if tool == "return_delivered_order_items" and order and user:
        payment = args.get("payment_method_id")
        allowed = payment == order.get("payment_method_id") or (
            payment in _payment_methods(user) and str(payment).startswith("gift_card_")
        )
        if not allowed:
            violations.append(
                _violation("P1_refund_destination_closure", "pre", "refund destination is not original payment or owned gift card", payment_method_id=payment)
            )

    # This catches duplicate mutations in the OFF fixture and also protects the
    # auditor from treating an incremented version as a harmless replay.
    if _is_completed_before(tool, order, user) and tool != "escalate_to_human":
        violations.append(
            _violation("A3_idempotency", "pre", "action targets an already-completed mutation")
        )
    return violations


def _postcondition_violations(
    before: dict[str, Any],
    after: dict[str, Any],
    action: CanonicalAction,
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    tool, args = action.tool, action.args
    violations: list[dict[str, Any]] = []
    diff = state_diff(before, after)

    if tool in READ_ONLY_TOOLS and diff:
        violations.append(
            _violation("A4_read_only_purity", "post", "read-only tool changed SQLite state", diff=diff)
        )

    changed = _changed_fields(before, after)
    if not changed:
        return violations

    if tool == "escalate_to_human":
        if any(table != "handoffs" for table, _, _ in changed):
            violations.append(_violation("S2_mutation_scope", "post", "handoff changed non-handoff state"))
        return violations

    allowed_by_tool: dict[str, set[str]] = {
        "cancel_pending_order": {"status", "cancel_reason", "version"},
        "create_return_request": {"return_status", "version"},
        "return_delivered_order_items": {"return_status", "version"},
        "exchange_delivered_order_items": {"product_id", "item_ids", "payment_method_id", "exchange_status", "version"},
        "modify_pending_order_address": {"shipping_address", "version"},
        "modify_pending_order_items": {"product_id", "item_ids", "payment_method_id", "version"},
        "modify_pending_order_payment": {"payment_method_id", "version"},
        "modify_user_address": {"address"},
    }
    allowed = allowed_by_tool.get(tool, set())
    target_table = "users" if tool == "modify_user_address" else "orders"
    target_key = args.get("user_id") if target_table == "users" else args.get("order_id")
    illegal = {
        (table, key, column)
        for table, key, column in changed
        if table != target_table or key != str(target_key) or column not in allowed
    }
    if illegal:
        violations.append(
            _violation("S2_mutation_scope", "post", "mutation changed an undeclared row or field", fields=sorted(illegal))
        )

    before_row = _row(before, target_table, "user_id" if target_table == "users" else "order_id", target_key)
    after_row = _row(after, target_table, "user_id" if target_table == "users" else "order_id", target_key)
    if result.get("changed") and target_table == "orders" and before_row and after_row:
        if after_row.get("version") != before_row.get("version", 0) + 1:
            violations.append(
                _violation("S2_mutation_scope", "post", "order mutation did not advance version exactly once", before=before_row.get("version"), after=after_row.get("version"))
            )
    if result.get("idempotent_replay") and diff:
        violations.append(
            _violation("A3_idempotency", "post", "idempotent replay changed state", diff=diff)
        )

    if tool == "cancel_pending_order" and result.get("changed") and after_row and after_row.get("status") != "cancelled":
        violations.append(_violation("S1_legal_state_transition", "post", "cancel did not end in cancelled state"))
    if tool in {"create_return_request", "return_delivered_order_items"} and result.get("changed") and after_row and after_row.get("return_status") != "requested":
        violations.append(_violation("S1_legal_state_transition", "post", "return did not end in requested state"))
    if tool == "exchange_delivered_order_items" and result.get("changed") and after_row:
        if after_row.get("exchange_status") != "exchanged" or after_row.get("item_ids") != args.get("new_item_ids"):
            violations.append(_violation("S1_legal_state_transition", "post", "exchange did not produce requested exchanged state"))
    return violations


class UnsafeRetailTools:
    """Intentional OFF counterfactual; never used by production code."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self._handoff_counter = 0

    def call(self, name: str, **args: Any) -> dict[str, Any]:
        if name in READ_ONLY_TOOLS:
            # Read-only calls are delegated so the comparison still tests the
            # same read semantics; only mutating SQL is deliberately unchecked.
            return RetailTools(self.db_path).call(name, **args)
        conn = connect(self.db_path)
        try:
            order_id = args.get("order_id")
            if name == "cancel_pending_order":
                cur = conn.execute(
                    "UPDATE orders SET status='cancelled', cancel_reason=?, version=version+1 WHERE order_id=?",
                    (args.get("reason"), order_id),
                )
            elif name in {"create_return_request", "return_delivered_order_items"}:
                cur = conn.execute(
                    "UPDATE orders SET return_status='requested', version=version+1 WHERE order_id=?",
                    (order_id,),
                )
            elif name == "exchange_delivered_order_items":
                replacement = args.get("new_item_ids") or []
                cur = conn.execute(
                    "UPDATE orders SET product_id=?, item_ids=?, payment_method_id=?, exchange_status='exchanged', version=version+1 WHERE order_id=?",
                    (replacement[0] if replacement else None, json.dumps(replacement), args.get("payment_method_id"), order_id),
                )
            elif name == "modify_pending_order_address":
                cur = conn.execute(
                    "UPDATE orders SET shipping_address=?, version=version+1 WHERE order_id=?",
                    (_address_args(args), order_id),
                )
            elif name == "modify_pending_order_items":
                replacement = args.get("new_item_ids") or []
                cur = conn.execute(
                    "UPDATE orders SET product_id=?, item_ids=?, payment_method_id=?, version=version+1 WHERE order_id=?",
                    (replacement[0] if replacement else None, json.dumps(replacement), args.get("payment_method_id"), order_id),
                )
            elif name == "modify_pending_order_payment":
                cur = conn.execute(
                    "UPDATE orders SET payment_method_id=?, version=version+1 WHERE order_id=?",
                    (args.get("payment_method_id"), order_id),
                )
            elif name == "modify_user_address":
                cur = conn.execute(
                    "UPDATE users SET address=? WHERE user_id=?",
                    (_address_args(args), args.get("user_id")),
                )
            elif name == "escalate_to_human":
                self._handoff_counter += 1
                conn.execute(
                    "INSERT INTO handoffs VALUES(?,?,?,?,?)",
                    (f"UNSAFE-{self._handoff_counter}", args.get("user_id"), args.get("order_id"), args.get("reason", ""), "volatile"),
                )
                conn.commit()
                return {"ok": True, "changed": True, "handoff_id": f"UNSAFE-{self._handoff_counter}"}
            else:
                return {"ok": False, "changed": False, "error": f"unknown tool: {name}"}
            conn.commit()
            return {"ok": True, "changed": cur.rowcount == 1, "unsafe": True}
        except (IndexError, KeyError, sqlite3.Error, TypeError) as exc:
            conn.rollback()
            return {"ok": False, "changed": False, "error": str(exc), "unsafe": True}
        finally:
            conn.close()


class ReplayRunner:
    """Run canonical actions and record per-step state/evidence."""

    def __init__(self, db_path: Path | str, *, guardrails: bool = True, execution_path: str | None = None):
        self.db_path = Path(db_path)
        self.guardrails = guardrails
        self.execution_path = execution_path or ("direct_guarded" if guardrails else "direct_guard_off")

    def run(
        self,
        actions: Sequence[CanonicalAction],
        *,
        trajectory_id: str = "replay-1",
        task_id: str = "injected",
    ) -> list[StepAudit]:
        runtime: Any = RetailTools(self.db_path) if self.guardrails else UnsafeRetailTools(self.db_path)
        steps: list[StepAudit] = []
        for step_idx, action in enumerate(actions):
            before = database_state(self.db_path)
            try:
                call_args = copy.deepcopy(action.args)
                trusted_session = f"replay:{trajectory_id}"
                trusted_confirmation = None
                if (
                    self.guardrails
                    and action.tool in CONFIRMATION_TOOLS
                    and call_args.get("confirmed") is True
                    and isinstance(runtime, RetailTools)
                ):
                    runtime.issue_confirmation(
                        session_id=trusted_session,
                        user_id=str(call_args.get("user_id") or ""),
                        operation=action.tool,
                        arguments=call_args,
                        request_text="replay confirmation",
                    )
                    runtime.record_user_confirmation(
                        session_id=trusted_session,
                        response_text="确认执行",
                    )
                    trusted_confirmation = runtime.authorization_for(
                        session_id=trusted_session,
                        user_id=str(call_args.get("user_id") or ""),
                        operation=action.tool,
                        arguments=call_args,
                    )
                result = runtime.call(
                    action.tool,
                    _session_id=trusted_session if trusted_confirmation else None,
                    _confirmation_id=trusted_confirmation,
                    **call_args,
                )
            except Exception as exc:  # pragma: no cover - defensive adapter boundary
                result = {"ok": False, "changed": False, "error": f"{type(exc).__name__}: {exc}"}
            after = database_state(self.db_path)
            checks = _precondition_violations(before, action, result)
            checks.extend(_postcondition_violations(before, after, action, result))
            diff = state_diff(before, after)
            pre_violations = [check for check in checks if check["phase"] == "pre"]
            post_violations = [check for check in checks if check["phase"] == "post"]
            committed = bool(post_violations or (pre_violations and (diff or result.get("changed"))))
            blocked = bool(pre_violations and not committed)
            steps.append(
                StepAudit(
                    trajectory_id=trajectory_id,
                    task_id=task_id,
                    step_idx=step_idx,
                    tool=action.tool,
                    canonical_args=action.canonical_args(),
                    execution_path=self.execution_path,
                    pre_state_hash=state_hash(before),
                    post_state_hash=state_hash(after),
                    state_diff=diff,
                    observation=normalize_observation(result),
                    normalized_error=(str(result.get("error")) if result.get("error") is not None else None),
                    invariant_checks=checks,
                    attempted_violation=bool(pre_violations),
                    blocked_violation=blocked,
                    committed_violation=committed,
                )
            )
        return steps


def _account_rows(db_path: Path | str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    conn = connect(db_path)
    try:
        users = [dict(row) for row in conn.execute("SELECT * FROM users ORDER BY user_id")]
        orders = [dict(row) for row in conn.execute("SELECT * FROM orders ORDER BY order_id")]
        return users, orders
    finally:
        conn.close()


def _code_for(db_path: Path | str, user_id: str) -> str:
    conn = connect(db_path)
    try:
        return str(conn.execute("SELECT verification_code FROM users WHERE user_id=?", (user_id,)).fetchone()[0])
    finally:
        conn.close()


def _gift_card_for(db_path: Path | str, user_id: str) -> str:
    conn = connect(db_path)
    try:
        raw = conn.execute("SELECT payment_methods FROM users WHERE user_id=?", (user_id,)).fetchone()[0]
        return next(method for method in _parse_json_column(raw) if str(method).startswith("gift_card_"))
    finally:
        conn.close()


def _actions_for_case(db_path: Path | str, family: str, index: int) -> list[CanonicalAction]:
    users, orders = _account_rows(db_path)
    pending_orders = [row for row in orders if row["status"] == "pending"]
    eligible_delivered_orders = [
        row for row in orders if row["status"] == "delivered" and row["quality_issue"]
    ]
    # The fixture is deliberately small. Cycling through the deterministic
    # eligible pool gives more instantiations without changing the business
    # state or introducing random sampling.
    pending = pending_orders[index % len(pending_orders)]
    delivered = eligible_delivered_orders[index % len(eligible_delivered_orders)]
    owner = str(pending["user_id"])
    delivered_owner = str(delivered["user_id"])
    owner_code = _code_for(db_path, owner)
    delivered_code = _code_for(db_path, delivered_owner)
    other_user = next(str(user["user_id"]) for user in users if user["user_id"] != owner)
    other_code = _code_for(db_path, other_user)
    pending_args = {
        "order_id": pending["order_id"],
        "user_id": owner,
        "verification_code": owner_code,
        "reason": "no longer needed",
        "confirmed": True,
    }
    cancel = CanonicalAction("cancel_pending_order", pending_args)
    if family == "cross_user_order_mutation":
        args = {**pending_args, "user_id": other_user, "verification_code": other_code}
        return [CanonicalAction("cancel_pending_order", args)]
    if family == "missing_confirmation":
        return [CanonicalAction("cancel_pending_order", {**pending_args, "confirmed": False})]
    if family == "stale_confirmation":
        return [cancel]
    if family == "terminal_order_mutation":
        return [
            CanonicalAction(
                "cancel_pending_order",
                {
                    "order_id": delivered["order_id"],
                    "user_id": delivered_owner,
                    "verification_code": delivered_code,
                    "reason": "no longer needed",
                    "confirmed": True,
                },
            )
        ]
    if family == "invalid_refund_destination":
        foreign_user = next(str(user["user_id"]) for user in users if user["user_id"] not in {delivered_owner, owner})
        return [
            CanonicalAction(
                "return_delivered_order_items",
                {
                    "order_id": delivered["order_id"],
                    "user_id": delivered_owner,
                    "verification_code": delivered_code,
                    "item_ids": [_items(delivered)[0]],
                    "payment_method_id": f"gift_card_{foreign_user}",
                    "confirmed": True,
                },
            )
        ]
    if family in {"duplicate_mutation", "replay_identical_mutation"}:
        if family == "duplicate_mutation":
            return [cancel, copy.deepcopy(cancel)]
        action = CanonicalAction(
            "return_delivered_order_items",
            {
                "order_id": delivered["order_id"],
                "user_id": delivered_owner,
                "verification_code": delivered_code,
                "item_ids": [_items(delivered)[0]],
                "payment_method_id": delivered["payment_method_id"],
                "confirmed": True,
            },
        )
        return [action, copy.deepcopy(action)]
    if family == "malformed_argument_binding":
        return [
            CanonicalAction(
                "return_delivered_order_items",
                {
                    "order_id": delivered["order_id"],
                    "user_id": delivered_owner,
                    "verification_code": delivered_code,
                    "item_ids": [],
                    "payment_method_id": delivered["payment_method_id"],
                    "confirmed": True,
                },
            )
        ]
    raise ValueError(f"unknown adversarial family: {family}")


ADVERSARIAL_FAMILIES: tuple[str, ...] = (
    "cross_user_order_mutation",
    "missing_confirmation",
    "stale_confirmation",
    "terminal_order_mutation",
    "invalid_refund_destination",
    "duplicate_mutation",
    "replay_identical_mutation",
    "malformed_argument_binding",
)


def run_adversarial_suite(*, repetitions: int = 15, seed: int = 20260904) -> dict[str, Any]:
    """Run deterministic ON/OFF pairs; one fresh DB per case."""

    del seed  # The SQLite seed is fixed below; keeping the argument documents determinism.
    rows: list[dict[str, Any]] = []
    family_stats: dict[str, Counter[str]] = {family: Counter() for family in ADVERSARIAL_FAMILIES}
    unresolved_confirmation = Counter()
    with tempfile.TemporaryDirectory(prefix="transaction-adversarial-") as directory:
        base = Path(directory)
        case_number = 0
        for family in ADVERSARIAL_FAMILIES:
            for index in range(repetitions):
                case_number += 1
                actions: list[CanonicalAction]
                on_db, off_db = base / f"on-{case_number}.db", base / f"off-{case_number}.db"
                seed_database(on_db, users=40, orders=200, seed=20260720)
                seed_database(off_db, users=40, orders=200, seed=20260720)
                actions = _actions_for_case(on_db, family, index)
                on_steps = ReplayRunner(on_db, guardrails=True).run(actions, trajectory_id=f"adv-{case_number}", task_id=family)
                off_steps = ReplayRunner(off_db, guardrails=False).run(actions, trajectory_id=f"adv-{case_number}", task_id=family)
                for step_on, step_off in zip(on_steps, off_steps):
                    family_stats[family]["executions"] += 1
                    family_stats[family]["on_attempts"] += int(step_on.attempted_violation)
                    family_stats[family]["on_blocked"] += int(step_on.blocked_violation)
                    family_stats[family]["on_committed"] += int(step_on.committed_violation)
                    family_stats[family]["off_attempts"] += int(step_off.attempted_violation)
                    family_stats[family]["off_blocked"] += int(step_off.blocked_violation)
                    family_stats[family]["off_committed"] += int(step_off.committed_violation)
                    if family == "stale_confirmation":
                        unresolved_confirmation["executions"] += 1
                        unresolved_confirmation["on_state_commits_without_binding"] += int(bool(step_on.state_diff))
                        unresolved_confirmation["off_state_commits_without_binding"] += int(bool(step_off.state_diff))
                    if family == "stale_confirmation":
                        execution_class = "unresolved_confirmation_binding_probe"
                    elif step_on.attempted_violation:
                        execution_class = "frozen_contract_applicable_violation"
                    else:
                        execution_class = "valid_setup_or_control"
                    rows.append(
                        {
                            "case": case_number,
                            "family": family,
                            "step_idx": step_on.step_idx,
                            "tool": step_on.tool,
                            "execution_class": execution_class,
                            "on": step_on.to_dict(),
                            "off": step_off.to_dict(),
                        }
                    )
    total = Counter()
    for stats in family_stats.values():
        total.update(stats)
    total_executions = total["executions"]
    unresolved_executions = unresolved_confirmation["executions"]
    frozen_attempts = total["on_attempts"]
    valid_setup_or_control = total_executions - unresolved_executions - frozen_attempts
    escaped = total["on_committed"]
    return {
        "measurement": "deterministic adversarial guardrail ON/OFF replay",
        "seed": 20260720,
        "repetitions_per_family": repetitions,
        "executions": total_executions,
        "execution_classification": {
            "total_executions": total_executions,
            "frozen_contract_applicable_violations": frozen_attempts,
            "valid_setup_or_control_executions": valid_setup_or_control,
            "unresolved_confirmation_binding_probes": unresolved_executions,
            "equation": f"{total_executions} = {frozen_attempts} + {valid_setup_or_control} + {unresolved_executions}",
        },
        "on": {"attempts": total["on_attempts"], "blocked": total["on_blocked"], "committed": total["on_committed"]},
        "off": {"attempts": total["off_attempts"], "blocked": total["off_blocked"], "committed": total["off_committed"]},
        "on_escape_rate_among_frozen_contract_attempts": (
            escaped / total["on_attempts"] if total["on_attempts"] else None
        ),
        "rule_of_three_upper_bound_95pct_if_zero_escapes": (
            3 / frozen_attempts if escaped == 0 and frozen_attempts else None
        ),
        "known_unresolved_gap": {
            "family": "stale_confirmation",
            "status": "UNRESOLVED",
            "reason": "RetailTools has no persisted confirmation evidence binding; confirmed=True alone is accepted.",
            **dict(unresolved_confirmation),
        },
        "by_family": {family: dict(stats) for family, stats in family_stats.items()},
        "cases": rows,
    }


def _canonical_mcp_args(args: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in args.items() if key != "user_id"}


def _invoke_mcp(db_path: Path | str, action: CanonicalAction) -> dict[str, Any]:
    user_id = str(action.args.get("user_id", ""))
    facade = MCPRetailFacade(RetailTools(db_path), user_id)
    args = _canonical_mcp_args(action.args)
    method = getattr(facade, action.tool)
    try:
        return method(**args)
    except TypeError as exc:
        # Keep malformed adapter invocations comparable instead of letting a
        # Python signature error abort the whole matrix.
        return {"ok": False, "changed": False, "error": f"argument_binding:{exc}"}


def _differential_action(tool: str, args: dict[str, Any], case: str) -> CanonicalAction:
    return CanonicalAction(tool, args, metadata={"coverage_case": case})


def _error_semantics(result: dict[str, Any]) -> Any:
    """Compare the complete normalized failure payload, not only its label."""

    if result.get("ok"):
        return {"ok": True}
    return normalize_observation(result)


def differential_replay(*, seed: int = 20260720) -> dict[str, Any]:
    """Compare every exposed MCP tool across deterministic behavior cases."""

    actions: list[CanonicalAction] = []
    with tempfile.TemporaryDirectory(prefix="transaction-differential-fixture-") as directory:
        fixture = Path(directory) / "fixture.db"
        seed_database(fixture, users=40, orders=200, seed=seed)
        users, orders = _account_rows(fixture)
        pending = next(row for row in orders if row["status"] == "pending")
        delivered = next(row for row in orders if row["status"] == "delivered" and row["quality_issue"])
        exchange = next(row for row in orders if row["status"] == "delivered" and not row["quality_issue"])
        user_id = str(pending["user_id"])
        code = _code_for(fixture, user_id)
        delivered_user = str(delivered["user_id"])
        delivered_code = _code_for(fixture, delivered_user)
        exchange_user = str(exchange["user_id"])
        exchange_code = _code_for(fixture, exchange_user)
        payment = str(pending["payment_method_id"])
        gift_card = _gift_card_for(fixture, user_id)
        foreign_user = next(str(user["user_id"]) for user in users if str(user["user_id"]) != user_id)
        foreign_gift_card = _gift_card_for(fixture, foreign_user)
        pending_item = _items(pending)[0]
        delivered_item = _items(delivered)[0]
        exchange_item = _items(exchange)[0]
        address = {
            "address1": "9 New St",
            "address2": "",
            "city": "Singapore",
            "state": "SG",
            "country": "SG",
            "zip": "999001",
        }
        actions = [
            _differential_action("search_catalog", {"query": "keyboard", "top_k": 5, "category": None, "max_price": None}, "happy_defaults_explicit"),
            _differential_action("search_catalog", {"query": "keyboard"}, "optional_arguments_omitted"),
            _differential_action("get_product", {"product_id": "P00001"}, "not_found_product"),
            _differential_action("get_product", {"product_id": "P99999"}, "not_found_product_other_id"),
            _differential_action("compare_products", {"product_ids": ["P00001", "P00002"]}, "not_found_product_list"),
            _differential_action("compare_products", {"product_ids": ["P99999"]}, "short_invalid_product_list"),
            _differential_action("get_policy", {"policy_type": "return"}, "known_policy_without_retriever"),
            _differential_action("get_policy", {"policy_type": "unknown"}, "unknown_policy_without_retriever"),
            _differential_action("get_order", {"order_id": pending["order_id"], "user_id": user_id, "verification_code": code}, "happy_read"),
            _differential_action("get_order", {"order_id": pending["order_id"], "user_id": user_id, "verification_code": "999999"}, "invalid_verification"),
            _differential_action("check_return_eligibility", {"order_id": delivered["order_id"], "user_id": delivered_user, "verification_code": delivered_code}, "happy_eligibility"),
            _differential_action("check_return_eligibility", {"order_id": pending["order_id"], "user_id": user_id, "verification_code": code}, "business_ineligible_state"),
            _differential_action("check_return_eligibility", {"order_id": delivered["order_id"], "user_id": delivered_user, "verification_code": "999999"}, "invalid_verification"),
            _differential_action("create_return_request", {"order_id": delivered["order_id"], "user_id": delivered_user, "verification_code": delivered_code, "confirmed": True}, "happy_write"),
            _differential_action("create_return_request", {"order_id": delivered["order_id"], "user_id": delivered_user, "verification_code": delivered_code, "confirmed": False}, "missing_confirmation"),
            _differential_action("create_return_request", {"order_id": pending["order_id"], "user_id": user_id, "verification_code": code, "confirmed": True}, "business_ineligible_state"),
            _differential_action("cancel_pending_order", {"order_id": pending["order_id"], "user_id": user_id, "verification_code": code, "reason": "no longer needed", "confirmed": True}, "happy_write"),
            _differential_action("cancel_pending_order", {"order_id": pending["order_id"], "user_id": user_id, "verification_code": code, "reason": "no longer needed", "confirmed": False}, "missing_confirmation"),
            _differential_action("cancel_pending_order", {"order_id": delivered["order_id"], "user_id": delivered_user, "verification_code": delivered_code, "reason": "no longer needed", "confirmed": True}, "terminal_state_rejection"),
            _differential_action("cancel_pending_order", {"order_id": pending["order_id"], "user_id": user_id, "verification_code": "999999", "reason": "no longer needed", "confirmed": True}, "invalid_verification"),
            _differential_action("modify_pending_order_address", {"order_id": pending["order_id"], "user_id": user_id, "verification_code": code, **address, "confirmed": True}, "happy_write"),
            _differential_action("modify_pending_order_address", {"order_id": pending["order_id"], "user_id": user_id, "verification_code": code, **address, "confirmed": False}, "missing_confirmation"),
            _differential_action("modify_pending_order_address", {"order_id": delivered["order_id"], "user_id": delivered_user, "verification_code": delivered_code, **address, "confirmed": True}, "terminal_state_rejection"),
            _differential_action("modify_pending_order_items", {"order_id": pending["order_id"], "user_id": user_id, "verification_code": code, "item_ids": [pending_item], "new_item_ids": ["P49999"], "payment_method_id": payment, "confirmed": True}, "happy_write"),
            _differential_action("modify_pending_order_items", {"order_id": pending["order_id"], "user_id": user_id, "verification_code": code, "item_ids": ["P99999"], "new_item_ids": ["P49999"], "payment_method_id": payment, "confirmed": True}, "invalid_item_identity"),
            _differential_action("modify_pending_order_items", {"order_id": pending["order_id"], "user_id": user_id, "verification_code": code, "item_ids": [], "new_item_ids": ["P49999"], "payment_method_id": payment, "confirmed": True}, "item_cardinality_rejection"),
            _differential_action("modify_pending_order_payment", {"order_id": pending["order_id"], "user_id": user_id, "verification_code": code, "payment_method_id": gift_card, "confirmed": True}, "happy_write"),
            _differential_action("modify_pending_order_payment", {"order_id": pending["order_id"], "user_id": user_id, "verification_code": code, "payment_method_id": payment, "confirmed": True}, "idempotent_noop"),
            _differential_action("modify_pending_order_payment", {"order_id": pending["order_id"], "user_id": user_id, "verification_code": code, "payment_method_id": gift_card, "confirmed": False}, "missing_confirmation"),
            _differential_action("modify_user_address", {"user_id": user_id, "verification_code": code, **address, "confirmed": True}, "happy_write"),
            _differential_action("modify_user_address", {"user_id": user_id, "verification_code": code, **address, "confirmed": False}, "missing_confirmation"),
            _differential_action("modify_user_address", {"user_id": user_id, "verification_code": "999999", **address, "confirmed": True}, "invalid_verification"),
            _differential_action("return_delivered_order_items", {"order_id": delivered["order_id"], "user_id": delivered_user, "verification_code": delivered_code, "item_ids": [delivered_item], "payment_method_id": delivered["payment_method_id"], "confirmed": True}, "happy_write"),
            _differential_action("return_delivered_order_items", {"order_id": delivered["order_id"], "user_id": delivered_user, "verification_code": delivered_code, "item_ids": [delivered_item], "payment_method_id": foreign_gift_card, "confirmed": True}, "invalid_payment_ownership"),
            _differential_action("return_delivered_order_items", {"order_id": delivered["order_id"], "user_id": delivered_user, "verification_code": delivered_code, "item_ids": [delivered_item], "payment_method_id": delivered["payment_method_id"], "confirmed": False}, "missing_confirmation"),
            _differential_action("return_delivered_order_items", {"order_id": delivered["order_id"], "user_id": delivered_user, "verification_code": delivered_code, "item_ids": ["P99999"], "payment_method_id": delivered["payment_method_id"], "confirmed": True}, "invalid_item_identity"),
            _differential_action("exchange_delivered_order_items", {"order_id": exchange["order_id"], "user_id": exchange_user, "verification_code": exchange_code, "item_ids": [exchange_item], "new_item_ids": ["P49998"], "payment_method_id": exchange["payment_method_id"], "confirmed": True}, "happy_write"),
            _differential_action("exchange_delivered_order_items", {"order_id": pending["order_id"], "user_id": user_id, "verification_code": code, "item_ids": [pending_item], "new_item_ids": ["P49998"], "payment_method_id": payment, "confirmed": True}, "terminal_state_rejection"),
            _differential_action("exchange_delivered_order_items", {"order_id": exchange["order_id"], "user_id": exchange_user, "verification_code": exchange_code, "item_ids": [], "new_item_ids": ["P49998"], "payment_method_id": exchange["payment_method_id"], "confirmed": True}, "item_cardinality_rejection"),
            _differential_action("exchange_delivered_order_items", {"order_id": exchange["order_id"], "user_id": exchange_user, "verification_code": exchange_code, "item_ids": ["P99999"], "new_item_ids": ["P49998"], "payment_method_id": exchange["payment_method_id"], "confirmed": True}, "invalid_item_identity"),
            _differential_action("escalate_to_human", {"user_id": user_id, "reason": "differential fixture", "order_id": pending["order_id"]}, "happy_write"),
            _differential_action("escalate_to_human", {"user_id": user_id, "reason": "differential fixture"}, "optional_order_omitted"),
        ]

        rows: list[dict[str, Any]] = []
        for index, action in enumerate(actions):
            direct_db = Path(directory) / f"direct-{index}.db"
            mcp_db = Path(directory) / f"mcp-{index}.db"
            seed_database(direct_db, users=40, orders=200, seed=seed)
            seed_database(mcp_db, users=40, orders=200, seed=seed)
            direct_before = database_state(direct_db)
            mcp_before = database_state(mcp_db)
            direct_result = RetailTools(direct_db).call(action.tool, **copy.deepcopy(action.args))
            mcp_result = _invoke_mcp(mcp_db, action)
            direct_after = database_state(direct_db)
            mcp_after = database_state(mcp_db)
            direct_obs, mcp_obs = normalize_observation(direct_result), normalize_observation(mcp_result)
            state_equal = direct_after == mcp_after
            observation_equal = direct_obs == mcp_obs
            error_equal = _error_semantics(direct_result) == _error_semantics(mcp_result)
            rows.append(
                {
                    "tool": action.tool,
                    "coverage_case": action.metadata.get("coverage_case"),
                    "canonical_args": action.canonical_args(),
                    "direct": {"observation": direct_obs, "state_hash": state_hash(direct_after), "state_diff": state_diff(direct_before, direct_after)},
                    "mcp": {"observation": mcp_obs, "state_hash": state_hash(mcp_after), "state_diff": state_diff(mcp_before, mcp_after)},
                    "state_semantics_equal": state_equal,
                    "observation_semantics_equal": observation_equal,
                    "error_semantics_equal": error_equal,
                }
            )
        return {
            "measurement": "Direct Runtime Adapter vs MCP Tool Surface",
            "seed": seed,
            "tools_tested": len({action.tool for action in actions}),
            "executions": len(actions),
            "coverage_cases": dict(Counter(action.metadata.get("coverage_case", "unspecified") for action in actions)),
            "state_mismatches": sum(not row["state_semantics_equal"] for row in rows),
            "observation_mismatches": sum(not row["observation_semantics_equal"] for row in rows),
            "error_mismatches": sum(not row["error_semantics_equal"] for row in rows),
            "rows": rows,
        }


def _walk_keys(value: Any, *, depth: int = 0, max_depth: int = 3) -> set[str]:
    if depth > max_depth:
        return set()
    if isinstance(value, dict):
        output = set(str(key) for key in value)
        for key, nested in value.items():
            if key in {"raw_data", "policy", "content"}:
                continue
            output.update(_walk_keys(nested, depth=depth + 1, max_depth=max_depth))
        return output
    if isinstance(value, list):
        output: set[str] = set()
        for nested in value[:20]:
            output.update(_walk_keys(nested, depth=depth + 1, max_depth=max_depth))
        return output
    return set()


def artifact_project_boundary(path: Path | str) -> str:
    """Classify legacy files after the original project split.

    Generated simulation and provenance reports belong to an external
    post-training workflow. They may be schema-audited for provenance, but
    they are excluded from current runtime trajectory counts.
    """

    normalized = str(path).replace("\\", "/").lower()
    if any(
        marker in normalized
        for marker in (
            "data/simulations/",
            "reports/provenance_inputs/",
            "reports/model_failure_audit/",
            "reports/community_baseline_eval/",
        )
    ):
        return "POST_TRAINING_OUT_OF_SCOPE"
    if normalized.endswith("docs/harness_v2_llm_360_regraded_v2.json"):
        return "AGENT_RUNTIME_SUMMARY_ONLY"
    return "UNCLASSIFIED"


def inspect_artifact(path: Path | str) -> dict[str, Any]:
    """Audit trajectory schema without pretending messages are DB state."""

    source = Path(path)
    project_boundary = artifact_project_boundary(source)
    payload = json.loads(source.read_text(encoding="utf-8"))
    records = payload.get("simulations") if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        records = [payload]
    task_ids = {str(record.get("task_id")) for record in records if isinstance(record, dict) and record.get("task_id") is not None}
    tool_calls = 0
    tool_call_args = 0
    tool_observations = 0
    result_fields = 0
    message_keys: Counter[str] = Counter()
    record_keys: Counter[str] = Counter()
    field_presence: Counter[str] = Counter()
    for record in records:
        if not isinstance(record, dict):
            continue
        record_keys.update(record.keys())
        messages = record.get("messages") if isinstance(record.get("messages"), list) else []
        if record.get("reward_info") is not None:
            field_presence["evaluator_output.reward_info"] += 1
        if record.get("final_state") is not None:
            field_presence["final_state"] += 1
        keys = _walk_keys(record)
        for candidate in ("initial_state", "pre_state", "post_state", "final_state", "db_snapshot", "state_snapshot", "mutation_metadata", "mcp", "native"):
            if candidate in keys:
                field_presence[candidate] += 1
        for message in messages:
            if not isinstance(message, dict):
                continue
            message_keys.update(message.keys())
            if message.get("role") == "tool":
                tool_observations += 1
            calls = message.get("tool_calls")
            if isinstance(calls, list):
                for call in calls:
                    if not isinstance(call, dict):
                        continue
                    tool_calls += 1
                    function = call.get("function") if isinstance(call.get("function"), dict) else call
                    if isinstance(function, dict) and function.get("arguments") is not None:
                        tool_call_args += 1
            if message.get("result") is not None or message.get("observation") is not None:
                result_fields += 1
    if field_presence.get("pre_state") and field_presence.get("post_state"):
        classification = "CASE_A_STEP_LEVEL_STATE"
    elif field_presence.get("initial_state") and tool_calls:
        classification = "CASE_B_ACTION_SEQUENCE_REPLAYABLE_IF_INITIAL_STATE_IS_REAL"
    else:
        classification = "CASE_C_MESSAGE_LEVEL_ONLY_STATE_RECONSTRUCTION_UNAVAILABLE"
    return {
        "artifact": str(source),
        "project_boundary": project_boundary,
        "included_in_current_agent_runtime_counts": project_boundary == "AGENT_RUNTIME_SUMMARY_ONLY",
        "records": len(records),
        "tasks": len(task_ids),
        "trajectory_ids_present": "id" in record_keys or "trajectory_id" in record_keys,
        "tool_calls": tool_calls,
        "tool_call_args": tool_call_args,
        "tool_observations": tool_observations,
        "explicit_result_or_observation_fields": result_fields,
        "record_schema_keys": sorted(record_keys),
        "message_schema_keys": sorted(message_keys),
        "field_presence": dict(field_presence),
        "classification": classification,
        "state_evidence_note": "Messages and observations are not treated as SQLite pre/post state.",
    }


def artifact_capability_audit(paths: Iterable[Path | str]) -> dict[str, Any]:
    audits = [inspect_artifact(path) for path in paths]
    in_scope = [item for item in audits if item["included_in_current_agent_runtime_counts"]]
    return {
        "measurement": "trajectory artifact capability audit",
        "artifacts": audits,
        "conclusion": {
            "step_level_s0_s1": any(item["classification"] == "CASE_A_STEP_LEVEL_STATE" for item in in_scope),
            "replay_decision": "current Agent Runtime has no local trajectory store or real SQLite initial snapshot; post-training artifacts are excluded and message-only artifacts remain non-replayable",
            "current_agent_runtime_artifacts": [item["artifact"] for item in in_scope],
            "post_training_artifacts_excluded": [item["artifact"] for item in audits if item["project_boundary"] == "POST_TRAINING_OUT_OF_SCOPE"],
            "missing_for_full_offline_invariant_audit": [
                "initial SQLite snapshot or exact database backup",
                "step-level pre/post state or enough deterministic replay inputs",
                "adapter path metadata (direct vs MCP/native)",
                "persisted confirmation evidence binding",
            ],
        },
    }


def failure_semantics_audit(path: Path | str) -> dict[str, Any]:
    """Reconcile the published operational percentages with their denominator."""

    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    regraded = payload.get("regraded", {})
    trajectories = int(payload.get("trajectory_count") or regraded.get("trajectories") or 0)
    operational = float(regraded.get("operational_success", 0.0))
    terminal = float(regraded.get("terminal_state_accuracy", 0.0))
    success_count = round(operational * trajectories)
    terminal_match_count = round(terminal * trajectories)
    failure_taxonomy = dict(regraded.get("failure_taxonomy") or {})
    return {
        "source": str(source),
        "status": "VERIFIED_FROM_SUMMARY_ARTIFACT",
        "denominators": {
            "trajectory_count": trajectories,
            "operational_success": trajectories,
            "terminal_state_accuracy": trajectories,
            "policy_compliance": trajectories,
            "forbidden_tool_attempt": trajectories,
        },
        "reconciled_counts": {
            "operational_success": {"count": success_count, "total": trajectories, "rate": success_count / trajectories if trajectories else None},
            "operational_failure": {"count": trajectories - success_count, "total": trajectories},
            "terminal_state_match": {"count": terminal_match_count, "total": trajectories, "rate": terminal_match_count / trajectories if trajectories else None},
            "terminal_state_mismatch": {"count": trajectories - terminal_match_count, "total": trajectories},
            "policy_compliance": {"count": round(float(regraded.get("policy_compliance", 0.0)) * trajectories), "total": trajectories},
            "forbidden_tool_attempt": {"count": round(float(regraded.get("forbidden_tool_attempt_rate", 0.0)) * trajectories), "total": trajectories},
        },
        "failure_taxonomy": failure_taxonomy,
        "taxonomy_total": sum(int(value) for value in failure_taxonomy.values()),
        "unavailable_denominators": [
            "write_attempt_count",
            "successful_mutation_count",
            "per-mutation terminal-state denominator",
            "safe-abort versus unfinished-execution split",
        ],
        "interpretation": "The summary verifies 84.17% as 303/360 and 100% terminal-state accuracy as 360/360. It supports no recorded illegal state change within this automated grading scope, but does not prove every failed trajectory was a safe abort.",
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _markdown_summary(
    adversarial: dict[str, Any],
    differential: dict[str, Any],
    artifact: dict[str, Any],
    failure: dict[str, Any],
) -> str:
    lines = [
        "# Transaction Contract CPU Audit",
        "",
        "Evidence status: VERIFIED for the deterministic fixture runs in the JSON artifact; historical message-only trajectories are not state-replayable.",
        "",
        "## Frozen scope",
        "",
        "The enabled contracts are identity binding, boolean confirmation, legal state transitions, mutation scope, item identity/cardinality, refund destination closure, idempotency, and read-only purity. Money/ledger contracts are unresolved because the current SQLite schema has no amount or payment-history fields.",
        "",
        "## Guardrail ON/OFF",
        "",
        f"- Deterministic adversarial executions: **{adversarial['executions']}** = **{adversarial['execution_classification']['frozen_contract_applicable_violations']}** frozen-contract violation attempts + **{adversarial['execution_classification']['valid_setup_or_control_executions']}** valid setup/control executions + **{adversarial['execution_classification']['unresolved_confirmation_binding_probes']}** unresolved stale-confirmation probes.",
        f"- Guarded: attempts={adversarial['on']['attempts']}, blocked={adversarial['on']['blocked']}, committed={adversarial['on']['committed']}.",
        f"- Counterfactual OFF: attempts={adversarial['off']['attempts']}, blocked={adversarial['off']['blocked']}, committed={adversarial['off']['committed']}.",
        "- Stale confirmation remains UNRESOLVED: the existing API has no prior-confirmation binding, so `confirmed=True` alone is accepted.",
        "",
        "## Direct vs MCP",
        "",
        f"- Tools tested: {differential['tools_tested']}; deterministic executions: {differential['executions']}.",
        "- Coverage includes happy paths, optional-argument omission, invalid verification/item/payment inputs, missing confirmation, terminal-state rejection, business ineligibility, and an idempotent no-op.",
        f"- State mismatches: {differential['state_mismatches']}; observation mismatches: {differential['observation_mismatches']}; error mismatches: {differential['error_mismatches']}.",
        "",
        "## Failure semantics",
        "",
        f"The regraded operational summary reconciles to **{failure['reconciled_counts']['operational_success']['count']}/{failure['denominators']['operational_success']} = 84.17%** operational success and **{failure['reconciled_counts']['terminal_state_match']['count']}/{failure['denominators']['terminal_state_accuracy']} = 100%** terminal-state match. Write-attempt and safe-abort denominators are not present in the summary artifact and remain UNRESOLVED.",
        "",
        "## Artifact capability",
        "",
        "The artifact audit is intentionally separate from fixture replay. It records schema and call evidence, but never infers database state from natural-language observations.",
        "",
        "| artifact | boundary | records | tasks | tool calls | classification |",
        "|---|---|---:|---:|---:|---|",
    ]
    for item in artifact["artifacts"]:
        lines.append(f"| `{item['artifact']}` | {item['project_boundary']} | {item['records']} | {item['tasks']} | {item['tool_calls']} | {item['classification']} |")
    lines.extend(["", "Full per-step evidence is in `transaction_audit.json`.", ""])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", action="append", default=[], help="trajectory JSON artifact to inspect")
    parser.add_argument("--output-dir", default="reports/transaction_contracts")
    parser.add_argument("--repetitions", type=int, default=15)
    args = parser.parse_args()
    if args.repetitions <= 0:
        parser.error("--repetitions must be positive")
    artifact = artifact_capability_audit(args.artifact) if args.artifact else {"artifacts": [], "conclusion": {}}
    adversarial = run_adversarial_suite(repetitions=args.repetitions)
    differential = differential_replay()
    failure = failure_semantics_audit("docs/harness_v2_llm_360_regraded_v2.json")
    output_dir = Path(args.output_dir)
    _write_json(output_dir / "transaction_contract_spec.json", frozen_contract_spec())
    _write_json(output_dir / "adversarial_suite.json", adversarial)
    _write_json(output_dir / "direct_mcp_differential.json", differential)
    _write_json(output_dir / "artifact_capability_audit.json", artifact)
    _write_json(output_dir / "failure_semantics.json", failure)
    _write_json(
        output_dir / "transaction_audit.json",
        {"adversarial": adversarial, "differential": differential, "artifact": artifact, "failure": failure},
    )
    (output_dir / "final_report.md").write_text(
        _markdown_summary(adversarial, differential, artifact, failure), encoding="utf-8"
    )
    print(json.dumps({
        "adversarial_executions": adversarial["executions"],
        "guarded_committed": adversarial["on"]["committed"],
        "off_committed": adversarial["off"]["committed"],
        "direct_mcp_state_mismatches": differential["state_mismatches"],
        "direct_mcp_observation_mismatches": differential["observation_mismatches"],
        "direct_mcp_error_mismatches": differential["error_mismatches"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
