"""Guardrails and action-space alignment for compiler write tools in RetailTools."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from ecommerce_rag.orders import connect, seed_database
from ecommerce_rag.retail_protocol import RETAIL_WRITE_TOOLS
from ecommerce_rag.tool_schema import IDENTITY_TOOLS, SCHEMA_BY_NAME, TOOL_SCHEMAS
from ecommerce_rag.tools import IDENTITY_GUARDED_TOOLS, WRITE_TOOLS, RetailTools


def _pending(db: Path):
    conn = connect(db)
    try:
        order = dict(conn.execute("SELECT * FROM orders WHERE status='pending' LIMIT 1").fetchone())
        code = conn.execute(
            "SELECT verification_code FROM users WHERE user_id=?", (order["user_id"],)
        ).fetchone()[0]
        return order, code
    finally:
        conn.close()


def _delivered_eligible(db: Path):
    conn = connect(db)
    try:
        order = dict(conn.execute(
            "SELECT * FROM orders WHERE status='delivered' AND quality_issue=1 LIMIT 1"
        ).fetchone())
        code = conn.execute(
            "SELECT verification_code FROM users WHERE user_id=?", (order["user_id"],)
        ).fetchone()[0]
        return order, code
    finally:
        conn.close()


def _user_row(db: Path, user_id: str):
    conn = connect(db)
    try:
        return dict(conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone())
    finally:
        conn.close()


def _call(tools: RetailTools, name: str, **arguments):
    """Exercise writes through the same trusted confirmation boundary as production."""

    if arguments.get("confirmed") is True:
        session_id = f"test:{name}:{len(tools.calls)}"
        user_id = str(arguments.get("user_id") or "")
        tools.issue_confirmation(
            session_id=session_id,
            user_id=user_id,
            operation=name,
            arguments=arguments,
            request_text=f"确认执行 {name}",
        )
        assert tools.record_user_confirmation(
            session_id=session_id, response_text="确认执行"
        )["decision"] is True
        confirmation_id = tools.authorization_for(
            session_id=session_id,
            user_id=user_id,
            operation=name,
            arguments=arguments,
        )
        return tools.call(
            name,
            _session_id=session_id,
            _confirmation_id=confirmation_id,
            **arguments,
        )
    return tools.call(name, **arguments)


def test_compiler_write_tools_are_executable_on_retail_tools():
    tools = RetailTools(":memory:")
    missing = sorted(RETAIL_WRITE_TOOLS - tools.executable_tool_names())
    assert missing == [], missing
    assert RETAIL_WRITE_TOOLS <= WRITE_TOOLS
    for name in RETAIL_WRITE_TOOLS:
        assert name in SCHEMA_BY_NAME
        assert name in IDENTITY_TOOLS
        assert name in IDENTITY_GUARDED_TOOLS


def test_tool_schemas_cover_full_write_surface():
    schema_names = {schema["name"] for schema in TOOL_SCHEMAS}
    assert WRITE_TOOLS <= schema_names
    assert RETAIL_WRITE_TOOLS <= schema_names


def test_cancel_requires_confirmation_and_blocks_non_pending():
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "retail.db"
        seed_database(db, users=20, orders=100)
        tools = RetailTools(db)
        pending, code = _pending(db)
        blocked = _call(tools,
            "cancel_pending_order",
            order_id=pending["order_id"],
            user_id=pending["user_id"],
            verification_code=code,
            reason="no longer needed",
            confirmed=False,
        )
        assert blocked == {
            "ok": False,
            "changed": False,
            "error": "confirmation_required",
            "order_id": pending["order_id"],
        }
        ok = _call(tools,
            "cancel_pending_order",
            order_id=pending["order_id"],
            user_id=pending["user_id"],
            verification_code=code,
            reason="no longer needed",
            confirmed=True,
        )
        assert ok["ok"] and ok["changed"] and ok["status"] == "cancelled"
        again = _call(tools,
            "cancel_pending_order",
            order_id=pending["order_id"],
            user_id=pending["user_id"],
            verification_code=code,
            reason="no longer needed",
            confirmed=True,
        )
        assert again["ok"] and again["idempotent_replay"] and again["changed"] is False

        delivered, dcode = _delivered_eligible(db)
        refused = _call(tools,
            "cancel_pending_order",
            order_id=delivered["order_id"],
            user_id=delivered["user_id"],
            verification_code=dcode,
            reason="ordered by mistake",
            confirmed=True,
        )
        assert refused["error"] == "order_not_pending"
        assert refused["changed"] is False


def test_modify_address_payment_items_guards():
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "retail.db"
        seed_database(db, users=20, orders=100)
        tools = RetailTools(db)
        pending, code = _pending(db)
        user = _user_row(db, pending["user_id"])
        payment = json.loads(user["payment_methods"])[0]
        alt_payment = json.loads(user["payment_methods"])[1]
        # Seed defaults to credit_card_*; switch onto the other method so the write is real.
        target_payment = payment if pending.get("payment_method_id") != payment else alt_payment

        no_confirm = _call(tools,
            "modify_pending_order_address",
            order_id=pending["order_id"],
            user_id=pending["user_id"],
            verification_code=code,
            address1="9 New St",
            address2="",
            city="Singapore",
            state="SG",
            country="SG",
            zip="999001",
            confirmed=False,
        )
        assert no_confirm["error"] == "confirmation_required"

        addr = _call(tools,
            "modify_pending_order_address",
            order_id=pending["order_id"],
            user_id=pending["user_id"],
            verification_code=code,
            address1="9 New St",
            address2="",
            city="Singapore",
            state="SG",
            country="SG",
            zip="999001",
            confirmed=True,
        )
        assert addr["ok"] and addr["changed"]

        pay = _call(tools,
            "modify_pending_order_payment",
            order_id=pending["order_id"],
            user_id=pending["user_id"],
            verification_code=code,
            payment_method_id=target_payment,
            confirmed=True,
        )
        assert pay["ok"] and pay["changed"] and pay["payment_method_id"] == target_payment

        items = _call(tools,
            "modify_pending_order_items",
            order_id=pending["order_id"],
            user_id=pending["user_id"],
            verification_code=code,
            item_ids=[pending["product_id"]],
            new_item_ids=["P99999"],
            payment_method_id=target_payment,
            confirmed=True,
        )
        assert items["ok"] and items["changed"] and items["product_id"] == "P99999"

        delivered, dcode = _delivered_eligible(db)
        blocked = _call(tools,
            "modify_pending_order_payment",
            order_id=delivered["order_id"],
            user_id=delivered["user_id"],
            verification_code=dcode,
            payment_method_id=json.loads(_user_row(db, delivered["user_id"])["payment_methods"])[0],
            confirmed=True,
        )
        assert blocked["error"] == "order_not_pending"


def test_return_and_exchange_delivered_guards():
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "retail.db"
        seed_database(db, users=40, orders=200)
        tools = RetailTools(db)
        delivered, code = _delivered_eligible(db)
        payment = delivered["payment_method_id"] or json.loads(
            _user_row(db, delivered["user_id"])["payment_methods"]
        )[0]

        pending, pcode = _pending(db)
        wrong_state = _call(tools,
            "exchange_delivered_order_items",
            order_id=pending["order_id"],
            user_id=pending["user_id"],
            verification_code=pcode,
            item_ids=[pending["product_id"]],
            new_item_ids=["P88888"],
            payment_method_id=json.loads(_user_row(db, pending["user_id"])["payment_methods"])[0],
            confirmed=True,
        )
        assert wrong_state["error"] == "order_not_delivered"

        returned = _call(tools,
            "return_delivered_order_items",
            order_id=delivered["order_id"],
            user_id=delivered["user_id"],
            verification_code=code,
            item_ids=[delivered["product_id"]],
            payment_method_id=payment,
            confirmed=True,
        )
        assert returned["ok"] and returned["changed"] and returned["return_status"] == "requested"
        again = _call(tools,
            "return_delivered_order_items",
            order_id=delivered["order_id"],
            user_id=delivered["user_id"],
            verification_code=code,
            item_ids=[delivered["product_id"]],
            payment_method_id=payment,
            confirmed=True,
        )
        assert again["ok"] and again["idempotent_replay"]

        # Fresh delivered order for exchange (not the one already returned).
        conn = connect(db)
        try:
            exchange_order = dict(conn.execute(
                "SELECT * FROM orders WHERE status='delivered' AND quality_issue=0 "
                "AND return_status IS NULL AND exchange_status IS NULL LIMIT 1"
            ).fetchone())
            exchange_code = conn.execute(
                "SELECT verification_code FROM users WHERE user_id=?",
                (exchange_order["user_id"],),
            ).fetchone()[0]
        finally:
            conn.close()
        pay = exchange_order["payment_method_id"] or json.loads(
            _user_row(db, exchange_order["user_id"])["payment_methods"]
        )[0]
        exchanged = _call(tools,
            "exchange_delivered_order_items",
            order_id=exchange_order["order_id"],
            user_id=exchange_order["user_id"],
            verification_code=exchange_code,
            item_ids=[exchange_order["product_id"]],
            new_item_ids=["P77777"],
            payment_method_id=pay,
            confirmed=True,
        )
        assert exchanged["ok"] and exchanged["changed"] and exchanged["exchange_status"] == "exchanged"


def test_refund_payment_method_invariant_allows_original_or_existing_gift_card_only():
    cases = ("original", "gift_card", "other_user_payment_method", "foreign_gift_card")
    for case in cases:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "retail.db"
            seed_database(db, users=40, orders=200)
            tools = RetailTools(db)
            delivered, code = _delivered_eligible(db)
            user = _user_row(db, delivered["user_id"])
            methods = json.loads(user["payment_methods"])
            gift_card = next(method for method in methods if method.startswith("gift_card_"))
            other_payment = f"debit_card_{delivered['user_id']}"
            foreign_user_id = "U0001" if delivered["user_id"] != "U0001" else "U0002"
            foreign_gift_card = f"gift_card_{foreign_user_id}"
            if case == "other_user_payment_method":
                conn = connect(db)
                try:
                    conn.execute(
                        "UPDATE users SET payment_methods=? WHERE user_id=?",
                        (json.dumps(methods + [other_payment]), delivered["user_id"]),
                    )
                    conn.commit()
                finally:
                    conn.close()
            payment_method_id = {
                "original": delivered["payment_method_id"],
                "gift_card": gift_card,
                "other_user_payment_method": other_payment,
                "foreign_gift_card": foreign_gift_card,
            }[case]
            result = _call(tools,
                "return_delivered_order_items",
                order_id=delivered["order_id"],
                user_id=delivered["user_id"],
                verification_code=code,
                item_ids=[delivered["product_id"]],
                payment_method_id=payment_method_id,
                confirmed=True,
            )
            if case in ("other_user_payment_method", "foreign_gift_card"):
                assert result["error"] == "payment_method_not_found"
                assert result["changed"] is False
            else:
                assert result["ok"] and result["changed"]


def test_modify_user_address_requires_confirmation_and_is_idempotent():
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "retail.db"
        seed_database(db, users=8, orders=20)
        tools = RetailTools(db)
        user = _user_row(db, "U0001")
        blocked = _call(tools,
            "modify_user_address",
            user_id=user["user_id"],
            verification_code=user["verification_code"],
            address1="2 Other St",
            address2="Unit 1",
            city="Singapore",
            state="SG",
            country="SG",
            zip="018957",
            confirmed=False,
        )
        assert blocked["error"] == "confirmation_required"
        ok = _call(tools,
            "modify_user_address",
            user_id=user["user_id"],
            verification_code=user["verification_code"],
            address1="2 Other St",
            address2="Unit 1",
            city="Singapore",
            state="SG",
            country="SG",
            zip="018957",
            confirmed=True,
        )
        assert ok["ok"] and ok["changed"]
        again = _call(tools,
            "modify_user_address",
            user_id=user["user_id"],
            verification_code=user["verification_code"],
            address1="2 Other St",
            address2="Unit 1",
            city="Singapore",
            state="SG",
            country="SG",
            zip="018957",
            confirmed=True,
        )
        assert again["ok"] and again["idempotent_replay"]


def test_identity_guard_blocks_new_write_tools_without_code():
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "retail.db"
        seed_database(db, users=8, orders=40)
        tools = RetailTools(db)
        pending, _ = _pending(db)
        blocked = _call(tools,
            "cancel_pending_order",
            order_id=pending["order_id"],
            user_id=pending["user_id"],
            verification_code="bad",
            reason="no longer needed",
            confirmed=True,
        )
        assert blocked["error"] == "verification_code_required"
        assert tools.guardrails[-1]["tool"] == "cancel_pending_order"
