import tempfile
import unittest
import json
from dataclasses import asdict
from pathlib import Path

from ecommerce_rag.domain import (
    SCORING_VERSION_HARNESS_V2_TERMINAL,
    SCORING_VERSION_HARNESS_V2_TOOL_CONTRACT,
    TaskSpec,
)
from ecommerce_rag.domain import AgentAction
from ecommerce_rag.domain import AgentObservation
from ecommerce_rag.harness import HarnessRunner, RulePolicy, _sequence_match, grade
from ecommerce_rag.domain import ToolCall, Trajectory
from ecommerce_rag.orders import connect, seed_database
from ecommerce_rag.tools import RetailTools


def _eligible(db):
    conn = connect(db)
    try:
        order = dict(conn.execute("SELECT * FROM orders WHERE status='delivered' AND quality_issue=1 LIMIT 1").fetchone())
        code = conn.execute("SELECT verification_code FROM users WHERE user_id=?", (order["user_id"],)).fetchone()[0]
        return order, code
    finally:
        conn.close()


class HarnessToolTests(unittest.TestCase):
 def test_tool_contract_separates_allowed_from_required_tools(self):
    task = TaskSpec(
        "tool-contract", "lookup", "U0001", "查询", 1,
        allowed_tools=["get_policy", "get_order"],
        required_tools=["get_policy"],
        output_requirements={"citation_format": "[E#]"},
        scoring_version=SCORING_VERSION_HARNESS_V2_TOOL_CONTRACT,
    )
    result = grade(task, Trajectory(
        "tr-tool-contract", task.task_id, 1,
        final_answer="已完成。", termination_reason="final_answer",
        tool_calls=[ToolCall("get_policy", {}, "policy", {"ok": True}, "now")],
    ))
    self.assertTrue(result.success)
    self.assertEqual(result.tool_recall, 1.0)
    self.assertFalse(result.unexpected_tool_attempt)
    self.assertEqual(result.scoring_version, SCORING_VERSION_HARNESS_V2_TOOL_CONTRACT)

 def test_tool_contract_rejects_a_tool_outside_allowed_set(self):
    task = TaskSpec(
        "tool-contract-boundary", "lookup", "U0001", "查询", 1,
        allowed_tools=["get_policy"], required_tools=["get_policy"],
        scoring_version=SCORING_VERSION_HARNESS_V2_TOOL_CONTRACT,
    )
    result = grade(task, Trajectory(
        "tr-tool-contract-boundary", task.task_id, 1,
        final_answer="已完成。", termination_reason="final_answer",
        tool_calls=[
            ToolCall("get_policy", {}, "policy", {"ok": True}, "now"),
            ToolCall("get_order", {}, "order", {"ok": True}, "now"),
        ],
    ))
    self.assertFalse(result.success)
    self.assertTrue(result.unexpected_tool_attempt)
    self.assertEqual(result.failure_type, "unexpected-tool-attempt")

 def test_tool_contract_exposes_only_public_output_requirements_and_allowed_tools(self):
    class CapturePolicy:
     def act(self, observation):
      self.observation = observation
      return AgentAction.answer("已完成。")

    policy = CapturePolicy()
    task = TaskSpec(
        "tool-contract-observation", "lookup", "U0001", "查询", 1,
        allowed_tools=["get_policy"],
        output_requirements={"citation_format": "[E#]", "cite_factual_claims": True},
        scoring_version=SCORING_VERSION_HARNESS_V2_TOOL_CONTRACT,
    )
    with tempfile.TemporaryDirectory() as directory:
        HarnessRunner(Path(directory) / "contract.sqlite", policy=policy).run(task)
    self.assertEqual([schema["name"] for schema in policy.observation.tool_schemas], ["get_policy"])
    self.assertEqual(policy.observation.output_requirements, task.output_requirements)

 def test_harness_v1_preserves_operational_success_without_final_answer(self):
    task = TaskSpec("v1-terminal-compat", "lookup", "U0001", "查询", 1)
    result = grade(task, Trajectory("tr-v1", task.task_id, 1))
    self.assertTrue(result.success)
    self.assertFalse(result.final_answer_present)
    self.assertFalse(result.terminal_completion)
    self.assertEqual(result.scoring_version, "harness-v1")

 def test_harness_v2_terminal_requires_answer_or_handoff(self):
    task = TaskSpec(
        "v2-terminal", "lookup", "U0001", "查询", 1,
        scoring_version=SCORING_VERSION_HARNESS_V2_TERMINAL,
    )
    incomplete = grade(task, Trajectory("tr-v2-incomplete", task.task_id, 1))
    complete = grade(task, Trajectory(
        "tr-v2-complete", task.task_id, 1,
        final_answer="已完成。", termination_reason="final_answer",
    ))
    self.assertFalse(incomplete.success)
    self.assertEqual(incomplete.failure_type, "incomplete-terminal")
    self.assertFalse(incomplete.terminal_completion)
    self.assertTrue(complete.success)
    self.assertTrue(complete.final_answer_present)
    self.assertTrue(complete.terminal_completion)
    self.assertEqual(complete.scoring_version, SCORING_VERSION_HARNESS_V2_TERMINAL)

 def test_harness_v2_terminal_accepts_successful_handoff_without_answer(self):
    task = TaskSpec(
        "v2-handoff", "lookup", "U0001", "转人工", 1,
        metadata={"handoff_expected": True},
        scoring_version=SCORING_VERSION_HARNESS_V2_TERMINAL,
    )
    trajectory = Trajectory(
        "tr-v2-handoff", task.task_id, 1,
        tool_calls=[ToolCall("escalate_to_human", {}, "handoff", {"ok": True}, "now")],
        termination_reason="handoff",
    )
    result = grade(task, trajectory)
    self.assertTrue(result.success)
    self.assertFalse(result.final_answer_present)
    self.assertTrue(result.terminal_completion)

 def test_harness_v2_rejects_max_steps_even_with_stop_text(self):
    class ToolLoop:
     def act(self, _observation):
      return AgentAction.tool_call("get_policy", policy_type="return")

    task = TaskSpec(
        "v2-max-steps", "lookup", "U0001", "查询", 1,
        allowed_tools=["get_policy"],
        scoring_version=SCORING_VERSION_HARNESS_V2_TERMINAL,
    )
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "max-steps.sqlite"
        seed_database(db)
        trajectory, result = HarnessRunner(db, policy=ToolLoop(), max_steps=1).run(task)
    self.assertEqual(trajectory.termination_reason, "max_steps")
    self.assertTrue(result.final_answer_present)
    self.assertFalse(result.terminal_completion)
    self.assertFalse(result.success)
    self.assertEqual(result.failure_type, "incomplete-terminal")

 def test_v1_and_v2_both_reject_last_required_tool_failure(self):
    calls = [ToolCall("get_policy", {"policy_type": "return"}, "failed", {"ok": False}, "now")]
    for version in ("harness-v1", SCORING_VERSION_HARNESS_V2_TERMINAL):
        with self.subTest(version=version):
            task = TaskSpec(
                "last-tool-failed", "lookup", "U0001", "查询", 1,
                allowed_tools=["get_policy"], scoring_version=version,
            )
            result = grade(task, Trajectory(
                f"tr-{version}", task.task_id, 1, final_answer="无法完成。",
                termination_reason="final_answer", tool_calls=calls,
            ))
            self.assertFalse(result.success)
            self.assertEqual(result.failure_type, "required-tool-failed")

 def test_v1_and_v2_continue_after_typed_user_input(self):
    class AskThenFinish:
     def __init__(self):
      self.calls = 0

     def act(self, _observation):
      self.calls += 1
      if self.calls == 1:
       return AgentAction.answer(
           "请提供六位验证码。", requires_user_response=True,
           requested_input_type="verification_code",
       )
      return AgentAction.answer("已继续完成。")

    for version in ("harness-v1", SCORING_VERSION_HARNESS_V2_TERMINAL):
        with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
            task = TaskSpec(
                f"typed-input-{version}", "lookup", "U0001", "查询", 1,
                scoring_version=version,
            )
            trajectory, result = HarnessRunner(
                Path(directory) / "typed.sqlite", policy=AskThenFinish(), max_steps=3,
            ).run(task)
            self.assertTrue(result.success)
            self.assertEqual(trajectory.termination_reason, "final_answer")
            self.assertEqual(trajectory.user_simulator_spans[0]["response"], "000000")
            self.assertEqual(
                [message["role"] for message in trajectory.messages],
                ["user", "assistant", "user", "assistant"],
            )

 def _return_v2_task(self, *, write=True):
    return TaskSpec(
        "return_v2", "return", "U0001", "退货", 1,
        allowed_tools=["get_policy", "get_order", "check_return_eligibility"]
                     + (["create_return_request"] if write else []),
        expected_state={"O000001": {"return_status": "requested" if write else None}},
        metadata={
            "return_required_tools": ["get_policy", "check_return_eligibility"],
            "return_write_expected": write,
        },
        scoring_version="return-closure-v2",
    )

 def _return_v2_trajectory(self, *, include_policy=True, confirmed=True):
    calls = []
    if include_policy:
        calls.append(ToolCall(
            "get_policy", {}, "policy", {"ok": True, "policies": [{"doc_id": "policy:POL001"}]}, "now"
        ))
    calls.append(ToolCall(
        "check_return_eligibility", {}, "eligibility",
        {"ok": True, "eligible": True, "order": {"order_id": "O000001"}}, "now"
    ))
    calls.append(ToolCall(
        "create_return_request", {}, "write",
        {"ok": True, "changed": True}, "now"
    ))
    spans = ([
        {"event": "request_issued", "operation": "create_return_request"},
        {"event": "user_response", "decision": True},
    ] if confirmed else [])
    return Trajectory(
        "tr-v2", "return_v2", 1, final_answer="已处理。",
        final_state={"O000001": {"return_status": "requested"}},
        tool_calls=calls, confirmation_spans=spans,
    )

 def test_return_v2_does_not_require_redundant_get_order(self):
    result = grade(self._return_v2_task(), self._return_v2_trajectory())
    self.assertTrue(result.success)
    self.assertEqual(result.scoring_version, "return-closure-v2")
    self.assertTrue(result.required_facts_pass)

 def test_return_v2_requires_policy_and_eligibility_facts(self):
    result = grade(
        self._return_v2_task(),
        self._return_v2_trajectory(include_policy=False),
    )
    self.assertFalse(result.success)
    self.assertFalse(result.required_facts_pass)
    self.assertEqual(result.failure_type, "required-facts-missing")

 def test_return_v2_requires_confirmation_for_a_successful_write(self):
    result = grade(
        self._return_v2_task(),
        self._return_v2_trajectory(confirmed=False),
    )
    self.assertFalse(result.success)
    self.assertFalse(result.confirmation_protocol_pass)
    self.assertEqual(result.failure_type, "confirmation-protocol-failure")

 def test_return_v2_rejects_unexpected_write_attempt(self):
    result = grade(self._return_v2_task(write=False), self._return_v2_trajectory())
    self.assertFalse(result.success)
    self.assertFalse(result.policy_compliant)
    self.assertTrue(result.unexpected_tool_attempt)
    self.assertEqual(result.failure_type, "unexpected-tool-attempt")

 def test_plain_text_confirmation_request_is_classified_without_auto_correction(self):
    task = self._return_v2_task(write=False)
    trajectory = Trajectory(
        "tr-protocol", "return_v2", 1,
        final_answer="订单符合条件，是否确认提交？",
        final_state={"O000001": {"return_status": None}},
        tool_calls=[
            ToolCall("get_policy", {}, "policy", {"ok": True, "policies": [{"doc_id": "policy:POL001"}]}, "now"),
            ToolCall("check_return_eligibility", {}, "eligibility",
                     {"ok": True, "eligible": True, "order": {"order_id": "O000001"}}, "now"),
        ],
        actions=[{
            "action_type": "final_answer",
            "content": "订单符合条件，是否确认提交？",
            "requires_user_response": False,
        }],
    )
    result = grade(task, trajectory)
    self.assertFalse(result.success)
    self.assertTrue(result.interaction_protocol_failure)
    self.assertEqual(result.failure_type, "interaction-protocol-failure")

 def test_expected_tool_sequence_is_an_ordered_successful_subsequence(self):
    def call(name, ok=True):
        arguments = {"product_id": "P00001"} if name == "get_product" else {}
        result = {"ok": ok, "items": [{"product_id": "P00001"}]} if name == "search_catalog" else {"ok": ok}
        return ToolCall(name, arguments, name, result, "now")
    self.assertEqual(_sequence_match(["search_catalog", "get_product"],
                                     [call("search_catalog"), call("search_catalog"), call("get_product")]),
                     (True, None))
    self.assertEqual(_sequence_match(["search_catalog", "get_product"],
                                     [call("get_product"), call("search_catalog")]),
                     (False, "wrong-tool-order"))
    self.assertEqual(_sequence_match(["search_catalog", "get_product"], [call("search_catalog")]),
                     (False, "missing-required-tool"))
 def test_explicit_policy_language_precedes_order_and_return_keywords(self):
    policy = RulePolicy()
    cases = {
        "我想了解物流，有没有正式规则": "shipping",
        "别猜，查一下退款规定": "refund",
        "别猜，查一下物流规定": "shipping",
    }
    for message, expected_type in cases.items():
        observation = AgentObservation(
            current_message=message,
            history=[{"role": "user", "content": message}],
            tool_schemas=[],
            session={"user_id": "U0001"},
        )
        action = policy.act(observation)
        assert action.tool_name == "get_policy"
        assert action.arguments["policy_type"] == expected_type

 def test_seed_database_creates_missing_parent_directory(self):
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "missing" / "nested" / "env.db"
        seed_database(db, users=20, orders=100)
        assert db.is_file()

 def test_policy_observation_excludes_hidden_gold(self):
    class SpyPolicy:
        privileged = False
        observed = None
        def act(self, observation):
            self.observed = asdict(observation)
            return AgentAction.answer("done")
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "env.db"; seed_database(db, users=20, orders=100)
        policy = SpyPolicy()
        task = TaskSpec("hidden_01", "secret_category", "U0001", "hello", 7,
                        gold_doc_ids=["secret_doc"], allowed_tools=[], expected_state={},
                        metadata={"answer": "secret", "verification_code": "123456"}, split="locked",
                        answer_expectations={"required_fact_keys": ["secret.answer"]},
                        expected_tool_sequence=["secret_tool_a", "secret_tool_b"])
        _, result = HarnessRunner(db, policy=policy).run(task)
        payload = str(policy.observed)
        assert result.leakage_checked
        for secret in ("secret_category", "secret_doc", "secret", "123456", "allowed_tools", "expected_state",
                       "secret.answer", "answer_expectations", "expected_tool_sequence", "secret_tool_a"):
            assert secret not in payload

 def test_rule_policy_gets_verification_and_confirmation_from_user_simulator(self):
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "env.db"; seed_database(db, users=20, orders=100)
        order, code = _eligible(db)
        task = TaskSpec("return_hidden", "return", order["user_id"], f"订单 {order['order_id']} 想退货", 8,
                        allowed_tools=["get_policy", "get_order", "check_return_eligibility", "create_return_request"],
                        expected_state={order["order_id"]: {"return_status": "requested"}},
                        initial_state={order["order_id"]: {"return_status": None, "version": 0}},
                        metadata={"order_id": order["order_id"], "verification_code": code,
                                  "user_behavior": {"verification_code": code, "confirmation": True}}, split="locked")
        trajectory, result = HarnessRunner(db, policy=RulePolicy()).run(task)
        assert result.success and result.leakage_checked
        assert len(trajectory.user_simulator_spans) == 2
        assert [c.name for c in trajectory.tool_calls] == [
            "get_policy", "get_order", "check_return_eligibility", "create_return_request"
        ]

 def test_illegal_return_is_blocked_without_state_change(self):
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "env.db"; seed_database(db, users=20, orders=100)
        order, _ = _eligible(db); tools = RetailTools(db)
        result = tools.call("create_return_request", order_id=order["order_id"], user_id=order["user_id"], verification_code="wrong", confirmed=True)
        assert result["changed"] is False
        conn = connect(db)
        try: assert conn.execute("SELECT return_status FROM orders WHERE order_id=?", (order["order_id"],)).fetchone()[0] is None
        finally: conn.close()


 def test_write_requires_confirmation_and_is_idempotent(self):
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "env.db"; seed_database(db, users=20, orders=100)
        order, code = _eligible(db); tools = RetailTools(db)
        blocked = tools.call("create_return_request", order_id=order["order_id"], user_id=order["user_id"], verification_code=code, confirmed=False)
        assert blocked["changed"] is False and blocked["error"] == "confirmation_required"
        args = {"order_id": order["order_id"], "user_id": order["user_id"],
                "verification_code": code, "confirmed": True}
        tools.issue_confirmation(session_id="test", user_id=order["user_id"],
                                 operation="create_return_request", arguments=args,
                                 request_text="确认提交退货？")
        assert tools.record_user_confirmation(session_id="test",
                                              response_text="确认提交退货")["decision"] is True
        confirmation_id = tools.authorization_for_current_state(
            session_id="test", user_id=order["user_id"],
            operation="create_return_request", arguments=args)
        ok = tools.call("create_return_request", _session_id="test",
                        _confirmation_id=confirmation_id, **args)
        again = tools.call("create_return_request", _session_id="test",
                           _confirmation_id=confirmation_id, **args)
        assert ok["changed"] is True and again["changed"] is False
        assert ok["ok"] is True and again["ok"] is True
        assert again["idempotent_replay"] is True
        assert ok["request_id"] == again["request_id"] == f"RR-{order['order_id']}"


 def test_harness_terminal_state_is_deterministic(self):
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "env.db"; seed_database(db, users=20, orders=100)
        order, code = _eligible(db)
        task = TaskSpec("return_01", "return", order["user_id"], "退货", 7,
                        allowed_tools=["check_return_eligibility", "create_return_request"],
                        expected_state={order["order_id"]: {"return_status": "requested"}},
                        initial_state={order["order_id"]: {"return_status": None, "version": 0}},
                        metadata={"order_id": order["order_id"], "verification_code": code, "confirmed": True})
        runner = HarnessRunner(db)
        _, first = runner.run(task); _, second = runner.run(task)
        assert first.success and second.success and first.reward == second.reward

 def test_policy_tasks_declare_the_exact_gold_document(self):
    mapping = {"退换货": "policy:POL001", "保修": "policy:POL002", "物流": "policy:POL003",
               "发票": "policy:POL004", "退款": "policy:POL005"}
    path = Path(__file__).parents[1] / "ecommerce_rag" / "data" / "harness_tasks_v2.jsonl"
    tasks = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    policies = [task for task in tasks if task["category"] == "policy"]
    assert len(policies) == 20
    for task in policies:
        assert task["gold_doc_ids"] == [mapping[task["metadata"]["policy_type"]]]
