import json
import random
from pathlib import Path

import pytest

from ecommerce_rag.domain import SCORING_VERSION_RESEARCH_FIND_V1, TaskSpec, ToolCall, Trajectory
from ecommerce_rag.harness import HarnessRunner, OraclePolicy, grade
from ecommerce_rag.orders import seed_database
from ecommerce_rag.research_find import RESEARCH_FIND_TOOLS, RetrievalTop1Policy, answer_product_ids
from scripts.generate_research_find_tasks import (
    Catalog,
    Constraint,
    Product,
    find_answerable,
    find_unsatisfiable,
    generate,
    minimal_unique,
    norm,
    typo,
    validate_tasks,
)


def _task(answer="P00002", task_type="near_sku", near=("P00003",)):
    return TaskSpec(
        f"rf-{task_type}", "research_find", "U0001", "请帮我找一个 Earbuds：品牌是 Acme，颜色是 Blue。", 7,
        gold_doc_ids=[f"product:{answer}"] if answer else [],
        allowed_tools=list(RESEARCH_FIND_TOOLS),
        scoring_version=SCORING_VERSION_RESEARCH_FIND_V1,
        output_requirements={"final_answer": "只给出一个商品编号"},
        evaluation_contract={
            "task_type": task_type, "answer_product_id": answer,
            "near_miss_product_ids": list(near),
        },
    )


def _search(*product_ids):
    items = [{"product_id": pid, "doc_id": f"product:{pid}"} for pid in product_ids]
    return ToolCall("search_catalog", {"query": "Acme Blue"}, "s1", {"ok": True, "items": items}, "now")


def _trajectory(task, answer, calls=(), termination="final_answer"):
    return Trajectory("tr", task.task_id, task.seed, final_answer=answer,
                      termination_reason=termination, tool_calls=list(calls))


def test_answer_product_ids_are_distinct_and_ordered():
    assert answer_product_ids("选 p00002，不是 P00003；再说一次 P00002") == ["P00002", "P00003"]
    assert answer_product_ids("型号 XP000021 和 P0000 不算") == []


@pytest.mark.parametrize("answer,calls,termination,failure", [
    ("P00002", [_search("P00003", "P00002")], "final_answer", None),
    ("P00002 或 P00003", [_search("P00003", "P00002")], "final_answer", "multiple-products-answered"),
    ("没有符合条件的商品", [_search("P00003")], "final_answer", "false-abstention"),
    ("P00003", [_search("P00003", "P00002")], "final_answer", "retrieved-not-selected"),
    ("P00003", [_search("P00003")], "final_answer", "never-retrieved"),
    ("达到最大交互步数，已停止。", [_search("P00003")], "max_steps", "step-limit"),
    ("请提供更多信息", [], "user_input_unavailable", "no-final-answer"),
])
def test_research_find_answer_level_scoring(answer, calls, termination, failure):
    task = _task()
    result = grade(task, _trajectory(task, answer, calls, termination))
    assert result.success is (failure is None)
    assert result.operational_success is (failure is None)
    assert result.failure_type == failure
    assert result.scoring_version == SCORING_VERSION_RESEARCH_FIND_V1


def test_retrieving_the_gold_product_is_not_enough():
    task = _task()
    result = grade(task, _trajectory(task, "P00003", [_search("P00002", "P00003")]))
    assert result.success is False
    assert result.answer_diagnostics["gold_retrieved"] is True
    assert result.answer_diagnostics["gold_best_search_rank"] == 1
    assert result.answer_diagnostics["answered_near_miss"] is True


@pytest.mark.parametrize("answer,failure", [
    ("商品资料中没有明确满足全部条件的商品。", None),
    ("P00003 最接近。", "answered-unsatisfiable"),
    ("这几款都还不错。", "no-clear-abstention"),
])
def test_no_answer_tasks_require_explicit_abstention(answer, failure):
    task = _task(answer=None, task_type="no_answer")
    result = grade(task, _trajectory(task, answer, [_search("P00003")]))
    assert result.failure_type == failure
    assert result.abstention_expected is True


def test_tools_outside_the_research_surface_fail_compliance():
    task = _task()
    calls = [_search("P00002"), ToolCall("get_order", {}, "o1", {"ok": True}, "now")]
    result = grade(task, _trajectory(task, "P00002", calls))
    assert result.success is False
    assert result.failure_type == "unexpected-tool-attempt"


class FakeRetriever:
    def __init__(self, products):
        self.chunks = [{
            "doc_id": f"product:{pid}", "source_type": "product", "product_id": pid, "title": title,
            "category": "Electronics Earbuds", "price": 10.0, "inventory": "unknown",
            "text": f"{title} specification: Color {color}",
        } for pid, title, color in products]

    def search(self, query, top_k=5, source_type=None, category=None):
        words = set(query.casefold().replace("：", " ").replace("，", " ").split())
        scored = sorted(self.chunks, key=lambda c: -len(words & set(c["text"].casefold().split())))
        return [dict(c) for c in scored[:top_k]]


@pytest.fixture
def runner_parts(tmp_path):
    db = tmp_path / "rf.db"
    seed_database(db)
    retriever = FakeRetriever([
        ("P00002", "Acme Earbuds Blue", "Blue"),
        ("P00003", "Acme Earbuds Red", "Red"),
    ])
    return db, retriever


def test_oracle_and_top1_baseline_run_through_the_harness(runner_parts):
    db, retriever = runner_parts
    task = _task()
    _, oracle = HarnessRunner(db, retriever, OraclePolicy()).run(task)
    assert oracle.success is True

    trajectory, top1 = HarnessRunner(db, retriever, RetrievalTop1Policy()).run(task)
    assert [c.name for c in trajectory.tool_calls] == ["search_catalog"]
    assert top1.answer_diagnostics["search_calls"] == 1
    offered = {schema["name"] for schema in trajectory.observations[0]["tool_schemas"]}
    assert offered == set(RESEARCH_FIND_TOOLS)
    serialized = json.dumps(trajectory.observations, ensure_ascii=False)
    assert "evaluation_contract" not in serialized and "near_miss" not in serialized


def test_top1_baseline_cannot_abstain_on_no_answer_tasks(runner_parts):
    db, retriever = runner_parts
    _, result = HarnessRunner(db, retriever, RetrievalTop1Policy()).run(_task(answer=None, task_type="no_answer"))
    assert result.failure_type == "answered-unsatisfiable"


def _product(pid, leaf, brand, color, material="Plastic", price=None, title=None):
    attrs = {"Brand": brand, "Color": color, "Material": material}
    text = norm(" ".join([title or f"{brand} {leaf}", *attrs.values()]))
    return Product(pid, title or f"{brand} {leaf}", leaf, price, brand, attrs, text, text, norm(f"{leaf} {title or ''}"))


def _catalog():
    return [
        _product("P00001", "Earbuds", "Acme", "Blue"),
        _product("P00002", "Earbuds", "Acme", "Red"),
        _product("P00003", "Earbuds", "Zenta", "Blue"),
        _product("P00004", "Earbuds", "Zenta", "Green", material="Metal"),
        _product("P00005", "Kettles", "Acme", "Blue", material="Steel"),
    ]


def test_minimal_unique_requires_every_constraint():
    catalog = Catalog(_catalog())
    target = catalog.products[0]
    ok = [Constraint("type", "type", "Earbuds"), Constraint("brand", "Brand", "Acme"),
          Constraint("attribute", "Color", "Blue")]
    assert minimal_unique(catalog, target, ok)
    # Material is shared by every Acme earbud, so it adds nothing beyond brand.
    loose = ok[:2] + [Constraint("attribute", "Material", "Plastic")]
    assert not minimal_unique(catalog, target, loose)


def test_generator_locks_answers_and_passes_independent_validation():
    products = _catalog()
    catalog = Catalog(products)
    found = find_answerable(catalog, products[0], with_brand=True, rng=random.Random(1))
    assert found and [p.product_id for p in catalog.matches(found)] == ["P00001"]
    unsat = find_unsatisfiable(catalog, products[0], random.Random(1))
    assert unsat and catalog.matches(unsat) == [] and catalog.near_misses(unsat)

    tasks = generate(products, per_type_split=2, seed=3, excluded=set())
    assert tasks and validate_tasks(tasks, products) == []
    for task in tasks:
        assert task["scoring_version"] == SCORING_VERSION_RESEARCH_FIND_V1
        assert not set(task["allowed_tools"]) - set(RESEARCH_FIND_TOOLS)


def test_typo_changes_the_brand_text():
    rng = random.Random(5)
    for _ in range(20):
        value = typo("Jabra Elite", rng)
        assert value is None or norm(value) != norm("Jabra Elite")


CORPUS = Path("ecommerce_rag/data/amazon_products_5k.jsonl")
PATHS = Path("ecommerce_rag/data/amazon_products_5k.category_paths.jsonl")


@pytest.mark.skipif(not (CORPUS.exists() and PATHS.exists()), reason="local 5k corpus and category sidecar not built")
def test_committed_task_file_passes_validation_against_the_corpus():
    from scripts.generate_research_find_tasks import load_products

    tasks = [json.loads(line) for line in Path("ecommerce_rag/data/research_find_v1.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(tasks) == 200
    assert validate_tasks(tasks, load_products(CORPUS, PATHS)) == []
