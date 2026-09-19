import tempfile
import unittest
import json
from dataclasses import asdict
from pathlib import Path

from ecommerce_rag.domain import TaskSpec
from ecommerce_rag.domain import AgentAction
from ecommerce_rag.domain import AgentObservation
from ecommerce_rag.harness import HarnessRunner, RulePolicy, _sequence_match
from ecommerce_rag.domain import ToolCall
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
        confirmation_id = tools.authorization_for(
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
