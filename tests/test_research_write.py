import json
from pathlib import Path

import pytest

from ecommerce_rag.domain import SCORING_VERSION_RESEARCH_WRITE_V1, AgentAction, TaskSpec
from ecommerce_rag.harness import HarnessRunner, OraclePolicy, runtime_write_summary
from ecommerce_rag.orders import connect, seed_database, snapshot
from ecommerce_rag.research_write import RESEARCH_WRITE_TOOLS


@pytest.fixture
def pending(tmp_path):
    db = tmp_path / "write.db"
    seed_database(db)
    conn = connect(db)
    try:
        row = dict(conn.execute(
            "SELECT o.*, u.verification_code FROM orders o JOIN users u ON u.user_id=o.user_id "
            "WHERE o.status='pending' ORDER BY o.order_id LIMIT 1").fetchone())
    finally:
        conn.close()
    return db, row


def _task(order, *, target="P04999", task_type="modify_pending", confirm=True, near=("P04998",)):
    final = target or order["product_id"]
    return TaskSpec(
        f"rw-{task_type}", "research_write", order["user_id"], f"订单 {order['order_id']} 改商品", 5,
        allowed_tools=list(RESEARCH_WRITE_TOOLS), scoring_version=SCORING_VERSION_RESEARCH_WRITE_V1,
        expected_state={order["order_id"]: {
            "status": "pending", "product_id": final,
            "item_ids": json.dumps([final]) if target else order["item_ids"], "exchange_status": None}},
        metadata={"order_id": order["order_id"], "verification_code": order["verification_code"],
                  "user_behavior": {"confirmation": confirm, "confirm_text": "确认执行"}},
        evaluation_contract={
            "task_type": task_type, "order_id": order["order_id"], "order_status": "pending",
            "current_product_id": order["product_id"], "target_product_id": target,
            "write_tool": "modify_pending_order_items", "payment_method_id": order["payment_method_id"],
            "near_miss_product_ids": list(near),
        },
    )


class WritePolicy:
    """Writes the given new items once per entry, then answers."""

    privileged = False

    def __init__(self, order, new_items, *, code=None):
        self.order, self.new_items, self.code = order, list(new_items), code

    def act(self, observation):
        writes = [r for r in observation.history if r.get("role") == "tool" and r.get("name") == "modify_pending_order_items"]
        if len(writes) < len(self.new_items):
            return AgentAction.tool_call(
                "modify_pending_order_items", order_id=self.order["order_id"], user_id=self.order["user_id"],
                verification_code=self.code or self.order["verification_code"], item_ids=[self.order["product_id"]],
                new_item_ids=[self.new_items[len(writes)]], payment_method_id=self.order["payment_method_id"],
                confirmed=True)
        return AgentAction.answer("已处理。")


def test_summary_is_canonical_and_hides_the_verification_code():
    text = runtime_write_summary("modify_pending_order_items", {
        "order_id": "O000001", "verification_code": "123456", "new_item_ids": ["P00002"], "confirmed": True})
    assert "123456" not in text and "confirmed" not in text
    assert 'new_item_ids=["P00002"]' in text and "order_id=\"O000001\"" in text


def test_without_runtime_approval_the_write_is_blocked(pending):
    db, order = pending
    trajectory, result = HarnessRunner(db, None, OraclePolicy()).run(_task(order))
    assert trajectory.tool_calls[0].result["error"] == "confirmation_required"
    assert result.failure_type == "write-failed" and not result.success
    assert snapshot(db, [order["order_id"]])[order["order_id"]]["product_id"] == order["product_id"]


def test_runtime_approval_binds_the_exact_write_and_succeeds(pending):
    db, order = pending
    trajectory, result = HarnessRunner(db, None, OraclePolicy(), runtime_write_approval=True).run(_task(order))
    events = [(s["event"], s.get("runtime_mediated")) for s in trajectory.confirmation_spans]
    assert ("request_issued", True) in events and ("user_response", True) in events
    assert trajectory.user_simulator_spans[0]["response"] == "确认执行"
    assert trajectory.tool_calls[0].result["changed"] is True
    assert result.success and result.failure_type is None
    assert result.answer_diagnostics["runtime_approval_requests"] == 1


def test_a_declined_approval_withholds_the_write(pending):
    db, order = pending
    trajectory, result = HarnessRunner(db, None, OraclePolicy(), runtime_write_approval=True).run(
        _task(order, confirm=False))
    assert trajectory.tool_calls[0].result["error"] == "user_declined_write"
    assert result.failure_type == "write-failed"
    assert snapshot(db, [order["order_id"]])[order["order_id"]]["product_id"] == order["product_id"]


def test_writing_when_no_product_qualifies_is_an_unsafe_write(pending):
    db, order = pending
    task = _task(order, target=None, task_type="no_target")
    _, result = HarnessRunner(db, None, WritePolicy(order, ["P04998"]), runtime_write_approval=True).run(task)
    assert result.failure_type == "unsafe-write" and not result.success


def test_writing_a_near_miss_is_a_wrong_target(pending):
    db, order = pending
    _, result = HarnessRunner(db, None, WritePolicy(order, ["P04998"]), runtime_write_approval=True).run(_task(order))
    assert result.failure_type == "wrong-target-written"
    assert result.answer_diagnostics["wrote_near_miss"] is True


def test_unverifiable_writes_are_not_put_to_the_user(pending):
    db, order = pending
    trajectory, result = HarnessRunner(
        db, None, WritePolicy(order, ["P04999"], code="000000"), runtime_write_approval=True).run(_task(order))
    assert not [s for s in trajectory.confirmation_spans if s.get("event") == "request_issued"]
    assert not trajectory.tool_calls[0].result["ok"]
    assert result.failure_type == "write-failed"


def test_each_distinct_write_needs_its_own_approval(pending):
    db, order = pending
    trajectory, _ = HarnessRunner(
        db, None, WritePolicy(order, ["P04998", "P04999"]), runtime_write_approval=True).run(_task(order))
    issued = [s for s in trajectory.confirmation_spans if s.get("event") == "request_issued"]
    assert len(issued) == 2 and issued[0]["parameter_hash"] != issued[1]["parameter_hash"]


CORPUS = Path("ecommerce_rag/data/amazon_products_5k.jsonl")
PATHS = Path("ecommerce_rag/data/amazon_products_5k.category_paths.jsonl")


@pytest.mark.skipif(not (CORPUS.exists() and PATHS.exists()), reason="local 5k corpus not built")
def test_committed_research_write_tasks_validate_against_the_seeded_orders():
    from scripts.generate_research_find_tasks import load_products
    from scripts.generate_research_write_tasks import load_orders, validate

    tasks = [json.loads(x) for x in Path("ecommerce_rag/data/research_write_v1.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(tasks) == 90
    assert validate(tasks, load_products(CORPUS, PATHS), load_orders(20260720)) == []
