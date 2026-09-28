from __future__ import annotations

import json
from pathlib import Path

import pytest

from ecommerce_rag.diagnostics.transaction_audit import database_state, state_diff, state_hash
from ecommerce_rag.mcp_server import MCPRetailFacade
from ecommerce_rag.orders import connect, seed_database
from ecommerce_rag.tools import RetailTools


SAFE_ORDER_FIELDS = (
    "order_id",
    "user_id",
    "status",
    "version",
    "opened",
    "quality_issue",
    "delivered_at",
    "return_status",
    "exchange_status",
)


def _eligible(db: Path) -> tuple[dict, str]:
    conn = connect(db)
    try:
        order = dict(conn.execute(
            "SELECT * FROM orders WHERE status='delivered' AND quality_issue=1 "
            "AND return_status IS NULL LIMIT 1"
        ).fetchone())
        code = conn.execute(
            "SELECT verification_code FROM users WHERE user_id=?", (order["user_id"],)
        ).fetchone()[0]
        return order, str(code)
    finally:
        conn.close()


def _args(order: dict, code: str) -> dict:
    return {
        "order_id": order["order_id"],
        "user_id": order["user_id"],
        "verification_code": code,
        "confirmed": True,
    }


def _authorize(
    tools: RetailTools,
    args: dict,
    *,
    session_id: str = "stale-test-session",
    operation: str = "create_return_request",
) -> tuple[str, dict]:
    request_id = tools.issue_confirmation(
        session_id=session_id,
        user_id=str(args["user_id"]),
        operation=operation,
        arguments=args,
        request_text="确认提交退货？",
    )
    response = tools.record_user_confirmation(
        session_id=session_id,
        response_text="确认提交退货",
    )
    assert response["decision"] is True
    record = tools.confirmation_ledger.records[request_id]
    authorization_id = tools.authorization_for(
        session_id=session_id,
        user_id=str(args["user_id"]),
        operation=operation,
        arguments=args,
    )
    assert authorization_id
    return authorization_id, record.to_dict()


def _order_view(db: Path, order_id: str) -> dict:
    conn = connect(db)
    try:
        row = dict(conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone())
        return {key: row.get(key) for key in SAFE_ORDER_FIELDS}
    finally:
        conn.close()


def _report_case(
    name: str,
    *,
    issue_state: dict,
    submit_state: dict,
    result: dict,
    before: dict,
    after: dict,
    record: dict,
) -> None:
    # Diagnostic output intentionally excludes verification_code and full rows.
    print(json.dumps({
        "case": name,
        "issue_state": issue_state,
        "submit_state": submit_state,
        "result": result,
        "state_hash_before_submit": state_hash(before),
        "state_hash_after_submit": state_hash(after),
        "state_diff": state_diff(before, after),
        "authorization": {
            key: record[key]
            for key in ("request_id", "session_id", "user_id", "operation", "parameter_hash", "status")
        },
    }, ensure_ascii=False, sort_keys=True))


def test_confirm_then_increment_version_does_not_commit_stale_write(tmp_path):
    db = tmp_path / "retail.db"
    seed_database(db, users=40, orders=200)
    order, code = _eligible(db)
    tools = RetailTools(db)
    args = _args(order, code)
    authorization_id, record = _authorize(tools, args)

    issue_state = _order_view(db, order["order_id"])
    conn = connect(db)
    try:
        conn.execute("UPDATE orders SET version=version+1 WHERE order_id=?", (order["order_id"],))
        conn.commit()
    finally:
        conn.close()
    submit_state = _order_view(db, order["order_id"])
    before_write = database_state(db)

    result = tools.call(
        "create_return_request",
        _session_id="stale-test-session",
        _confirmation_id=authorization_id,
        **args,
    )
    after_submit = database_state(db)
    _report_case(
        "version_changed_after_confirmation",
        issue_state=issue_state,
        submit_state=submit_state,
        result=result,
        before=before_write,
        after=after_submit,
        record=record,
    )
    assert result["changed"] is False
    assert state_diff(before_write, after_submit) == {}


@pytest.mark.parametrize("mutation", ["status", "eligibility"])
def test_confirm_then_change_state_or_eligibility_does_not_commit(
    tmp_path, mutation: str
):
    db = tmp_path / f"{mutation}.db"
    seed_database(db, users=40, orders=200)
    order, code = _eligible(db)
    tools = RetailTools(db)
    args = _args(order, code)
    authorization_id, record = _authorize(tools, args)

    issue_state = _order_view(db, order["order_id"])
    conn = connect(db)
    try:
        if mutation == "status":
            conn.execute("UPDATE orders SET status='pending' WHERE order_id=?", (order["order_id"],))
        else:
            conn.execute(
                "UPDATE orders SET quality_issue=0, opened=1, delivered_at='2026-07-01' "
                "WHERE order_id=?",
                (order["order_id"],),
            )
        conn.commit()
    finally:
        conn.close()
    submit_state = _order_view(db, order["order_id"])
    before_write = database_state(db)
    result = tools.call(
        "create_return_request",
        _session_id="stale-test-session",
        _confirmation_id=authorization_id,
        **args,
    )
    after_submit = database_state(db)
    _report_case(
        f"{mutation}_changed_after_confirmation",
        issue_state=issue_state,
        submit_state=submit_state,
        result=result,
        before=before_write,
        after=after_submit,
        record=record,
    )
    assert result["changed"] is False
    assert state_diff(before_write, after_submit) == {}


def test_state_change_after_authorization_check_cannot_race_the_write(tmp_path, monkeypatch):
    db = tmp_path / "race.db"
    seed_database(db, users=40, orders=200)
    order, code = _eligible(db)
    tools = RetailTools(db)
    args = _args(order, code)
    authorization_id, record = _authorize(tools, args)
    before_submit = database_state(db)
    issue_state = _order_view(db, order["order_id"])
    original = tools._require_trusted_confirmation

    def authorize_then_mutate(name, arguments, confirmed):
        result = original(name, arguments, confirmed)
        if result is None:
            conn = connect(db)
            try:
                conn.execute("UPDATE orders SET version=version+1 WHERE order_id=?", (order["order_id"],))
                conn.commit()
            finally:
                conn.close()
        return result

    monkeypatch.setattr(tools, "_require_trusted_confirmation", authorize_then_mutate)
    result = tools.call(
        "create_return_request",
        _session_id="stale-test-session",
        _confirmation_id=authorization_id,
        **args,
    )
    submit_state = _order_view(db, order["order_id"])
    after_submit = database_state(db)
    _report_case(
        "version_changed_between_auth_check_and_write",
        issue_state=issue_state,
        submit_state=submit_state,
        result=result,
        before=before_submit,
        after=after_submit,
        record=record,
    )
    assert result["changed"] is False
    assert _order_view(db, order["order_id"])["return_status"] is None


def test_confirmation_context_operation_and_key_changes_are_fail_closed(tmp_path):
    db = tmp_path / "scope.db"
    seed_database(db, users=40, orders=200)
    order, code = _eligible(db)
    tools = RetailTools(db)
    args = _args(order, code)
    authorization_id, _record = _authorize(tools, args)

    cases = [
        ("session", {"_session_id": "other-session", "_confirmation_id": authorization_id, **args}),
        ("user", {"_session_id": "stale-test-session", "_confirmation_id": authorization_id, **{**args, "user_id": "U0001"}}),
        ("operation", {"_session_id": "stale-test-session", "_confirmation_id": authorization_id, "order_id": order["order_id"], "user_id": order["user_id"], "verification_code": code, "reason": "no longer needed", "confirmed": True}),
        ("key_parameter", {"_session_id": "stale-test-session", "_confirmation_id": authorization_id, **{**args, "verification_code": "000000"}}),
    ]
    for name, call in cases:
        before = database_state(db)
        operation = "cancel_pending_order" if name == "operation" else "create_return_request"
        result = tools.call(operation, **call)
        after = database_state(db)
        print(json.dumps({"case": f"binding_{name}", "result": result, "state_diff": state_diff(before, after)}, ensure_ascii=False, sort_keys=True))
        assert result["changed"] is False
        assert state_diff(before, after) == {}


def test_direct_and_mcp_reject_the_same_stale_status_change(tmp_path):
    direct_db = tmp_path / "direct.db"
    mcp_db = tmp_path / "mcp.db"
    for db in (direct_db, mcp_db):
        seed_database(db, users=40, orders=200)
    direct_order, direct_code = _eligible(direct_db)
    mcp_order, mcp_code = _eligible(mcp_db)
    direct_tools = RetailTools(direct_db)
    direct_args = _args(direct_order, direct_code)
    direct_auth, direct_record = _authorize(direct_tools, direct_args)
    direct_before = database_state(direct_db)
    facade = MCPRetailFacade(RetailTools(mcp_db), mcp_order["user_id"], session_id="mcp-stale-session")
    mcp_args = {"order_id": mcp_order["order_id"], "verification_code": mcp_code, "confirmed": True}
    mcp_request = facade.issue_confirmation("create_return_request", mcp_args, "确认提交退货？")
    assert facade.record_user_confirmation("确认提交退货")["decision"] is True
    mcp_auth = facade.tools.authorization_for(
        session_id=facade.session_id,
        user_id=facade.user_id,
        operation="create_return_request",
        arguments={**mcp_args, "user_id": facade.user_id},
    )
    assert mcp_auth
    mcp_before = database_state(mcp_db)
    for db, order in ((direct_db, direct_order), (mcp_db, mcp_order)):
        conn = connect(db)
        try:
            conn.execute("UPDATE orders SET status='pending' WHERE order_id=?", (order["order_id"],))
            conn.commit()
        finally:
            conn.close()
    direct_result = direct_tools.call(
        "create_return_request", _session_id="stale-test-session", _confirmation_id=direct_auth, **direct_args
    )
    mcp_result = facade.create_return_request(mcp_order["order_id"], mcp_code, confirmed=True)
    direct_after = database_state(direct_db)
    mcp_after = database_state(mcp_db)
    print(json.dumps({
        "case": "direct_mcp_status_changed_after_confirmation",
        "direct": {"result": direct_result, "state_diff": state_diff(direct_before, direct_after), "authorization": {"request_id": direct_record["request_id"], "status": direct_record["status"]}},
        "mcp": {"result": mcp_result, "state_diff": state_diff(mcp_before, mcp_after), "request_id": mcp_request},
    }, ensure_ascii=False, sort_keys=True))
    assert direct_result["changed"] is False
    assert mcp_result["changed"] is False
    assert state_diff(direct_before, direct_after) == state_diff(mcp_before, mcp_after)
