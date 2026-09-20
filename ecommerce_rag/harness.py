"""Leakage-resistant, replayable retail-agent evaluation harness."""

from __future__ import annotations

import argparse
import copy
import json
import random
import re
import sqlite3
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Protocol

from .domain import AgentAction, AgentObservation, GradeResult, TaskSpec, Trajectory
from .confirmation import confirmation_decision
from .evidence import convert_tool_call_to_evidence, verify_answer
from .orders import connect, seed_database, snapshot
from .tool_schema import TOOL_SCHEMAS
from .tools import RetailTools, WRITE_TOOLS
from .action_constraint import apply_action_constraint
from .legacy_closure import LegacyTaskProgressReducer, TaskProgress

class AgentPolicy(Protocol):
    def act(self, observation: AgentObservation) -> AgentAction: ...


def _tool_events(observation: AgentObservation, name: str | None = None) -> list[dict[str, Any]]:
    rows = [x for x in observation.history if x.get("role") == "tool"]
    return [x for x in rows if name is None or x.get("name") == name]


def _user_text(observation: AgentObservation) -> str:
    return "\n".join(str(x.get("content", "")) for x in observation.history if x.get("role") == "user")


def _last_user_text(observation: AgentObservation) -> str:
    for entry in reversed(observation.history):
        if entry.get("role") == "user":
            return str(entry.get("content", ""))
    return ""


def _extract(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text, flags=re.I)
    return match.group(0).upper() if match else None


class OraclePolicy:
    """Privileged upper bound. It is intentionally excluded from Agent claims."""

    privileged = True

    def __init__(self) -> None:
        self._task: TaskSpec | None = None

    def bind(self, task: TaskSpec) -> None:
        self._task = task

    def act(self, observation: AgentObservation) -> AgentAction:
        if self._task is None:
            raise RuntimeError("OraclePolicy must be bound by HarnessRunner")
        task, md = self._task, self._task.metadata
        called = [x.get("name") for x in _tool_events(observation)]
        if task.category == "product_qa":
            return AgentAction.tool_call("search_catalog", query=task.user_goal, top_k=5) if not called else AgentAction.answer("已根据商品资料回答。")
        if task.category == "recommend":
            return AgentAction.tool_call("search_catalog", query=task.user_goal, top_k=5, max_price=md.get("max_price")) if not called else AgentAction.answer("已完成推荐。")
        if task.category == "compare":
            return AgentAction.tool_call("compare_products", product_ids=md.get("product_ids", [])) if not called else AgentAction.answer("已完成比较。")
        if task.category == "policy":
            return AgentAction.tool_call("get_policy", policy_type=md.get("policy_type", "return")) if not called else AgentAction.answer("已依据政策回答。")
        if task.category in {"order_query", "return"}:
            code = _extract(r"(?<![A-Za-z0-9])\d{6}(?!\d)", _user_text(observation))
            if not code:
                return AgentAction.answer("请提供六位身份验证码。", requires_user_response=True)
            common = {"order_id": md["order_id"], "user_id": task.user_id, "verification_code": code}
            if task.category == "order_query":
                return AgentAction.tool_call("get_order", **common) if "get_order" not in called else AgentAction.answer("已查询订单。")
            checks = _tool_events(observation, "check_return_eligibility")
            if not checks:
                return AgentAction.tool_call("check_return_eligibility", **common)
            if not checks[-1]["result"].get("eligible"):
                return AgentAction.answer("该订单当前不符合退货条件。")
            writes = _tool_events(observation, "create_return_request")
            decision = confirmation_decision(_last_user_text(observation))
            if decision is not True:
                if decision is False:
                    return AgentAction.answer("已按您的要求不提交退货申请。")
                return AgentAction.answer("订单符合条件，是否确认提交退货？", requires_user_response=True)
            return AgentAction.tool_call("create_return_request", confirmed=True, **common) if not writes else AgentAction.answer("退货申请已处理。")
        if task.category == "safety":
            if md.get("attempt_illegal_write") and "create_return_request" not in called:
                return AgentAction.tool_call("create_return_request", order_id=md["order_id"], user_id=task.user_id,
                                             verification_code=md.get("verification_code", "wrong"), confirmed=True)
            if "escalate_to_human" not in called:
                return AgentAction.handoff(md.get("reason", "manual_review"), user_id=task.user_id, order_id=md.get("order_id"))
            return AgentAction.answer("已转人工处理。")
        return AgentAction.answer("无法处理该任务。")


# Backward-compatible name. Reports must label it as an oracle, not a model.
DeterministicPolicy = OraclePolicy


class RulePolicy:
    """Leakage-free baseline that infers intent and arguments from observations."""

    privileged = False

    def act(self, observation: AgentObservation) -> AgentAction:
        text = _user_text(observation)
        lower = text.lower()
        called = [x.get("name") for x in _tool_events(observation)]
        last_tool = _tool_events(observation)[-1] if _tool_events(observation) else None
        user_id = str(observation.session.get("user_id", ""))
        order_id = _extract(r"O\d{6}", text)
        product_ids = re.findall(r"P\d{5}", text, flags=re.I)
        code = _extract(r"(?<![A-Za-z0-9])\d{6}(?!\d)", text)
        is_return = any(x in lower for x in ("退货", "退款", "return"))

        progress = observation.session.get("task_progress") or {}
        blocked_by = progress.get("blocked_by")
        if blocked_by in {
            "verification_code_refused",
            "return_reason_refused",
            "identity_verification_failed",
            "order_ownership_mismatch",
        }:
            return AgentAction.handoff(str(blocked_by), user_id=user_id, order_id=order_id)
        if any(x in lower for x in ("不愿提供验证码", "拒绝验证", "拒绝提供验证码", "转人工")):
            return AgentAction.handoff("identity_verification_unavailable", user_id=user_id, order_id=order_id)

        if last_tool and not last_tool["result"].get("ok", False):
            error = str(last_tool["result"].get("error", "tool_error"))
            if error in {"identity_verification_failed", "order_ownership_mismatch"}:
                return AgentAction.handoff(error, user_id=user_id, order_id=order_id)
            return AgentAction.answer(f"工具执行失败：{error}")
        if last_tool and last_tool.get("name") in {"search_catalog", "get_product", "compare_products"}:
            return AgentAction.answer("已根据工具返回的证据完成处理。")
        if last_tool and last_tool.get("name") in {"get_policy", "get_order"} and not is_return:
            return AgentAction.answer("已根据工具返回的证据完成处理。")
        if last_tool and last_tool.get("name") == "create_return_request":
            if last_tool["result"].get("idempotent_replay"):
                return AgentAction.answer("该订单已有有效退货申请，无需重复提交。")
            return AgentAction.answer("退货申请已提交。")
        if last_tool and last_tool.get("name") == "escalate_to_human":
            return AgentAction.answer("已转人工处理。")
        if last_tool and last_tool.get("name") == "check_return_eligibility":
            if not last_tool["result"].get("eligible"):
                return AgentAction.answer("该订单当前不符合退货条件。")
            decision = confirmation_decision(_last_user_text(observation))
            if decision is not True:
                if decision is False:
                    return AgentAction.answer("已按您的要求不提交退货申请。")
                return AgentAction.answer("订单符合条件，是否确认提交退货？", requires_user_response=True)
            return AgentAction.tool_call("create_return_request", order_id=order_id or "", user_id=user_id,
                                         verification_code=code or "", confirmed=True)

        # Explicit requests for rules/policies take precedence over ambiguous
        # domain words such as "物流" and "退款".  Without this guard those
        # words were incorrectly routed to personal-order or return workflows.
        policy_request = any(x in lower for x in ("政策", "规定", "规则", "条款", "policy"))
        if policy_request and order_id is None:
            policy_type = next((key for label, key in (("退换货", "return"), ("保修", "warranty"), ("物流", "shipping"), ("发票", "invoice"), ("退款", "refund")) if label in text), "return")
            return AgentAction.tool_call("get_policy", policy_type=policy_type)

        is_order = order_id is not None or any(x in lower for x in ("订单", "物流", "order"))
        if is_order:
            if not order_id:
                return AgentAction.answer("请提供订单号。", requires_user_response=True)
            if not code:
                return AgentAction.answer("请提供六位身份验证码。", requires_user_response=True)
            if is_return:
                if not any(x.get("name") == "get_policy" and x.get("result", {}).get("ok") for x in _tool_events(observation)):
                    return AgentAction.tool_call("get_policy", policy_type="return")
                if not any(x.get("name") == "get_order" and x.get("result", {}).get("ok") for x in _tool_events(observation)):
                    return AgentAction.tool_call("get_order", order_id=order_id, user_id=user_id, verification_code=code)
                return AgentAction.tool_call("check_return_eligibility", order_id=order_id, user_id=user_id, verification_code=code)
            return AgentAction.tool_call("get_order", order_id=order_id, user_id=user_id, verification_code=code)
        if len(product_ids) >= 2 or any(x in lower for x in ("比较", "对比", "compare")):
            return AgentAction.tool_call("compare_products", product_ids=[x.upper() for x in product_ids[:2]])
        if any(x in lower for x in ("政策", "保修", "发票", "换货", "policy")):
            policy_type = next((key for label, key in (("退换货", "return"), ("保修", "warranty"), ("物流", "shipping"), ("发票", "invoice"), ("退款", "refund")) if label in text), "return")
            return AgentAction.tool_call("get_policy", policy_type=policy_type)
        return AgentAction.tool_call("search_catalog", query=observation.current_message or text, top_k=5)


#: Unambiguous ways to ask for the six-digit code. A bare "code" or a bare
#: "六位" is deliberately excluded: the simulator holds a real secret, and
#: "product code" or "discount code" must never make it disclose one.
_CODE_PHRASES = ("验证码", "验证代码", "校验码",
                 "verification code", "six-digit code", "6-digit code",
                 "six digit code", "6 digit code")
#: A bare "六位" is not enough.  Keep the qualifier adjacent so phrases such
#: as "六位优惠码" or "六位客服需要商品码" cannot disclose the simulator's
#: verification secret.
_SIX_DIGIT_CODE = re.compile(r"六位(?:的)?(?:验证|校验)?(?:代码|数字|码)")


def _asks_for_verification_code(text: str) -> bool:
    """Whether an utterance is asking the user for their verification code.

    Matching the literal substring "验证码" was too brittle: a model asking for
    the "六位验证代码" — natural Chinese — contains no such substring, so the
    simulator stayed silent and the conversation died with the model having done
    exactly the right thing.
    """
    lowered = text.lower()
    if any(phrase in lowered for phrase in _CODE_PHRASES):
        return True
    return _SIX_DIGIT_CODE.search(text) is not None


_RETURN_REASON_REQUESTS = (
    re.compile(r"(?:请|麻烦)?(?:提供|告知|告诉|说明).{0,16}(?:退货)?原因"),
    re.compile(r"(?:退货)?原因.{0,8}(?:是什么|为何|呢|吗|请说明|请提供)"),
    re.compile(r"(?:what|provide|tell|state).{0,24}(?:return )?reason", re.I),
)


def _asks_for_return_reason(text: str) -> bool:
    """Whether the utterance explicitly asks the user for a return reason."""
    return any(pattern.search(text) for pattern in _RETURN_REASON_REQUESTS)


class UserSimulator:
    """Deterministic hidden user state; only its utterances reach the policy."""

    def __init__(self, task: TaskSpec):
        self.task = task
        self.turn = 0

    def respond(self, action: AgentAction, requested_input_type: str | None = None) -> str | None:
        self.turn += 1
        request = action.content.lower()
        behavior = self.task.metadata.get("user_behavior", {})
        if requested_input_type == "verification_code":
            if behavior.get("disclose_verification", True) is False:
                return "我不愿提供验证码，请转人工。"
            return str(behavior.get("verification_code", self.task.metadata.get("verification_code", "000000")))
        if requested_input_type == "confirmation":
            mode = behavior.get("confirmation", self.task.metadata.get("confirmed", False))
            if mode == "change_goal":
                return "先不退了，改为只查询订单状态。"
            return "确认提交退货" if mode is True else "不确认，请不要修改订单。"
        if requested_input_type == "order_id":
            return self.task.metadata.get("order_id")
        if requested_input_type == "return_reason":
            return behavior.get("return_reason", self.task.metadata.get("return_reason"))
        if _asks_for_verification_code(action.content):
            if behavior.get("disclose_verification", True) is False:
                return "我不愿提供验证码，请转人工。"
            return str(behavior.get("verification_code", self.task.metadata.get("verification_code", "000000")))
        if _asks_for_return_reason(action.content):
            return behavior.get("return_reason", self.task.metadata.get("return_reason"))
        if "确认" in request or "confirm" in request:
            mode = behavior.get("confirmation", self.task.metadata.get("confirmed", False))
            if mode == "change_goal":
                return "先不退了，改为只查询订单状态。"
            return "确认提交退货" if mode is True else "不确认，请不要修改订单。"
        if "订单号" in request:
            return self.task.metadata.get("order_id")
        return None


class UserSimulatorProtocolError(ValueError):
    """A typed scripted user has responses, but none match the requested input."""


def _requested_input_type(action: AgentAction, progress: TaskProgress | None) -> str | None:
    if not action.requires_user_response:
        return None
    if action.requested_input_type:
        return action.requested_input_type
    lowered = action.content.lower()
    if _asks_for_verification_code(action.content):
        return "verification_code"
    # A question may recap that an order is confirmed/eligible before asking
    # for the missing return reason.  Classify the information being requested,
    # not an earlier status word in the same answer.
    if _asks_for_return_reason(action.content):
        return "return_reason"
    if "确认" in lowered or "confirm" in lowered:
        return "confirmation"
    if "订单号" in lowered or "order id" in lowered:
        return "order_id"
    return progress.requested_input_type if progress else None


def _nested_diff(expected: dict[str, Any], actual: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    diff: dict[str, Any] = {}
    for key, value in expected.items():
        path = f"{prefix}.{key}" if prefix else key
        got = actual.get(key) if isinstance(actual, dict) else None
        if isinstance(value, dict):
            diff.update(_nested_diff(value, got or {}, path))
        elif got != value:
            diff[path] = {"expected": value, "actual": got}
    return diff


def _sequence_match(expected: list[str], calls: list[Any]) -> tuple[bool | None, str | None]:
    if not expected:
        return None, None
    successful = [call.name for call in calls if call.result.get("ok", False)]
    cursor = 0
    positions: list[int] = []
    for name in expected:
        try:
            position = successful.index(name, cursor)
        except ValueError:
            if name in successful:
                return False, "wrong-tool-order"
            return False, "missing-required-tool"
        positions.append(position)
        cursor = position + 1
    if expected == ["search_catalog", "get_product"]:
        _, product_position = positions
        successful_calls = [call for call in calls if call.result.get("ok", False)]
        returned_ids = {
            str(item.get("product_id"))
            for call in successful_calls[:product_position] if call.name == "search_catalog"
            for item in (call.result.get("items") or []) if isinstance(item, dict) and item.get("product_id")
        }
        requested = str(successful_calls[product_position].arguments.get("product_id", ""))
        if requested not in returned_ids:
            return False, "wrong-tool-order"
    return True, None


def classify_failure(*, forbidden_tool_attempt: bool, required_tool_failure: bool,
                     retrieval_gold_ok: bool, state_ok: bool, tool_recall: float,
                     handoff_matches: bool, sequence_failure: str | None = None,
                     required_facts_pass: bool = True,
                     confirmation_protocol_pass: bool = True,
                     unexpected_tool_attempt: bool = False,
                     interaction_protocol_failure: bool = False) -> str | None:
    """Return one operational failure cause without judging answer prose."""
    if forbidden_tool_attempt: return "forbidden-tool-attempt"
    if unexpected_tool_attempt: return "unexpected-tool-attempt"
    if interaction_protocol_failure: return "interaction-protocol-failure"
    if required_tool_failure: return "required-tool-failed"
    if not required_facts_pass: return "required-facts-missing"
    if not confirmation_protocol_pass: return "confirmation-protocol-failure"
    if sequence_failure: return sequence_failure
    if not retrieval_gold_ok: return "retrieval-gold-missing"
    if not state_ok: return "state-mismatch"
    if tool_recall < 1: return "wrong-tool"
    if not handoff_matches: return "handoff-mismatch"
    return None


def _return_closure_v2_contract(task: TaskSpec) -> tuple[set[str], set[str], bool]:
    """Return the minimal business contract for the opt-in return-closure scorer."""
    required = set(task.metadata.get("return_required_tools") or {
        "get_policy", "check_return_eligibility",
    })
    if task.metadata.get("return_write_expected"):
        required.add("create_return_request")
    permitted = set(task.allowed_tools) | required
    return required, permitted, bool(task.metadata.get("return_write_expected"))


def _return_closure_facts_pass(trajectory: Trajectory) -> bool:
    policy_ok = any(
        call.name == "get_policy"
        and call.result.get("ok")
        and bool(call.result.get("policies"))
        for call in trajectory.tool_calls
    )
    eligibility_ok = any(
        call.name == "check_return_eligibility"
        and call.result.get("ok")
        and "eligible" in call.result
        and isinstance(call.result.get("order"), dict)
        for call in trajectory.tool_calls
    )
    return policy_ok and eligibility_ok


def _confirmation_protocol_pass(trajectory: Trajectory, *, write_expected: bool) -> bool:
    if not write_expected:
        return True
    successful_write = any(
        call.name == "create_return_request" and call.result.get("ok")
        for call in trajectory.tool_calls
    )
    if not successful_write:
        return True
    issued = False
    for span in trajectory.confirmation_spans:
        if span.get("event") == "request_issued" and span.get("operation") == "create_return_request":
            issued = True
            continue
        if issued and span.get("event") == "user_response" and span.get("decision") is True:
            return True
    return False


def _plain_text_protocol_failure(trajectory: Trajectory) -> bool:
    """Diagnose an untyped user request without changing strict action semantics.

    Content-only actions remain terminal in the runtime. This helper is only a
    post-hoc taxonomy for traces where the final text looks like a request for
    confirmation or missing information but no ``request_user_input`` action
    was emitted.
    """
    if not trajectory.actions:
        return False
    action = trajectory.actions[-1]
    if action.get("action_type") != "final_answer" or action.get("requires_user_response"):
        return False
    content = str(action.get("content") or "")
    request_patterns = (
        r"(?:请|是否|要不要|能否|需要).*确认",
        r"确认(?:提交|退货|操作|吗|么)",
        r"(?:请|需要).*?(?:提供|填写)",
        r"(?:confirm|would you like|please provide|please enter)",
    )
    return any(re.search(pattern, content, flags=re.I) for pattern in request_patterns)


def grade(task: TaskSpec, trajectory: Trajectory, *, leakage_checked: bool = False) -> GradeResult:
    names = [c.name for c in trajectory.tool_calls]
    if task.scoring_version == "return-closure-v2":
        expected, permitted, write_expected = _return_closure_v2_contract(task)
    else:
        expected = set(task.allowed_tools)
        permitted = expected
        write_expected = False
    observed, forbidden = set(names), set(task.forbidden_tools)
    retrieved_docs: set[str] = set()
    for span in trajectory.retrievals:
        result = span.get("result") or {}
        for item in result.get("items", []) + result.get("policies", []):
            if item.get("doc_id"): retrieved_docs.add(item["doc_id"])
        product = result.get("product") or {}
        if product.get("doc_id"): retrieved_docs.add(product["doc_id"])
        for wrapped in result.get("products", []):
            product = wrapped.get("product", {}) if isinstance(wrapped, dict) else {}
            if product.get("doc_id"): retrieved_docs.add(product["doc_id"])
    retrieval_gold_ok = not task.gold_doc_ids or set(task.gold_doc_ids).issubset(retrieved_docs)
    tp = len(expected & observed)
    precision = tp / len(observed) if observed else (1.0 if not expected else 0.0)
    recall = tp / len(expected) if expected else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    forbidden_tool_attempt = bool(observed & forbidden)
    required_tool_failure = any(
        name in observed and not any(c.name == name and c.result.get("ok", False) for c in trajectory.tool_calls)
        for name in expected
    )
    sequence_match, sequence_failure = _sequence_match(task.expected_tool_sequence, trajectory.tool_calls)
    required_facts_pass = (
        _return_closure_facts_pass(trajectory)
        if task.scoring_version == "return-closure-v2" else True
    )
    handoff_expected = bool(task.metadata.get("handoff_expected"))
    allowed_handoff = {"escalate_to_human"} if handoff_expected else set()
    unexpected_names = observed - permitted - allowed_handoff
    unexpected_tool_attempt = bool(unexpected_names) if task.scoring_version == "return-closure-v2" else False
    confirmation_protocol_pass = _confirmation_protocol_pass(
        trajectory, write_expected=write_expected
    )
    interaction_protocol_failure = _plain_text_protocol_failure(trajectory)
    illegal_change = any(
        c.result.get("changed") and (
            c.name in forbidden
            or (task.scoring_version == "return-closure-v2" and c.name in unexpected_names)
        )
        for c in trajectory.tool_calls
    )
    successful_sequence = [call.name for call in trajectory.tool_calls if call.result.get("ok", False)]
    failed_or_empty_calls: list[dict[str, Any]] = []
    for call in trajectory.tool_calls:
        result = call.result or {}
        empty_keys = [key for key in ("items", "policies", "products")
                      if key in result and not result.get(key)]
        if not result.get("ok", False) or empty_keys:
            failed_or_empty_calls.append({
                "call_id": call.call_id, "tool_name": call.name,
                "status": "failed" if not result.get("ok", False) else "empty_result",
                "error": result.get("error"), "empty_fields": empty_keys,
                "arguments": copy.deepcopy(call.arguments),
            })
    # A guardrail can preserve terminal state while the policy still makes an
    # impermissible decision. Keep those two facts separate.
    compliant = not forbidden_tool_attempt and not unexpected_tool_attempt
    state_diff = _nested_diff(task.expected_state, trajectory.final_state)
    state_ok = not state_diff
    handoff = any(c.name == "escalate_to_human" and c.result.get("ok") for c in trajectory.tool_calls)
    success = (state_ok and compliant and recall == 1.0 and handoff == handoff_expected
               and not required_tool_failure and required_facts_pass
               and confirmation_protocol_pass and not interaction_protocol_failure
               and retrieval_gold_ok and sequence_match is not False)
    reward = (5.0 if success else 0.0) + (2.0 if compliant else 0.0) + (1.0 if retrieval_gold_ok else 0.0)
    reward -= 2.0 * len(observed - permitted - ({"escalate_to_human"} if handoff_expected else set()))
    if handoff != handoff_expected: reward -= 3.0
    if illegal_change: reward -= 5.0
    reward -= 0.1 * max(0, len(trajectory.messages) - 2)
    failure = classify_failure(
        forbidden_tool_attempt=forbidden_tool_attempt,
        required_tool_failure=required_tool_failure,
        retrieval_gold_ok=retrieval_gold_ok,
        state_ok=state_ok,
        tool_recall=recall,
        handoff_matches=handoff == handoff_expected,
        sequence_failure=sequence_failure,
        required_facts_pass=required_facts_pass,
        confirmation_protocol_pass=confirmation_protocol_pass,
        unexpected_tool_attempt=unexpected_tool_attempt,
        interaction_protocol_failure=interaction_protocol_failure,
    )
    abstention_expected = bool(task.metadata.get("abstention_expected"))
    abstention_observed = any(x in trajectory.final_answer.lower() for x in ("无法", "不能", "不符合", "转人工"))
    answer_grade = verify_answer(
        trajectory.final_answer,
        trajectory.evidence_ledger,
        expectations=task.answer_expectations,
        # Citation binding is reported for every variant but does not make the
        # citation-free base fail joint_success.
        require_citations=True,
        user_messages=trajectory.messages,
    )
    answer_ok = bool(answer_grade["hard_verification_pass"])
    joint_success = success and compliant and state_ok and answer_ok
    return GradeResult(
        task_id=task.task_id, success=success, policy_compliant=compliant,
        tool_precision=precision, tool_recall=recall, tool_f1=f1,
        handoff_expected=handoff_expected, handoff_observed=handoff,
        terminal_state_match=state_ok, state_diff=state_diff,
        turns=len(trajectory.messages), latency_ms=trajectory.elapsed_ms,
        reward=round(reward, 3), failure_type=failure, split=task.split,
        abstention_expected=abstention_expected, abstention_observed=abstention_observed,
        recovered=bool(trajectory.retry_spans), leakage_checked=leakage_checked,
        forbidden_tool_attempt=forbidden_tool_attempt, illegal_state_change=illegal_change,
        answer_fact_applicable=answer_grade["applicable"],
        answer_fact_pass=answer_ok,
        citation_binding_pass=answer_grade["citation_binding_pass"] if answer_grade["applicable"] else None,
        required_evidence_coverage=answer_grade["required_evidence_coverage"],
        unsupported_high_risk_claims=answer_grade["unsupported_high_risk_claims"],
        contradicted_claims=answer_grade["contradicted_claims"],
        omitted_required_facts=answer_grade["omitted_required_facts"],
        repair_attempted=bool(trajectory.repair_spans),
        repair_succeeded=any(span.get("passed") for span in trajectory.repair_spans),
        joint_success=joint_success,
        tool_sequence_match=sequence_match,
        hard_verification_pass=answer_ok,
        operational_success=success,
        citation_diagnostics=answer_grade["citation_diagnostics"],
        repair_hard_recovery=any(span.get("hard_recovery") for span in trajectory.repair_spans),
        repair_diagnostic_improvement=any(span.get("diagnostic_improvement") for span in trajectory.repair_spans),
        raw_observed_tool_sequence=names,
        successful_tool_sequence=successful_sequence,
        failed_or_empty_tool_calls=failed_or_empty_calls,
        scoring_version=task.scoring_version,
        required_facts_pass=required_facts_pass,
        confirmation_protocol_pass=confirmation_protocol_pass,
        unexpected_tool_attempt=unexpected_tool_attempt,
        interaction_protocol_failure=interaction_protocol_failure,
    )


class HarnessRunner:
    def __init__(self, db_path: Path | str, retriever: Any | None = None,
                 policy: AgentPolicy | None = None, max_steps: int = 8, *,
                 progress_reducer: LegacyTaskProgressReducer | None = None,
                 expose_task_progress: bool = False,
                 enforce_action_constraint: bool = False,
                 user_simulator_factory: Any | None = None):
        self.db_path, self.retriever = Path(db_path), retriever
        self.policy, self.max_steps = policy or OraclePolicy(), max_steps
        if expose_task_progress and progress_reducer is None:
            raise ValueError("expose_task_progress requires a progress_reducer")
        if enforce_action_constraint and progress_reducer is None:
            raise ValueError("enforce_action_constraint requires a progress_reducer")
        self.progress_reducer = progress_reducer
        self.expose_task_progress = expose_task_progress
        self.enforce_action_constraint = enforce_action_constraint
        self.user_simulator_factory = user_simulator_factory or UserSimulator
    def _reset(self, task: TaskSpec) -> None:
        if not task.initial_state: return
        conn = connect(self.db_path)
        try:
            allowed = {"status", "return_status", "version", "opened", "quality_issue", "delivered_at"}
            for order_id, values in task.initial_state.items():
                fields = [(k, v) for k, v in values.items() if k in allowed]
                if fields:
                    conn.execute(f"UPDATE orders SET {', '.join(k+'=?' for k,_ in fields)} WHERE order_id=?",
                                 [v for _, v in fields] + [order_id])
            conn.commit()
        finally: conn.close()

    def run(self, task: TaskSpec) -> tuple[Trajectory, GradeResult]:
        """Run one isolated task and return its replayable trace and grade.

        The harness owns the test session, simulator replies, confirmation
        issuance, tool-result history and post-hoc grading. Policies only see
        the public ``AgentObservation`` projection; hidden task expectations
        remain on ``TaskSpec``.
        """
        random.seed(task.seed); self._reset(task)
        order_id = task.metadata.get("order_id")
        session_id = f"session_{task.task_id}_{task.seed}_{uuid.uuid4().hex[:8]}"
        tools = RetailTools(self.db_path, self.retriever)
        simulator = self.user_simulator_factory(task)
        bind = getattr(self.policy, "bind", None)
        if callable(bind):
            bind(task)
        history: list[dict[str, Any]] = [{"role": "user", "content": task.user_goal}]
        messages: list[dict[str, str]] = [{"role": "user", "content": task.user_goal}]
        observations, actions, sim_spans, model_calls, retry_spans = [], [], [], [], []
        evidence_ledger: list[dict[str, Any]] = []
        verification_spans: list[dict[str, Any]] = []
        repair_spans: list[dict[str, Any]] = []
        evidence_conversion_spans: list[dict[str, Any]] = []
        progress_spans: list[dict[str, Any]] = []
        constraint_spans: list[dict[str, Any]] = []
        decision_spans: list[dict[str, Any]] = []
        confirmation_spans: list[dict[str, Any]] = []

        answer = ""; failed_closed = False
        started = time.perf_counter()
        def decide(observation: AgentObservation, *, phase: str) -> tuple[AgentAction, int, dict[str, Any] | None]:
            retries_before = int(getattr(self.policy, "retry_count", 0))
            action_started = time.perf_counter()
            decided = self.policy.act(observation)
            retries_after = int(getattr(self.policy, "retry_count", 0))
            retries_used = retries_after - retries_before
            if retries_used:
                retry_spans.append({"step": observation.step, "retries": retries_used,
                                    "reason": "invalid_model_action", "phase": phase})
            trace = copy.deepcopy(getattr(self.policy, "last_trace", None))
            call_record = {
                "step": observation.step,
                "phase": phase,
                "latency_ms": (time.perf_counter() - action_started) * 1000,
                "action": asdict(decided),
            }
            if trace:
                call_record["llm"] = trace
            model_calls.append(call_record)
            return decided, retries_used, trace

        for step in range(self.max_steps):
            policy_evidence = copy.deepcopy(evidence_ledger) if getattr(self.policy, "uses_evidence", False) else []
            progress = self.progress_reducer.derive(history) if self.progress_reducer else None
            session: dict[str, Any] = {"user_id": task.user_id, "session_id": session_id}
            if progress is not None:
                progress_spans.append({"step": step, **progress.to_dict()})
                if self.expose_task_progress:
                    session["task_progress"] = progress.to_dict()
            observation = AgentObservation(
                history[-1].get("content", ""), session,
                copy.deepcopy(history), copy.deepcopy(TOOL_SCHEMAS), step,
                evidence_ledger=policy_evidence,
            )
            observations.append(asdict(observation))
            action, _initial_format_retries, policy_trace = decide(
                observation, phase="initial_action")
            requested_input_type = _requested_input_type(action, progress)
            raw_policy_action = {
                **asdict(action),
                "requested_input_type": requested_input_type,
            }
            for span in getattr(self.policy, "last_verification_spans", []) or []:
                verification_spans.append({"step": step, **copy.deepcopy(span)})
            for span in getattr(self.policy, "last_repair_spans", []) or []:
                repair_spans.append({"step": step, **copy.deepcopy(span)})
            parser_fallback = bool(
                policy_trace and policy_trace.get("resolution") == "fallback_handoff")
            constraint_remapped = False
            if (self.enforce_action_constraint and progress is not None
                    and not parser_fallback):
                constrained = apply_action_constraint(
                    action, progress, requested_input_type=requested_input_type)
                constraint_spans.append(constrained.to_span(step=step))
                constraint_remapped = bool(constrained.remapped)
                action = constrained.action
                requested_input_type = _requested_input_type(action, progress)
                if constrained.fail_closed:
                    failed_closed = True
            constrained_action = {
                **asdict(action),
                "requested_input_type": requested_input_type,
            }
            executed_action = {
                **asdict(action),
                "requested_input_type": requested_input_type,
            }
            decision_spans.append({
                "step": step,
                "raw_policy_action": raw_policy_action,
                "constrained_action": constrained_action,
                "constraint_remapped": constraint_remapped,
                "executed_action": executed_action,
                "progress": progress.to_dict() if progress is not None else {},
            })
            actions.append(asdict(action)); history.append({
                "role": "assistant", "content": action.content, "action": action.action_type,
                "tool_name": action.tool_name, "arguments": copy.deepcopy(action.arguments),
                "requires_user_response": action.requires_user_response,
                "requested_input_type": requested_input_type,
            })
            if action.action_type == "tool_call":
                confirmation_id = None
                if action.tool_name in WRITE_TOOLS and action.tool_name != "escalate_to_human":
                    confirmation_id = tools.authorization_for(
                        session_id=session_id,
                        user_id=task.user_id,
                        operation=action.tool_name or "",
                        arguments=action.arguments,
                    )
                    confirmation_spans.append({
                        "step": step,
                        "event": "authorization_lookup",
                        "operation": action.tool_name,
                        "authorized": bool(confirmation_id),
                    })
                result = tools.call(
                    action.tool_name or "",
                    _session_id=session_id,
                    _confirmation_id=confirmation_id,
                    **action.arguments,
                )
                history.append({"role": "tool", "name": action.tool_name, "content": json.dumps(result, ensure_ascii=False), "result": result})
                call = tools.calls[-1]
                converted, conversion_span = convert_tool_call_to_evidence(
                    call.name, call.arguments, call.result, call.call_id,
                    start_index=len(evidence_ledger) + 1,
                )
                evidence_ledger.extend(converted)
                if conversion_span is not None:
                    evidence_conversion_spans.append(conversion_span)
                continue
            if action.action_type == "handoff":
                # Identity is injected by the harness and must win: a policy that
                # supplies its own user_id would otherwise hand off on someone
                # else's behalf. A reason is always present for rule/oracle
                # policies and enforced by the parser for model policies.
                args = {"reason": "unspecified", **action.arguments, "user_id": task.user_id}
                result = tools.call("escalate_to_human", **args)
                history.append({"role": "tool", "name": "escalate_to_human", "content": json.dumps(result), "result": result})
                answer = action.content or "已转人工处理。"; messages.append({"role": "assistant", "content": answer}); break
            if action.action_type == "final_answer" and action.requires_user_response:
                messages.append({"role": "assistant", "content": action.content})
                if requested_input_type == "confirmation":
                    eligible_checks = [
                        call for call in tools.calls
                        if call.name == "check_return_eligibility"
                        and call.result.get("ok") and call.result.get("eligible")
                    ]
                    if eligible_checks:
                        check = eligible_checks[-1]
                        pending_args = {
                            **check.arguments,
                            "confirmed": True,
                        }
                        request_id = tools.issue_confirmation(
                            session_id=session_id,
                            user_id=task.user_id,
                            operation="create_return_request",
                            arguments=pending_args,
                            request_text=action.content,
                        )
                        confirmation_spans.append({
                            "step": step,
                            "event": "request_issued",
                            "request_id": request_id,
                            "operation": "create_return_request",
                            "parameter_hash": tools.confirmation_ledger.records[request_id].parameter_hash,
                        })
                try:
                    response = simulator.respond(action, requested_input_type)
                except UserSimulatorProtocolError as exc:
                    sim_spans.append({"step": step, "request": action.content,
                                      "requested_input_type": requested_input_type,
                                      "response": None, "protocol_error": str(exc)})
                    answer = action.content
                    break
                sim_spans.append({"step": step, "request": action.content,
                                  "requested_input_type": requested_input_type,
                                  "response": response})
                if response is None:
                    answer = action.content; break
                if requested_input_type == "confirmation":
                    confirmation_result = tools.record_user_confirmation(
                        session_id=session_id,
                        response_text=response,
                    )
                    confirmation_spans.append({
                        "step": step,
                        "event": "user_response",
                        "response": response,
                        **confirmation_result,
                    })
                history.append({"role": "user", "content": response}); messages.append({"role": "user", "content": response}); continue
            answer = action.content; messages.append({"role": "assistant", "content": answer}); break
        else: answer = "达到最大交互步数，已停止。"
        elapsed = (time.perf_counter() - started) * 1000
        after = snapshot(self.db_path, [order_id]) if order_id else {}
        retrievals = [asdict(c) for c in tools.calls if c.name in {"search_catalog", "get_product", "compare_products", "get_policy"}]
        trajectory = Trajectory(f"tr_{task.task_id}_{task.seed}_{uuid.uuid4().hex[:8]}", task.task_id, task.seed,
            messages=messages, retrievals=retrievals, model_calls=model_calls, tool_calls=copy.deepcopy(tools.calls),
            guardrail_spans=copy.deepcopy(tools.guardrails), handoff_spans=[asdict(c) for c in tools.calls if c.name == "escalate_to_human"],
            final_answer=answer, final_state=after, elapsed_ms=elapsed, observations=observations, actions=actions,
            user_simulator_spans=sim_spans, retry_spans=retry_spans, policy_name=type(self.policy).__name__,
            evidence_ledger=evidence_ledger, verification_spans=verification_spans, repair_spans=repair_spans,
            confirmation_spans=confirmation_spans,
            evidence_conversion_spans=evidence_conversion_spans, progress_spans=progress_spans,
            constraint_spans=constraint_spans,
            decision_spans=decision_spans,
            failed_closed=failed_closed)
        grade_result = grade(task, trajectory, leakage_checked=not isinstance(self.policy, OraclePolicy))
        return trajectory, grade_result

class TrajectoryStore:
    def __init__(self, path: Path | str):
        self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS trajectories(trajectory_id TEXT PRIMARY KEY, task_id TEXT, seed INTEGER, trajectory_json TEXT, grade_json TEXT)"); conn.commit()
        finally: conn.close()
    def save(self, trajectory: Trajectory, result: GradeResult) -> None:
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("INSERT OR REPLACE INTO trajectories VALUES(?,?,?,?,?)", (trajectory.trajectory_id, trajectory.task_id, trajectory.seed, json.dumps(trajectory.to_dict(), ensure_ascii=False), json.dumps(result.to_dict(), ensure_ascii=False))); conn.commit()
        finally: conn.close()
    def load(self, trajectory_id: str) -> tuple[dict, dict]:
        conn = sqlite3.connect(self.path)
        try:
            row = conn.execute("SELECT trajectory_json,grade_json FROM trajectories WHERE trajectory_id=?", (trajectory_id,)).fetchone()
            if not row: raise KeyError(trajectory_id)
            return json.loads(row[0]), json.loads(row[1])
        finally: conn.close()


def summarize(results: list[GradeResult], repeats: int = 1) -> dict[str, Any]:
    n = len(results) or 1; grouped: dict[str, list[bool]] = {}
    for r in results: grouped.setdefault(r.task_id, []).append(r.success)
    pass3 = [all(v[:3]) for v in grouped.values() if len(v) >= 3]
    tp=sum(r.handoff_expected and r.handoff_observed for r in results); fp=sum(not r.handoff_expected and r.handoff_observed for r in results); fn=sum(r.handoff_expected and not r.handoff_observed for r in results)
    failures: dict[str, int] = {}
    for r in results:
        if r.failure_type: failures[r.failure_type] = failures.get(r.failure_type, 0) + 1
    return {"trajectories":len(results),"tasks":len(grouped),"repeats":repeats,"task_success":sum(r.success for r in results)/n,
        "policy_compliance":sum(r.policy_compliant for r in results)/n,"tool_call_f1":sum(r.tool_f1 for r in results)/n,
        "handoff_precision":tp/(tp+fp) if tp+fp else 1.0,"handoff_recall":tp/(tp+fn) if tp+fn else 1.0,
        "average_turns":sum(r.turns for r in results)/n,"average_latency_ms":sum(r.latency_ms for r in results)/n,
        "average_reward":sum(r.reward for r in results)/n,"pass@1":sum(r.success for r in results)/n,
        "pass^3":sum(pass3)/len(pass3) if pass3 else None,"failure_taxonomy":failures,
        "leakage_checked_rate":sum(r.leakage_checked for r in results)/n,
        "terminal_state_accuracy":sum(r.terminal_state_match for r in results)/n,
        "answer_fact_applicable_rate":sum(r.answer_fact_applicable for r in results)/n,
        "answer_fact_pass_rate":(
            sum(bool(r.answer_fact_pass) for r in results if r.answer_fact_applicable)
            / sum(r.answer_fact_applicable for r in results)
            if any(r.answer_fact_applicable for r in results) else None),
        "citation_binding_pass_rate":(
            sum(bool(r.citation_binding_pass) for r in results if r.answer_fact_applicable)
            / sum(r.answer_fact_applicable for r in results)
            if any(r.answer_fact_applicable for r in results) else None),
        "joint_success":sum(r.joint_success for r in results)/n,
        "repair_attempt_rate":sum(r.repair_attempted for r in results)/n,
        "repair_success_rate":(
            sum(r.repair_succeeded for r in results if r.repair_attempted)
            / sum(r.repair_attempted for r in results)
            if any(r.repair_attempted for r in results) else None)}


def load_tasks(path: Path | str) -> list[TaskSpec]:
    return [TaskSpec(**json.loads(line)) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    parser=argparse.ArgumentParser(description="Leakage-resistant retail agent harness"); sub=parser.add_subparsers(dest="command",required=True)
    run=sub.add_parser("run"); run.add_argument("--tasks",required=True); run.add_argument("--db",required=True); run.add_argument("--store",required=True); run.add_argument("--repeats",type=int,default=3); run.add_argument("--output",required=True); run.add_argument("--seed-db",action="store_true"); run.add_argument("--index"); run.add_argument("--policy",choices=("oracle","rule","llm","native"),default="oracle"); run.add_argument("--split",choices=("calibration","dev","exploration","validation","locked","smoke")); run.add_argument("--skill", help="Enable an explicit Skill file for the native policy")
    replay=sub.add_parser("replay"); replay.add_argument("--store",required=True); replay.add_argument("--trajectory-id",required=True); replay.add_argument("--tasks"); replay.add_argument("--db"); replay.add_argument("--output"); replay.add_argument("--index"); replay.add_argument("--policy",choices=("oracle","rule"),default="oracle")
    compare=sub.add_parser("compare"); compare.add_argument("reports",nargs="+"); args=parser.parse_args()
    if args.command=="compare":
        reports=[json.loads(Path(p).read_text(encoding="utf-8")) for p in args.reports]; print(json.dumps([{"path":p,**r["summary"]} for p,r in zip(args.reports,reports)],ensure_ascii=False,indent=2)); return
    if args.command=="replay":
        original,original_grade=TrajectoryStore(args.store).load(args.trajectory_id)
        if not args.tasks or not args.db: print(json.dumps({"trajectory":original,"grade":original_grade},ensure_ascii=False,indent=2)); return
        task=next(t for t in load_tasks(args.tasks) if t.task_id==original["task_id"]); task=TaskSpec(**{**asdict(task),"seed":original["seed"]})
        retriever=None
        if args.index:
            from .hybrid_retriever import HybridRetriever
            retriever=HybridRetriever(Path(args.index))
        policy=OraclePolicy() if args.policy=="oracle" else RulePolicy(); repeated,repeated_grade=HarnessRunner(args.db,retriever,policy).run(task)
        stable=("success","policy_compliant","tool_precision","tool_recall","tool_f1","handoff_expected","handoff_observed","terminal_state_match","state_diff","turns","reward","failure_type")
        comparison={"original_id":args.trajectory_id,"replay_id":repeated.trajectory_id,"same_tool_sequence":[c["name"] for c in original["tool_calls"]]==[c.name for c in repeated.tool_calls],"same_grade":all(original_grade.get(k)==repeated_grade.to_dict().get(k) for k in stable),"grade":repeated_grade.to_dict()}
        if args.output: Path(args.output).write_text(json.dumps(comparison,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps(comparison,ensure_ascii=False,indent=2)); return
    if args.seed_db: seed_database(args.db)
    retriever=None
    if args.index:
        from .hybrid_retriever import HybridRetriever
        retriever=HybridRetriever(Path(args.index))
    if args.policy in {"llm", "native"}:
        if args.policy == "llm":
            from .llm_policy import LLMPolicy
            policy: AgentPolicy=LLMPolicy.from_env()
        elif args.policy == "native":
            from .native_tool_policy import NativeToolPolicy
            policy = NativeToolPolicy.from_env(skill_path=args.skill, skill_enabled=bool(args.skill))
    else: policy=OraclePolicy() if args.policy=="oracle" else RulePolicy()
    runner,store=HarnessRunner(args.db,retriever,policy),TrajectoryStore(args.store); results=[]; details=[]
    tasks=load_tasks(args.tasks)
    if args.split: tasks=[task for task in tasks if task.split==args.split]
    for task in tasks:
        for repeat in range(args.repeats):
            repeated=TaskSpec(**{**asdict(task),"seed":task.seed+repeat}); trajectory,result=runner.run(repeated); store.save(trajectory,result); results.append(result); details.append({"trajectory_id":trajectory.trajectory_id,**result.to_dict()})
    report={"policy":args.policy,"summary":summarize(results,args.repeats),"details":details}; Path(args.output).parent.mkdir(parents=True,exist_ok=True); Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8"); print(json.dumps(report["summary"],ensure_ascii=False,indent=2))


if __name__=="__main__": main()
