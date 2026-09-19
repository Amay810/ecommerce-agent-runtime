"""Propose one bounded Skill-only patch from an exploration failure."""

from __future__ import annotations

import argparse
import difflib
import json
from pathlib import Path

from ecommerce_rag.harness import TrajectoryStore
from ecommerce_rag.skill_loader import load_skill


def _candidate_content(base: str, failure_type: str | None) -> str:
    marker = "version: v0"
    if marker not in base:
        raise ValueError("base skill must use version: v0")
    content = base.replace(marker, "version: v1", 1).rstrip()
    addition = (
        "\n- Treat confirmation as valid only when it is the immediate user response to the current, "
        "fully specified request; never infer authorization from an earlier message or from a bare "
        "affirmation without a pending request."
    )
    if failure_type:
        addition += f"\n\n<!-- Candidate generated from failure category: {failure_type}; no business facts were added. -->"
    return content + addition + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exploration-report", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--base-skill", type=Path, default=Path("skills/return_request/SKILL.md"))
    parser.add_argument("--output-skill", type=Path, default=Path("logs/return_request_candidate_v1/SKILL.md"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = json.loads(args.exploration_report.read_text(encoding="utf-8"))
    arm = (report.get("arms") or {}).get("B") or {}
    details = arm.get("details") or []
    if arm.get("status") != "executed":
        result = {
            "experiment": "return-closure-skill-candidate-v1",
            "status": "not_evaluated",
            "reason": "exploration arm B was not executed",
            "base_skill": load_skill(args.base_skill).metadata(),
            "scope": "only return_request Skill content; no model, tool, policy, task, scorer, or budget changes",
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    failures = [detail for detail in details if not detail.get("success")]
    result = {
        "experiment": "return-closure-skill-candidate-v1",
        "status": "no_candidate" if not failures else "candidate_proposed",
        "base_skill": load_skill(args.base_skill).metadata(),
        "scope": "only return_request Skill content; no model, tool, policy, task, scorer, or budget changes",
    }
    if not failures:
        result["reason"] = "exploration arm B has no real task failure"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    first = failures[0]
    trajectory, grade = TrajectoryStore(args.store).load(first["trajectory_id"])
    failure_type = first.get("failure_type")
    base_raw = args.base_skill.read_text(encoding="utf-8")
    candidate_raw = _candidate_content(base_raw, failure_type)
    args.output_skill.parent.mkdir(parents=True, exist_ok=True)
    args.output_skill.write_text(candidate_raw, encoding="utf-8")
    candidate = load_skill(args.output_skill)
    result.update({
        "failure_evidence": {
            "task_id": first.get("task_id"),
            "trajectory_id": first.get("trajectory_id"),
            "failure_type": failure_type,
            "grade": grade,
            "trajectory_steps": len(trajectory.get("actions") or []),
        },
        "candidate_skill": candidate.metadata(),
        "skill_diff": "".join(difflib.unified_diff(
            base_raw.splitlines(keepends=True), candidate_raw.splitlines(keepends=True),
            fromfile=str(args.base_skill), tofile=str(args.output_skill),
        )),
        "hypothesis": "binding the confirmation request to the immediately preceding user response may reduce stale or unbound write proposals",
        "potential_regression": "extra clarification can increase turns and tool-call cost",
    })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
