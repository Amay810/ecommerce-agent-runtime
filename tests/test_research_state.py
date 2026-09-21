from __future__ import annotations

import json

from ecommerce_rag.domain import AgentAction, AgentObservation, TaskSpec
from ecommerce_rag.harness import HarnessRunner
from ecommerce_rag.native_tool_policy import NativeGeneration, NativeToolPolicy
from ecommerce_rag.orders import seed_database
from ecommerce_rag.research_policy import EvidenceStateRulePolicy
from ecommerce_rag.research_state import derive_research_state
from ecommerce_rag.tool_schema import TOOL_SCHEMAS


class _StaticRetriever:
    chunks = [
        {
            "doc_id": "product:P001",
            "source_type": "product",
            "product_id": "P001",
            "title": "Air Pro 2",
            "category": "耳机",
            "price": 499,
            "inventory": "现货",
            "updated_at": "2026-06-12",
            "text": "主动降噪45dB；单次8小时；IPX4；单耳4.4g。",
        },
        {
            "doc_id": "product:P007",
            "source_type": "product",
            "product_id": "P007",
            "title": "RunBuds Clip",
            "category": "耳机",
            "price": 359,
            "inventory": "现货",
            "updated_at": "2026-06-12",
            "text": "耳挂式；单次10小时；IPX6；单耳6.8g。",
        },
    ]

    def search(self, _query, top_k=5, source_type=None, category=None):
        rows = [row for row in self.chunks if not source_type or row["source_type"] == source_type]
        if category:
            rows = [row for row in rows if category in row["category"]]
        return rows[:top_k]


def _product_ledger(*sources):
    rows = []
    for index, source in enumerate(sources, 1):
        rows.extend([
            {
                "evidence_id": f"E{index}",
                "source_id": source,
                "tool_name": "get_product",
                "field": "product.title",
                "value": source,
                "text": source,
            },
            {
                "evidence_id": f"E{index + 10}",
                "source_id": source,
                "tool_name": "get_product",
                "field": "product.evidence.1",
                "value": "attribute evidence",
                "text": "attribute evidence",
            },
        ])
    return rows


def test_state_tracks_constraints_queries_sources_and_gaps_without_task_gold():
    history = [
        {"role": "user", "content": "预算600元，比较两款耳机并核对退货政策，验证码123456"},
        {
            "role": "assistant",
            "action": "tool_call",
            "tool_name": "search_catalog",
            "arguments": {"query": "耳机", "top_k": 5},
        },
        {
            "role": "tool",
            "name": "search_catalog",
            "result": {"ok": True, "items": [{"product_id": "P001", "doc_id": "product:P001"}]},
        },
    ]
    state = derive_research_state(history, [], budget=3).to_dict()
    assert state["user_constraints"]["budgets"] == [600.0]
    assert set(state["user_constraints"]["requested_facets"]) >= {"product", "comparison", "policy"}
    assert state["executed_queries"][0]["arguments"] == {"query": "耳机", "top_k": 5}
    assert state["remaining_budget"] == 2
    assert {gap["kind"] for gap in state["missing_evidence"]} >= {"comparison_coverage", "policy_clause"}
    assert "123456" not in json.dumps(state, ensure_ascii=False)


def test_state_redacts_standalone_codes_without_erasing_order_ids():
    state = derive_research_state(
        [{"role": "user", "content": "查询订单 O000003，验证码 814752"}],
        [],
        budget=2,
    ).to_dict()
    assert "O000003" in state["user_constraints"]["request"]
    assert "814752" not in state["user_constraints"]["request"]


def test_state_becomes_answerable_after_independent_product_evidence():
    history = [
        {"role": "user", "content": "比较两款耳机的续航和重量"},
        {"role": "assistant", "action": "tool_call", "tool_name": "compare_products", "arguments": {"product_ids": ["P001", "P007"]}},
        {"role": "tool", "name": "compare_products", "result": {"ok": True, "products": [{"product": {"product_id": "P001"}}, {"product": {"product_id": "P007"}}]}},
    ]
    state = derive_research_state(history, _product_ledger("product:P001", "product:P007"), budget=2)
    assert state.status == "ready_to_answer"
    assert state.remaining_budget == 1
    assert len(state.obtained_facts) == 4
    assert not state.missing_evidence


def test_native_baseline_and_research_context_are_distinguishable():
    captured = []

    def generate(messages, _tools):
        captured.append(messages)
        return NativeGeneration(content="已完成。")

    observation = AgentObservation(
        current_message="比较耳机",
        session={"user_id": "U0001"},
        history=[{"role": "user", "content": "比较耳机"}],
        tool_schemas=TOOL_SCHEMAS,
        research_state={
            "user_constraints": {"requested_facets": ["product", "comparison"]},
            "obtained_facts": [{"evidence_id": "E1", "source_id": "product:P001"}],
            "missing_evidence": [{"kind": "comparison_coverage"}],
            "executed_queries": [],
            "retrieval_calls": 0,
            "remaining_budget": 2,
            "status": "needs_more_evidence",
        },
    )
    baseline = NativeToolPolicy(generate, research_context=False)
    baseline.act(observation)
    assert baseline.uses_evidence is False
    assert "research_state" not in captured[-1][0]["content"]
    research = NativeToolPolicy(generate, research_context=True)
    research.act(observation)
    assert research.uses_evidence is False
    assert "<research_state>" in captured[-1][0]["content"]
    assert "comparison_coverage" in captured[-1][0]["content"]


def test_evidence_rule_policy_continues_and_harness_keeps_task_isolated(tmp_path):
    db = tmp_path / "env.sqlite"
    seed_database(db, users=20, orders=20)
    task = TaskSpec(
        "research_wire", "complex_research", "U0001", "比较 Air Pro 2 和 RunBuds Clip 的续航和重量", 1,
        gold_doc_ids=["product:P001", "product:P007"],
        allowed_tools=["search_catalog", "compare_products"],
        research_budget=3,
    )
    trajectory, result = HarnessRunner(
        db, _StaticRetriever(), EvidenceStateRulePolicy(), max_steps=6,
        research_enabled=True,
    ).run(task)
    assert [call.name for call in trajectory.tool_calls] == ["search_catalog", "compare_products"]
    assert result.leakage_checked
    assert trajectory.research_spans[0]["state_before"]["remaining_budget"] == 3
    assert trajectory.research_spans[1]["state_before"]["retrieval_calls"] == 1
    assert trajectory.research_spans[-1]["outcome"]["kind"] == "final_answer"
    assert trajectory.observations[1]["research_state"]["executed_queries"][0]["tool_name"] == "search_catalog"


def test_evidence_rule_policy_does_not_force_retrieval_for_general_question(tmp_path):
    db = tmp_path / "general.sqlite"
    seed_database(db, users=20, orders=20)
    task = TaskSpec("general_wire", "complex_research", "U0001", "你好，请说明你能做什么。", 3)
    trajectory, _ = HarnessRunner(
        db, _StaticRetriever(), EvidenceStateRulePolicy(), max_steps=3,
        research_enabled=True,
    ).run(task)
    assert trajectory.tool_calls == []
    assert trajectory.research_spans[-1]["outcome"]["kind"] == "final_answer"


def test_budget_guard_stops_extra_read_only_calls(tmp_path):
    class Oversearch:
        uses_evidence = True
        uses_research_state = True

        def act(self, _observation):
            return AgentAction.tool_call("search_catalog", query="耳机", top_k=1)

    db = tmp_path / "env.sqlite"
    seed_database(db, users=20, orders=20)
    task = TaskSpec("budget_wire", "complex_research", "U0001", "找耳机", 2, research_budget=1)
    trajectory, _ = HarnessRunner(
        db, _StaticRetriever(), Oversearch(), max_steps=5,
        research_enabled=True,
    ).run(task)
    assert len(trajectory.tool_calls) == 2
    assert trajectory.tool_calls[0].result["ok"] is True
    assert trajectory.tool_calls[1].result["error"] == "research_budget_exhausted"
    assert trajectory.research_spans[-1]["outcome"]["kind"] == "budget_exhausted"
    assert "无法确认" in trajectory.final_answer


def test_evaluation_contract_never_enters_research_observation(tmp_path):
    class Spy:
        uses_evidence = True
        uses_research_state = True

        def __init__(self):
            self.seen = None

        def act(self, observation):
            self.seen = observation
            return AgentAction.answer("done")

    db = tmp_path / "env.sqlite"
    seed_database(db, users=20, orders=20)
    policy = Spy()
    task = TaskSpec(
        "contract_hidden", "complex_research", "U0001", "查一下商品", 3,
        answer_expectations={"required_fact_keys": ["SECRET_ANSWER"]},
        evaluation_contract={"necessary_facts": ["SECRET_FACT"], "sources": ["SECRET_SOURCE"]},
        gold_doc_ids=["SECRET_GOLD"],
        allowed_tools=["search_catalog"],
        research_budget=2,
    )
    HarnessRunner(db, _StaticRetriever(), policy, research_enabled=True).run(task)
    payload = json.dumps(policy.seen.research_state, ensure_ascii=False)
    assert all(secret not in payload for secret in ("SECRET_ANSWER", "SECRET_FACT", "SECRET_SOURCE", "SECRET_GOLD"))
