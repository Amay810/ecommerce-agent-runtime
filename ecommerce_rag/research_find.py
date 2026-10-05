"""Answer-level scoring and baselines for research-find product search tasks.

``research-find-v1`` tasks ask the Agent to identify one catalog product from
natural constraints, or to state that no product explicitly satisfies them.
The answer key lives only in ``TaskSpec.evaluation_contract``; it is never part
of an ``AgentObservation``. Unlike the historical operational scorer, success
requires the final answer itself to name the correct product.
"""

from __future__ import annotations

import re
from typing import Any

from .domain import AgentAction, AgentObservation, TaskSpec, Trajectory


RESEARCH_FIND_TOOLS = ("search_catalog", "get_product", "compare_products")
PRODUCT_ID_PATTERN = re.compile(r"(?<![A-Za-z0-9])P\d{5}(?!\d)", re.I)
ABSTENTION_MARKERS = (
    "没有", "无符合", "不存在", "找不到", "未找到", "没找到", "无法找到", "未能找到",
    "no matching", "no product", "none of", "not found",
)
STEP_LIMIT_TERMINATIONS = {"max_steps", "budget_exhausted"}


def answer_product_ids(text: str) -> list[str]:
    """Return distinct product IDs in first-mention order."""
    seen: list[str] = []
    for match in PRODUCT_ID_PATTERN.findall(text or ""):
        value = match.upper()
        if value not in seen:
            seen.append(value)
    return seen


def retrieved_product_ids(trajectory: Trajectory) -> dict[str, int | None]:
    """Map every product surfaced by a successful read tool to its best search rank.

    ``None`` means the product was returned by ``get_product`` or
    ``compare_products`` but never ranked by ``search_catalog``.
    """
    seen: dict[str, int | None] = {}

    def note(product_id: Any, rank: int | None) -> None:
        if not product_id:
            return
        key = str(product_id).upper()
        previous = seen.get(key)
        if key not in seen or (rank is not None and (previous is None or rank < previous)):
            seen[key] = rank

    for call in trajectory.tool_calls:
        result = call.result or {}
        if not result.get("ok"):
            continue
        if call.name == "search_catalog":
            for rank, item in enumerate(result.get("items") or [], 1):
                if isinstance(item, dict):
                    note(item.get("product_id"), rank)
        elif call.name == "get_product":
            note((result.get("product") or {}).get("product_id"), None)
        elif call.name == "compare_products":
            for wrapped in result.get("products") or []:
                if isinstance(wrapped, dict):
                    note((wrapped.get("product") or {}).get("product_id"), None)
    return seen


def grade_research_find(task: TaskSpec, trajectory: Trajectory) -> dict[str, Any]:
    """Score the final answer and attribute a failure without an LLM judge."""
    contract = task.evaluation_contract or {}
    expected = contract.get("answer_product_id")
    expected = str(expected).upper() if expected else None
    answered = answer_product_ids(trajectory.final_answer)
    retrieved = retrieved_product_ids(trajectory)
    near_misses = {str(x).upper() for x in contract.get("near_miss_product_ids") or []}
    has_marker = any(marker in (trajectory.final_answer or "").casefold() for marker in ABSTENTION_MARKERS)
    abstained = not answered and has_marker
    calls = [call.name for call in trajectory.tool_calls]
    diagnostics = {
        "task_type": contract.get("task_type"),
        "expected_product_id": expected,
        "answered_product_ids": answered,
        "abstained": abstained,
        "gold_retrieved": bool(expected and expected in retrieved),
        "gold_best_search_rank": retrieved.get(expected) if expected else None,
        "answered_near_miss": bool(set(answered) & near_misses),
        # Diagnostic only: an abstention phrase next to a product ID still fails
        # the public "no product ID" rule, but is not the same as recommending it.
        "abstention_phrase_with_product_id": bool(answered and has_marker),
        "search_calls": calls.count("search_catalog"),
        "get_product_calls": calls.count("get_product"),
        "compare_calls": calls.count("compare_products"),
        "termination_reason": trajectory.termination_reason,
    }

    if trajectory.termination_reason in STEP_LIMIT_TERMINATIONS:
        failure = "step-limit"
    elif trajectory.termination_reason != "final_answer":
        failure = "no-final-answer"
    elif expected:
        if answered == [expected]:
            failure = None
        elif len(answered) > 1:
            failure = "multiple-products-answered"
        elif not answered:
            failure = "false-abstention"
        elif expected in retrieved:
            failure = "retrieved-not-selected"
        else:
            failure = "never-retrieved"
    elif answered:
        failure = "answered-unsatisfiable"
    elif not abstained:
        failure = "no-clear-abstention"
    else:
        failure = None
    return {
        "success": failure is None,
        "failure_type": failure,
        "abstention_expected": expected is None,
        "abstention_observed": abstained,
        "diagnostics": diagnostics,
    }


class RetrievalTop1Policy:
    """Single-shot RAG baseline: one search with the raw request, answer top-1.

    It never verifies constraints and can only abstain when search returns no
    item, which makes it the non-agentic reference for the same tool surface.
    """

    privileged = False

    def act(self, observation: AgentObservation) -> AgentAction:
        searches = [
            row for row in observation.history
            if row.get("role") == "tool" and row.get("name") == "search_catalog"
        ]
        if not searches:
            request = next(
                (str(row.get("content", "")) for row in observation.history if row.get("role") == "user"),
                observation.current_message,
            )
            return AgentAction.tool_call("search_catalog", query=request, top_k=5)
        items = (searches[-1].get("result") or {}).get("items") or []
        if not items:
            return AgentAction.answer("商品库中没有找到符合条件的商品。")
        return AgentAction.answer(f"{items[0]['product_id']}")
