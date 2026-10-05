import pytest

from ecommerce_rag.argument_grounding import ground_search_filters
from ecommerce_rag.domain import AgentAction, SCORING_VERSION_RESEARCH_FIND_V1, TaskSpec
from ecommerce_rag.harness import HarnessRunner
from ecommerce_rag.orders import seed_database
from ecommerce_rag.query_fusion import ContextFusionRetriever
from ecommerce_rag.research_find import RESEARCH_FIND_TOOLS, RetrievalTop1Policy


def test_stated_budget_and_category_are_kept():
    arguments = {"query": "towel", "max_price": 25, "category": "Hand Towels"}
    applied, note = ground_search_filters(arguments, ["我想买一款 Hand Towels，预算 25 元以内。"])
    assert applied == arguments and note is None


def test_unstated_filters_are_dropped_and_reported():
    arguments = {"query": "Soap Dishes", "max_price": 50, "category": "Kitchen", "top_k": 5}
    applied, note = ground_search_filters(arguments, ["我想买一款 Soap Dishes，品牌是 IDesign。"])
    assert applied == {"query": "Soap Dishes", "top_k": 5}
    assert note == {"ignored_arguments": {"max_price": 50, "category": "Kitchen"}, "reason": "not_stated_by_user"}


class ListRetriever:
    def __init__(self, rankings):
        self.rankings = rankings
        self.chunks = [{"doc_id": f"product:{pid}", "product_id": pid, "title": pid, "category": "Kitchen Cases",
                        "price": price, "inventory": "unknown", "text": pid}
                       for pid, price in (("P00001", None), ("P00002", 30.0), ("P00003", None))]
        self.queries = []

    def search(self, query, top_k=5, source_type=None, category=None):
        self.queries.append(query)
        by_id = {c["product_id"]: c for c in self.chunks}
        return [dict(by_id[pid]) for pid in self.rankings.get(query, [])][:top_k]


def test_fusion_adds_products_found_only_by_the_user_message():
    base = ListRetriever({"short": ["P00002", "P00001"], "full user request": ["P00003", "P00002"]})
    fused = ContextFusionRetriever(base)
    fused.context_query = "full user request"
    ids = [c["product_id"] for c in fused.search("short", top_k=3)]
    assert ids[0] == "P00002" and set(ids) == {"P00001", "P00002", "P00003"}
    assert fused.last_fusion == {"context_fused": True, "added_by_context": ["product:P00003"]}


def test_fusion_is_a_pass_through_when_the_query_is_the_user_message():
    base = ListRetriever({"same": ["P00002", "P00001"]})
    fused = ContextFusionRetriever(base)
    fused.context_query = " same "
    assert [c["product_id"] for c in fused.search("same")] == ["P00002", "P00001"]
    assert base.queries == ["same"] and fused.last_fusion == {"context_fused": False}


class FilteringPolicy:
    privileged = False

    def act(self, observation):
        if not any(row.get("role") == "tool" for row in observation.history):
            return AgentAction.tool_call("search_catalog", query="short", max_price=50, category="Kitchen")
        return AgentAction.answer("没有符合条件的商品。")


def _task():
    return TaskSpec(
        "rf-runtime", "research_find", "U0001", "full user request", 3,
        allowed_tools=list(RESEARCH_FIND_TOOLS), scoring_version=SCORING_VERSION_RESEARCH_FIND_V1,
        evaluation_contract={"task_type": "multi_constraint", "answer_product_id": "P00003"},
    )


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "runtime.db"
    seed_database(path)
    return path


def _ranked():
    return ListRetriever({"short": ["P00002", "P00001"], "full user request": ["P00003", "P00001"]})


def test_stable_path_still_applies_invented_filters(db):
    trajectory, _ = HarnessRunner(db, _ranked(), FilteringPolicy()).run(_task())
    call = trajectory.tool_calls[0]
    assert call.arguments["max_price"] == 50
    assert [i["product_id"] for i in call.result["items"]] == ["P00002"]
    assert "argument_grounding" not in call.result and "query_fusion" not in call.result


def test_grounding_records_the_requested_and_applied_search(db):
    trajectory, _ = HarnessRunner(db, _ranked(), FilteringPolicy(), ground_search_filters=True).run(_task())
    call = trajectory.tool_calls[0]
    assert call.arguments == {"query": "short"}
    assert trajectory.actions[0]["arguments"]["max_price"] == 50
    assert call.result["argument_grounding"]["ignored_arguments"] == {"max_price": 50, "category": "Kitchen"}
    assert [i["product_id"] for i in call.result["items"]] == ["P00002", "P00001"]


def test_both_switches_surface_the_product_only_the_user_message_finds(db):
    trajectory, result = HarnessRunner(
        db, _ranked(), FilteringPolicy(), ground_search_filters=True, search_query_fusion=True,
    ).run(_task())
    call = trajectory.tool_calls[0]
    assert "P00003" in [i["product_id"] for i in call.result["items"]]
    assert call.result["query_fusion"]["context_fused"] is True
    assert result.failure_type == "false-abstention"
    assert result.answer_diagnostics["gold_retrieved"] is True


def test_top1_baseline_is_unchanged_by_query_fusion(db):
    retriever = _ranked()
    plain, _ = HarnessRunner(db, retriever, RetrievalTop1Policy()).run(_task())
    fused, _ = HarnessRunner(db, retriever, RetrievalTop1Policy(), search_query_fusion=True).run(_task())
    assert plain.tool_calls[0].result["items"] == fused.tool_calls[0].result["items"]
    assert fused.tool_calls[0].result["query_fusion"] == {"context_fused": False}
