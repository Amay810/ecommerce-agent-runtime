"""Run paired A/B/C return-closure trials with independent DB/session resets."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

from ecommerce_rag.harness import HarnessRunner, RulePolicy, TrajectoryStore, load_tasks, summarize
from ecommerce_rag.orders import seed_database
from ecommerce_rag.skill_loader import load_skill


def _policy(kind: str, skill_path: Path | None):
    if kind == "rule":
        return RulePolicy()
    from ecommerce_rag.native_tool_policy import NativeToolPolicy

    return NativeToolPolicy.from_env(skill_path=skill_path, skill_enabled=skill_path is not None)


def _run_arm(
    *,
    arm: str,
    tasks: list[Any],
    db: Path,
    store: TrajectoryStore,
    policy_kind: str,
    skill_path: Path | None,
    index: Any | None,
    max_steps: int,
) -> dict[str, Any]:
    results = []
    details = []
    invalid_task_ids = []
    for task in tasks:
        # A fresh seed is used for every paired task/arm. The task's initial
        # state then applies on top of the same deterministic database.
        seed_database(db)
        runner = HarnessRunner(db, index, _policy(policy_kind, skill_path), max_steps=max_steps)
        trajectory, result = runner.run(task)
        store.save(trajectory, result)
        results.append(result)
        if any(
            span.get("parse_stage") == "generation_error"
            for model_call in trajectory.model_calls
            for span in (
                model_call.get("llm", {}).get("attempts", [])
                if isinstance(model_call.get("llm"), dict) else []
            )
        ):
            invalid_task_ids.append(task.task_id)
        details.append({
            "task_id": task.task_id,
            "seed": task.seed,
            "trajectory_id": trajectory.trajectory_id,
            **result.to_dict(),
            "tool_calls_total": len(trajectory.tool_calls),
            "changed_write_count": sum(
                1 for call in trajectory.tool_calls
                if call.name == "create_return_request" and call.result.get("changed")
            ),
            "guardrail_block_count": len(trajectory.guardrail_spans),
            "confirmation_events": len(trajectory.confirmation_spans),
            "tool_error_count": sum(1 for call in trajectory.tool_calls if call.error or not call.result.get("ok")),
            "answer_evidence_coverage": result.required_evidence_coverage,
            "answer_fact_pass": result.answer_fact_pass,
            "latency_ms": trajectory.elapsed_ms,
            "token_count": sum(
                (span.get("prompt_tokens") or 0) + (span.get("completion_tokens") or 0)
                for model_call in trajectory.model_calls
                for span in (model_call.get("llm", {}).get("attempts", []) if isinstance(model_call.get("llm"), dict) else [])
            ),
        })
    skill = load_skill(skill_path) if skill_path else None
    return {
        "arm": arm,
        "status": "executed",
        "policy": policy_kind,
        "execution_class": "real_model" if policy_kind == "native" else "deterministic_offline",
        "skill": skill.metadata() if skill else {"enabled": False},
        "summary": summarize(results, repeats=1),
        "details": details,
        "invalid_runs": invalid_task_ids,
    }


def _apply_scoring_version(tasks: list[Any], scoring_version: str) -> list[Any]:
    if scoring_version == "return-closure-v1":
        return tasks
    return [
        replace(
            task,
            scoring_version="return-closure-v2",
            metadata={
                **task.metadata,
                "return_required_tools": ["get_policy", "check_return_eligibility"],
                "return_write_expected": "create_return_request" in task.allowed_tools,
            },
        )
        for task in tasks
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, default=Path("ecommerce_rag/data/return_closure_tasks.jsonl"))
    parser.add_argument("--db", type=Path, default=Path("logs/return_closure.db"))
    parser.add_argument("--store", type=Path, default=Path("logs/return_closure_trajectories.sqlite"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--policy", choices=("native", "rule"), default="native")
    parser.add_argument("--skill-v0", type=Path, default=Path("skills/return_request/SKILL.md"))
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--arm", choices=("A", "B", "C", "all"), default="all")
    parser.add_argument("--split", choices=("smoke", "exploration", "validation", "locked"))
    parser.add_argument(
        "--scoring-version",
        choices=("return-closure-v1", "return-closure-v2"),
        default="return-closure-v2",
    )
    parser.add_argument("--max-steps", type=int, default=8)
    args = parser.parse_args()
    tasks = load_tasks(args.tasks)
    if args.split:
        tasks = [task for task in tasks if task.split == args.split]
    tasks = _apply_scoring_version(tasks, args.scoring_version)
    if not tasks:
        raise SystemExit("no tasks selected")
    # Without a real failure-derived candidate, an offline run may still
    # produce the paired A/B evidence. Arm C is intentionally omitted rather
    # than filled with a fabricated candidate.
    arms = ("A", "B", "C") if args.arm == "all" and args.candidate else (
        ("A", "B") if args.arm == "all" else (args.arm,)
    )
    if "C" in arms and not args.candidate:
        raise SystemExit("--candidate is required for arm C")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.store.parent.mkdir(parents=True, exist_ok=True)
    store = TrajectoryStore(args.store)
    index = None
    if os.getenv("ERAG_RETURN_CLOSURE_INDEX", ""):
        from ecommerce_rag.hybrid_retriever import HybridRetriever
        index = HybridRetriever(Path(os.environ["ERAG_RETURN_CLOSURE_INDEX"]))
    report = {
        "experiment": "return-closure-skill-trial-v2" if args.scoring_version == "return-closure-v2" else "return-closure-skill-trial-v1",
        "scoring_version": args.scoring_version,
        "status": "executed",
        "model_experiment": args.policy == "native",
        "tasks": str(args.tasks),
        "arms": {},
    }
    for arm in arms:
        skill_path = None if arm == "A" else args.skill_v0 if arm == "B" else args.candidate
        try:
            report["arms"][arm] = _run_arm(
                arm=arm, tasks=tasks, db=args.db, store=store,
                policy_kind=args.policy, skill_path=skill_path,
                index=index, max_steps=args.max_steps,
            )
        except RuntimeError as exc:
            if args.policy == "native" and any(
                marker in str(exc) for marker in ("API_KEY", "BASE_URL")
            ):
                report["arms"][arm] = {
                    "arm": arm,
                    "status": "not_executed",
                    "reason": "no_model_service_configured",
                    "error": str(exc),
                }
                continue
            raise
    statuses = [arm_report.get("status") for arm_report in report["arms"].values()]
    if statuses and all(status == "not_executed" for status in statuses):
        report["status"] = "not_executed"
    elif any(status == "not_executed" for status in statuses):
        report["status"] = "partially_executed"
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
