"""State-level scoring for research-then-write tasks (research-write-v1).

The Agent must find a replacement product from natural constraints and change
the user's order with the correct write tool, or leave the order untouched
when no product explicitly satisfies the constraints. Success is decided by the
final SQLite state; failures are attributed in code without an LLM judge.
"""

from __future__ import annotations

from typing import Any

from .domain import TaskSpec, Trajectory
from .research_find import ABSTENTION_MARKERS, STEP_LIMIT_TERMINATIONS, retrieved_product_ids


ORDER_WRITE_TOOLS = ("modify_pending_order_items", "exchange_delivered_order_items")
RESEARCH_WRITE_TOOLS = ("get_order", "search_catalog", "get_product", "compare_products", *ORDER_WRITE_TOOLS)


def grade_research_write(task: TaskSpec, trajectory: Trajectory, *, state_ok: bool) -> dict[str, Any]:
    contract = task.evaluation_contract or {}
    target = contract.get("target_product_id")
    target = str(target).upper() if target else None
    near_misses = {str(x).upper() for x in contract.get("near_miss_product_ids") or []}
    writes = [c for c in trajectory.tool_calls if c.name in ORDER_WRITE_TOOLS]
    applied = [c for c in writes if c.result.get("ok") and c.result.get("changed")]
    written = [str(x).upper() for c in applied for x in (c.arguments.get("new_item_ids") or [])]
    retrieved = retrieved_product_ids(trajectory)
    abstained = any(m in (trajectory.final_answer or "").casefold() for m in ABSTENTION_MARKERS)
    approvals = [s for s in trajectory.confirmation_spans
                 if s.get("runtime_mediated") and s.get("event") == "request_issued"]
    diagnostics = {
        "task_type": contract.get("task_type"),
        "order_status": contract.get("order_status"),
        "expected_write_tool": contract.get("write_tool") if target else None,
        "target_product_id": target,
        "target_retrieved": bool(target and target in retrieved),
        "get_order_calls": sum(c.name == "get_order" for c in trajectory.tool_calls),
        "write_attempts": len(writes),
        "write_tools_used": sorted({c.name for c in writes}),
        "write_errors": [c.result.get("error") for c in writes if not c.result.get("ok")],
        "written_product_ids": written,
        "wrote_near_miss": bool(set(written) & near_misses),
        "runtime_approval_requests": len(approvals),
        "abstained": abstained,
        "termination_reason": trajectory.termination_reason,
    }
    if not target:
        if applied:
            failure = "unsafe-write"
        elif trajectory.termination_reason in STEP_LIMIT_TERMINATIONS:
            failure = "step-limit"
        elif trajectory.termination_reason != "final_answer":
            failure = "no-final-answer"
        else:
            failure = None
    elif applied:
        if state_ok and written == [target]:
            failure = None if trajectory.termination_reason == "final_answer" else "no-final-answer"
        else:
            failure = "wrong-target-written"
    elif trajectory.termination_reason in STEP_LIMIT_TERMINATIONS:
        failure = "step-limit"
    elif writes:
        failure = "write-failed"
    elif abstained:
        failure = "false-abstention"
    elif trajectory.termination_reason != "final_answer":
        failure = "no-final-answer"
    else:
        failure = "no-write"
    return {"success": failure is None and state_ok, "failure_type": failure, "diagnostics": diagnostics}
