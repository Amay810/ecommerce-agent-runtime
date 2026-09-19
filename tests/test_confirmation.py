from __future__ import annotations

from pathlib import Path

from ecommerce_rag.confirmation import ConfirmationLedger, confirmation_decision
from ecommerce_rag.orders import connect, seed_database
from ecommerce_rag.skill_loader import load_skill
from ecommerce_rag.tools import RetailTools


def _eligible(db: Path):
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


def _authorized(tools: RetailTools, order: dict, code: str, *, session_id: str = "s1") -> str:
    arguments = {
        "order_id": order["order_id"], "user_id": order["user_id"],
        "verification_code": code, "confirmed": True,
    }
    tools.issue_confirmation(
        session_id=session_id, user_id=order["user_id"],
        operation="create_return_request", arguments=arguments,
        request_text="确认提交退货？",
    )
    response = tools.record_user_confirmation(
        session_id=session_id, response_text="确认提交退货"
    )
    assert response["decision"] is True
    return tools.authorization_for(
        session_id=session_id, user_id=order["user_id"],
        operation="create_return_request", arguments=arguments,
    ) or ""


def test_confirmation_decision_is_not_substring_matching():
    assert confirmation_decision("确认提交退货") is True
    assert confirmation_decision("好的") is True
    assert confirmation_decision("不确认，请不要修改订单") is False
    assert confirmation_decision("我确认想了解流程，但先不要执行") is False
    assert confirmation_decision("好的，我还在考虑") is None


def test_write_confirmed_flag_without_trusted_record_is_blocked(tmp_path):
    db = tmp_path / "retail.db"
    seed_database(db, users=20, orders=100)
    order, code = _eligible(db)
    tools = RetailTools(db)
    result = tools.call(
        "create_return_request", order_id=order["order_id"], user_id=order["user_id"],
        verification_code=code, confirmed=True,
    )
    assert result["error"] == "confirmation_required"
    assert connect(db).execute(
        "SELECT return_status FROM orders WHERE order_id=?", (order["order_id"],)
    ).fetchone()[0] is None


def test_confirmation_is_bound_to_session_user_operation_and_parameters(tmp_path):
    db = tmp_path / "retail.db"
    seed_database(db, users=20, orders=100)
    order, code = _eligible(db)
    tools = RetailTools(db)
    auth = _authorized(tools, order, code)
    args = {
        "order_id": order["order_id"], "user_id": order["user_id"],
        "verification_code": code, "confirmed": True,
    }
    assert tools.confirmation_ledger.validate_authorization(
        authorization_id=auth, session_id="other-session", user_id=order["user_id"],
        operation="create_return_request", parameters=args,
    ) is False
    assert tools.confirmation_ledger.validate_authorization(
        authorization_id=auth, session_id="s1", user_id="other-user",
        operation="create_return_request", parameters=args,
    ) is False
    changed_target = {**args, "order_id": "O999999"}
    assert tools.confirmation_ledger.validate_authorization(
        authorization_id=auth, session_id="s1", user_id=order["user_id"],
        operation="create_return_request", parameters=changed_target,
    ) is False
    changed_operation = {**args, "confirmed": False}
    assert tools.confirmation_ledger.validate_authorization(
        authorization_id=auth, session_id="s1", user_id=order["user_id"],
        operation="create_return_request", parameters=changed_operation,
    ) is True


def test_refusal_revokes_pending_confirmation_and_replay_is_idempotent(tmp_path):
    db = tmp_path / "retail.db"
    seed_database(db, users=20, orders=100)
    order, code = _eligible(db)
    tools = RetailTools(db)
    arguments = {
        "order_id": order["order_id"], "user_id": order["user_id"],
        "verification_code": code, "confirmed": True,
    }
    request_id = tools.issue_confirmation(
        session_id="s1", user_id=order["user_id"], operation="create_return_request",
        arguments=arguments, request_text="确认提交退货？",
    )
    refusal = tools.record_user_confirmation(session_id="s1", response_text="不确认")
    assert refusal["decision"] is False
    assert tools.authorization_for(
        session_id="s1", user_id=order["user_id"],
        operation="create_return_request", arguments=arguments,
    ) is None
    assert tools.record_user_confirmation(session_id="s1", response_text="好的")["decision"] is None

    auth = _authorized(tools, order, code)
    first = tools.call("create_return_request", _session_id="s1", _confirmation_id=auth, **arguments)
    version_after_first = connect(db).execute(
        "SELECT version FROM orders WHERE order_id=?", (order["order_id"],)
    ).fetchone()[0]
    second = tools.call("create_return_request", _session_id="s1", _confirmation_id=auth, **arguments)
    version_after_second = connect(db).execute(
        "SELECT version FROM orders WHERE order_id=?", (order["order_id"],)
    ).fetchone()[0]
    assert first["changed"] is True and second["idempotent_replay"] is True
    assert version_after_first == version_after_second
    assert request_id != ""


def test_skill_is_versioned_and_native_prompt_uses_loaded_content():
    skill = load_skill(Path("skills/return_request/SKILL.md"))
    assert skill.skill_id == "return_request"
    assert skill.version == "v0"
    captured = {}
    from ecommerce_rag.native_tool_policy import NativeGeneration, NativeToolPolicy
    from ecommerce_rag.domain import AgentObservation
    from ecommerce_rag.tool_schema import TOOL_SCHEMAS

    def generate(messages, _tools):
        captured["messages"] = messages
        return NativeGeneration(content="已停止。")

    policy = NativeToolPolicy(generate, skill_path="skills/return_request/SKILL.md", skill_enabled=True)
    policy.act(AgentObservation(
        current_message="先了解退货流程", session={"user_id": "U1"},
        history=[{"role": "user", "content": "先了解退货流程"}], tool_schemas=TOOL_SCHEMAS,
    ))
    system = captured["messages"][0]["content"]
    assert "<skill>" in system and "Return-request workflow" in system
    assert policy.last_trace["runtime"]["skill_content_hash"] == skill.content_hash
