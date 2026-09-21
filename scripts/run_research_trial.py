"""Run paired retrieval-context arms for complex local-data consultations.

Arm A keeps the current policy-visible NativeToolPolicy behavior. Arm B uses
the same tools, tasks, model and resource budget but adds the derived research
state to later decisions. ``--policy rule`` is a deterministic CPU wiring
substitute; it is not model evidence.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ecommerce_rag.harness import HarnessRunner, RulePolicy, TrajectoryStore, load_tasks, summarize
from ecommerce_rag.orders import seed_database
from ecommerce_rag.research_policy import EvidenceStateRulePolicy


def _make_policy(kind: str, arm: str, skill: Path | None):
    if kind == "rule":
        return RulePolicy() if arm == "A" else EvidenceStateRulePolicy()
    from ecommerce_rag.native_tool_policy import NativeToolPolicy

    return NativeToolPolicy.from_env(
        skill_path=skill,
        skill_enabled=skill is not None,
        research_context=arm == "B",
    )


def _token_count(trajectory: Any) -> int:
    return sum(
        (span.get("prompt_tokens") or 0) + (span.get("completion_tokens") or 0)
        for call in trajectory.model_calls
        for span in ((call.get("llm") or {}).get("attempts") or [])
    )


def _comparison_contract(policy_kind: str) -> dict[str, Any]:
    common = [
        "task file and task seeds",
        "DB/session reset per task",
        "retriever instance and tool schemas",
        "research budget",
        "HarnessRunner read-only budget guard",
        "max_steps and terminal stop rules",
        "Skill path and decoding configuration",
    ]
    if policy_kind == "native":
        return {
            "valid_for_native_attribution": True,
            "common_resources_and_rules": common,
            "changed_between_arms": [
                "NativeToolPolicy.research_context: false (A) vs true (B)",
                "B receives the derived ResearchState context block; A does not",
            ],
            "raw_evidence_ledger_separately_exposed": False,
        }
    return {
        "valid_for_native_attribution": False,
        "common_resources_and_rules": common,
        "changed_between_arms": [
            "deterministic policy implementation: RulePolicy (A) vs EvidenceStateRulePolicy (B)",
            "B follows derived research gaps; A retains the historical RulePolicy behavior",
        ],
        "note": "CPU rule arms verify wiring and stop behavior, not a causal Native A/B effect.",
    }


def _run_arm(
    *,
    arm: str,
    tasks: list[Any],
    db: Path,
    store: TrajectoryStore,
    policy_kind: str,
    skill: Path | None,
    index: Any | None,
    max_steps: int,
    budget_override: int | None,
) -> dict[str, Any]:
    results = []
    details = []
    for task in tasks:
        seed_database(db)
        budget = budget_override if budget_override is not None else task.research_budget
        policy = _make_policy(policy_kind, arm, skill)
        runner = HarnessRunner(
            db,
            index,
            policy,
            max_steps=max_steps,
            research_enabled=True,
            research_budget=budget,
        )
        trajectory, result = runner.run(task)
        store.save(trajectory, result)
        results.append(result)
        details.append({
            "task_id": task.task_id,
            "seed": task.seed,
            "trajectory_id": trajectory.trajectory_id,
            **result.to_dict(),
            "tool_calls_total": len(trajectory.tool_calls),
            "retrieval_calls": sum(call.name in {"search_catalog", "get_product", "compare_products", "get_policy", "get_order", "check_return_eligibility"} for call in trajectory.tool_calls),
            "budget_exhausted": any(span.get("outcome", {}).get("kind") == "budget_exhausted" for span in trajectory.research_spans),
            "necessary_fact_coverage": result.required_evidence_coverage,
            "answer_evidence_supported": result.citation_binding_pass,
            "manual_answer_support": "pending",
            "latency_ms": trajectory.elapsed_ms,
            "token_count": _token_count(trajectory),
            "research_span_count": len(trajectory.research_spans),
            "diagnostic_trace": {
                "model_calls": trajectory.model_calls,
                "tool_results": [asdict(call) for call in trajectory.tool_calls],
                "research_spans": trajectory.research_spans,
            },
        })
    return {
        "arm": arm,
        "status": "executed",
        "policy": policy_kind,
        "execution_class": "real_model" if policy_kind == "native" else "deterministic_offline",
        "research_context": arm == "B",
        "summary": summarize(results, repeats=1),
        "details": details,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, default=Path("ecommerce_rag/data/complex_research_exploration.jsonl"))
    parser.add_argument("--db", type=Path, default=Path("logs/complex_research.db"))
    parser.add_argument("--store", type=Path, default=Path("logs/complex_research_trajectories.sqlite"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--policy", choices=("native", "rule"), default="native")
    parser.add_argument("--arm", choices=("A", "B", "all"), default="all")
    parser.add_argument("--task-id", action="append", dest="task_ids", help="Select one task id; repeat for a small diagnostic run")
    parser.add_argument("--split", choices=("exploration", "validation"))
    parser.add_argument("--index", type=Path)
    parser.add_argument("--fixture", action="store_true", help="Use checked-in lexical data as a CPU-only retriever substitute")
    parser.add_argument("--skill", type=Path)
    parser.add_argument("--research-budget", type=int)
    parser.add_argument("--max-steps", type=int, default=8)
    args = parser.parse_args()

    tasks = load_tasks(args.tasks)
    if args.task_ids:
        wanted = set(args.task_ids)
        tasks = [task for task in tasks if task.task_id in wanted]
        missing = sorted(wanted - {task.task_id for task in tasks})
        if missing:
            raise SystemExit(f"unknown task id(s): {', '.join(missing)}")
    if args.split:
        tasks = [task for task in tasks if task.split == args.split]
    if not tasks:
        raise SystemExit("no tasks selected")

    if args.index and args.fixture:
        raise SystemExit("--index and --fixture are mutually exclusive")
    index = None
    if args.index:
        from ecommerce_rag.hybrid_retriever import HybridRetriever

        index = HybridRetriever(args.index)
    elif args.fixture:
        from ecommerce_rag.research_fixture import LocalResearchFixtureRetriever

        index = LocalResearchFixtureRetriever()
    arms = ("A", "B") if args.arm == "all" else (args.arm,)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.store.parent.mkdir(parents=True, exist_ok=True)
    store = TrajectoryStore(args.store)
    report: dict[str, Any] = {
        "experiment": "complex-research-context-ab-v1",
        "status": "executed",
        "tasks": str(args.tasks),
        "split": args.split or "all",
        "policy": args.policy,
        "fixed_resources": {
            "task_file": str(args.tasks),
            "retrieval_index": str(args.index) if args.index else None,
            "retrieval_fixture": bool(args.fixture),
            "research_budget": args.research_budget,
            "max_steps": args.max_steps,
            "skill": str(args.skill) if args.skill else None,
            "task_ids": [task.task_id for task in tasks],
        },
        "comparison_contract": _comparison_contract(args.policy),
        "diagnostic_storage": {
            "trajectory_store": str(args.store),
            "report_details_field": "diagnostic_trace",
            "contents": ["full request messages and tools", "raw provider response", "tool arguments and results", "research spans"],
        },
        "answer_support_note": "citation_binding_pass is automatic evidence binding; manual_answer_support remains pending because citation presence alone is insufficient.",
        "arms": {},
    }
    for arm in arms:
        try:
            report["arms"][arm] = _run_arm(
                arm=arm,
                tasks=tasks,
                db=args.db,
                store=store,
                policy_kind=args.policy,
                skill=args.skill,
                index=index,
                max_steps=args.max_steps,
                budget_override=args.research_budget,
            )
        except RuntimeError as exc:
            if args.policy == "native" and any(marker in str(exc) for marker in ("API_KEY", "BASE_URL")):
                report["arms"][arm] = {
                    "arm": arm,
                    "status": "not_executed",
                    "reason": "no_model_service_configured",
                    "error": str(exc),
                }
                continue
            raise
    statuses = [arm.get("status") for arm in report["arms"].values()]
    if statuses and all(status == "not_executed" for status in statuses):
        report["status"] = "not_executed"
    elif any(status == "not_executed" for status in statuses):
        report["status"] = "partially_executed"
    report["arm_contract"] = {
        "A": "current policy-visible retrieval behavior; no structured research state in the provider prompt",
        "B": "same policy/tools/tasks/budget with derived constraints, facts+sources, gaps, query history, and remaining budget",
    }
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
