from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from ecommerce_rag.context_compaction import compact_history
from ecommerce_rag.domain import AgentObservation
from ecommerce_rag.native_tool_policy import NativeGeneration, NativeToolPolicy, native_tool_schemas
from ecommerce_rag.tool_schema import TOOL_SCHEMAS
from ecommerce_rag.harness import _requested_input_type


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps({
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_product", "arguments": "{\"product_id\":\"P1\"}"},
                    }],
                },
            }],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5},
        }).encode("utf-8")


def _observation(history=None):
    return AgentObservation(
        current_message="验证码是123456，请查询订单 O1",
        session={"user_id": "U1"},
        history=history or [{"role": "user", "content": "验证码是123456，请查询订单 O1"}],
        tool_schemas=TOOL_SCHEMAS,
    )


def test_native_schema_hides_system_user_id_and_adds_control_tools():
    schemas = native_tool_schemas(TOOL_SCHEMAS)
    by_name = {row["function"]["name"]: row["function"] for row in schemas}
    assert "user_id" not in by_name["get_order"]["parameters"]["properties"]
    assert "user_id" not in by_name["get_order"]["parameters"]["required"]
    assert "escalate_to_human" not in by_name
    assert {"request_user_input", "handoff_to_human"} <= set(by_name)


def test_native_tool_call_injects_user_id_and_validates_arguments():
    def generate(messages, tools):
        assert messages[0]["role"] == "system"
        assert any(tool["function"]["name"] == "get_order" for tool in tools)
        return NativeGeneration(tool_calls=[{
            "id": "call_1", "type": "function",
            "function": {"name": "get_order", "arguments": json.dumps({
                "order_id": "O1", "verification_code": "123456", "user_id": "attacker"})},
        }], finish_reason="tool_calls", prompt_tokens=100, completion_tokens=10)

    policy = NativeToolPolicy(generate)
    action = policy.act(_observation())
    assert action.action_type == "tool_call"
    assert action.tool_name == "get_order"
    assert action.arguments["user_id"] == "U1"
    assert policy.last_trace["protocol"] == "native_tool_calls"


def test_openai_generator_uses_native_tools_wire_format():
    generator = NativeToolPolicy._openai_generator("http://localhost:8123/v1", "test-key", "Qwen")
    with patch("urllib.request.urlopen", return_value=_Response()) as mocked:
        generation = generator(
            [{"role": "user", "content": "查询商品"}],
            [{"type": "function", "function": {
                "name": "get_product", "parameters": {"type": "object"}}}],
        )

    request = mocked.call_args.args[0]
    body = json.loads(request.data.decode("utf-8"))
    assert body["tool_choice"] == "auto"
    assert body["parallel_tool_calls"] is False
    assert body["tools"][0]["function"]["name"] == "get_product"
    assert "response_format" not in body
    assert generation.tool_calls[0]["function"]["name"] == "get_product"
    assert generation.prompt_tokens == 20


def test_native_env_requires_endpoint_and_defaults_to_project_model(monkeypatch):
    monkeypatch.delenv("ARAG_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("ERAG_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("ARAG_LLM_MODEL", raising=False)
    monkeypatch.delenv("ERAG_LLM_MODEL", raising=False)
    monkeypatch.setenv("ARAG_LLM_API_KEY", "test-key")
    with pytest.raises(RuntimeError, match="ARAG_LLM_BASE_URL"):
        NativeToolPolicy.from_env()

    monkeypatch.setenv("ARAG_LLM_BASE_URL", "http://127.0.0.1:8123/v1")
    policy = NativeToolPolicy.from_env()
    assert policy.generator_meta["model"] == "Qwen/Qwen3-4B-Instruct-2507"
    assert policy.generator_meta["backend"] == "openai_compatible_native_tools"


def test_native_control_tools_map_to_internal_actions():
    ask = NativeToolPolicy(lambda _m, _t: NativeGeneration(tool_calls=[{
        "function": {"name": "request_user_input", "arguments": json.dumps({
            "message": "请提供验证码", "input_type": "verification_code"})}}]))
    action = ask.act(_observation())
    assert action.action_type == "final_answer" and action.requires_user_response

    handoff = NativeToolPolicy(lambda _m, _t: NativeGeneration(tool_calls=[{
        "function": {"name": "handoff_to_human", "arguments": json.dumps({
            "reason": "ownership_failed", "order_id": "O1"})}}]))
    action = handoff.act(_observation())
    assert action.action_type == "handoff"
    assert action.arguments == {"reason": "ownership_failed", "order_id": "O1"}


def test_native_control_tool_preserves_declared_input_type():
    ask = NativeToolPolicy(lambda _m, _t: NativeGeneration(tool_calls=[{
        "function": {"name": "request_user_input", "arguments": json.dumps({
            "message": "请提供退货原因", "input_type": "reason"})}}]))
    action = ask.act(_observation())
    assert action.requested_input_type == "return_reason"
    assert _requested_input_type(action, None) == "return_reason"


def test_native_control_tool_rejects_invalid_input_type():
    policy = NativeToolPolicy(lambda _m, _t: NativeGeneration(tool_calls=[{
        "function": {"name": "request_user_input", "arguments": json.dumps({
            "message": "请提供信息", "input_type": "not-a-contract-value"})}}]))
    action = policy.act(_observation())
    assert action.action_type == "handoff"
    assert policy.last_trace["attempts"][-1]["parse_stage"] == "request_input_type_invalid"


def test_native_control_tool_records_non_string_input_type_as_protocol_error():
    policy = NativeToolPolicy(lambda _m, _t: NativeGeneration(tool_calls=[{
        "function": {"name": "request_user_input", "arguments": json.dumps({
            "message": "请提供信息", "input_type": ["confirmation"]})}}]))
    action = policy.act(_observation())
    assert action.action_type == "handoff"
    assert policy.last_trace["attempts"][-1]["parse_stage"] == "request_input_type_invalid"


def test_tool_result_continuation_does_not_duplicate_current_user_turn():
    captured = {}

    def generate(messages, _tools):
        captured["messages"] = messages
        return NativeGeneration(content="订单状态已核实。")

    history = [
        {"role": "user", "content": "查询订单 O1"},
        {"role": "assistant", "content": "", "action": "tool_call",
         "tool_name": "get_order", "arguments": {
             "order_id": "O1", "verification_code": "123456", "user_id": "U1"}},
        {"role": "tool", "name": "get_order", "content": json.dumps({
            "ok": True, "order": {"order_id": "O1", "user_id": "U1", "status": "delivered"}})},
    ]
    observation = AgentObservation(
        current_message="查询订单 O1", session={"user_id": "U1"},
        history=history, tool_schemas=TOOL_SCHEMAS,
    )
    NativeToolPolicy(generate).act(observation)
    messages = captured["messages"]
    assert [message["role"] for message in messages[-3:]] == ["user", "assistant", "tool"]
    assert sum(message.get("content") == "查询订单 O1" for message in messages) == 1


def test_compaction_preserves_decision_fields_and_reduces_tool_payload():
    result = {
        "ok": True,
        "order": {
            "order_id": "O1", "user_id": "U1", "status": "delivered",
            "items": [{"item_id": "I1", "name": "Tablet", "price": 99.0,
                       "options": {"color": "black"}, "unused": "x" * 1000}],
            "address": {"address1": "private and unnecessary for this decision"},
            "payment_history": [{"transaction_type": "payment", "amount": 99.0,
                                 "payment_method_id": "P1", "unused": "x" * 1000}],
        },
    }
    history = [{"role": "tool", "name": "get_order", "content": json.dumps(result), "result": result}]
    compacted, stats = compact_history(history)
    compact = compacted[0]["result"]
    assert compact["order"]["order_id"] == "O1"
    assert compact["order"]["status"] == "delivered"
    assert compact["order"]["items"][0]["item_id"] == "I1"
    assert stats.compact_chars < stats.raw_chars
