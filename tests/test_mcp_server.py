from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from ecommerce_rag.mcp_server import MCP_TOOL_NAMES, MCPRetailFacade, _runtime_user_id, build_server
from ecommerce_rag.orders import connect, seed_database
from ecommerce_rag.tools import RetailTools


def _account(db: Path):
    conn = connect(db)
    try:
        row = conn.execute(
            "SELECT o.order_id, o.user_id, u.verification_code "
            "FROM orders o JOIN users u ON u.user_id=o.user_id LIMIT 1"
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def test_mcp_facade_injects_server_user_and_preserves_identity_guard():
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "retail.db"
        seed_database(db, users=4, orders=12)
        account = _account(db)
        facade = MCPRetailFacade(RetailTools(db), account["user_id"])
        ok = facade.get_order(account["order_id"], account["verification_code"])
        blocked = facade.get_order(account["order_id"], "bad-code")
        assert ok["ok"] is True
        assert ok["order"]["user_id"] == account["user_id"]
        assert blocked == {"ok": False, "changed": False, "error": "verification_code_required"}


def test_mcp_server_discovers_exact_guarded_tool_surface():
    pytest.importorskip("mcp")

    async def exercise():
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "retail.db"
            seed_database(db, users=4, orders=12)
            account = _account(db)
            server = build_server(RetailTools(db), account["user_id"])
            names = {tool.name for tool in await server.list_tools()}
            assert names == set(MCP_TOOL_NAMES)
            _content, result = await server.call_tool("get_order", {
                "order_id": account["order_id"],
                "verification_code": account["verification_code"],
            })
            assert result["ok"] is True
            assert result["order"]["user_id"] == account["user_id"]

    asyncio.run(exercise())


def test_mcp_write_still_requires_confirmation():
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "retail.db"
        seed_database(db, users=20, orders=100)
        conn = connect(db)
        try:
            order = dict(conn.execute(
                "SELECT * FROM orders WHERE status='delivered' AND quality_issue=1 LIMIT 1"
            ).fetchone())
            code = conn.execute(
                "SELECT verification_code FROM users WHERE user_id=?", (order["user_id"],)
            ).fetchone()[0]
        finally:
            conn.close()
        facade = MCPRetailFacade(RetailTools(db), order["user_id"])
        result = facade.create_return_request(order["order_id"], code, confirmed=False)
        assert result["ok"] is False
        assert result["changed"] is False
        assert result["error"] == "confirmation_required"


def test_mcp_confirmation_callback_cannot_override_server_identity():
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "retail.db"
        seed_database(db, users=20, orders=100)
        account = _account(db)
        facade = MCPRetailFacade(RetailTools(db), account["user_id"], session_id="mcp-test")
        request_id = facade.issue_confirmation(
            "create_return_request",
            {
                "order_id": account["order_id"],
                "verification_code": account["verification_code"],
                "confirmed": True,
                "user_id": "U-attacker",
            },
            "确认提交退货？",
        )
        record = facade.tools.confirmation_ledger.records[request_id]
        assert record.user_id == account["user_id"]
        assert record.parameters["user_id"] == account["user_id"]


def test_mcp_surface_matches_retail_tools_registry():
    assert set(MCP_TOOL_NAMES) == set(RetailTools(":memory:").executable_tool_names())


def test_mcp_runtime_requires_server_side_user_id(monkeypatch):
    monkeypatch.delenv("ERAG_MCP_USER_ID", raising=False)
    with pytest.raises(RuntimeError, match="ERAG_MCP_USER_ID is required"):
        _runtime_user_id()
