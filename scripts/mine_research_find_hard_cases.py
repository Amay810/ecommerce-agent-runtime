"""Select model-in-the-loop hard cases from a research-find candidate pool.

Every candidate the current system failed is kept; a seeded sample of passed
candidates is kept for calibration. The output is an exploration set for
diagnosis and development only: its success rate is selection-biased by
construction and must never be reported as an evaluation result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import Counter
from pathlib import Path


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def mine(candidates: list[dict], report: dict, *, calibration_rate: float, seed: int, report_sha256: str) -> list[dict]:
    outcomes = {row["task_id"]: row for row in report["details"]}
    missing = [t["task_id"] for t in candidates if t["task_id"] not in outcomes]
    if missing:
        raise SystemExit(f"report lacks {len(missing)} candidates, e.g. {missing[:3]}")
    rng = random.Random(seed)
    selected = []
    for task in candidates:
        row = outcomes[task["task_id"]]
        if row["success"]:
            if rng.random() >= calibration_rate:
                continue
            outcome = "passed_calibration"
        else:
            outcome = "failed"
        contract = {**task["evaluation_contract"], "generation_mode": "model_mined",
                    "mining": {"outcome": outcome, "failure_type": row.get("failure_type"),
                               "report_sha256": report_sha256}}
        selected.append({**task, "evaluation_contract": contract})
    return selected


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True, help="harness report of the current system on the candidates")
    parser.add_argument("--calibration-rate", type=float, default=0.25)
    parser.add_argument("--seed", type=int, default=20261007)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    candidates = [json.loads(line) for line in args.candidates.read_text(encoding="utf-8").splitlines() if line.strip()]
    report = json.loads(args.report.read_text(encoding="utf-8"))
    report_sha = sha256_file(args.report)
    selected = mine(candidates, report, calibration_rate=args.calibration_rate, seed=args.seed, report_sha256=report_sha)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in selected), encoding="utf-8")
    outcomes = {row["task_id"]: row for row in report["details"]}
    manifest = {
        "mode": "model_mined",
        "candidates": {"path": str(args.candidates), "sha256": sha256_file(args.candidates), "tasks": len(candidates)},
        "report": {"path": str(args.report), "sha256": report_sha, "configuration": report.get("configuration"),
                   "policy": report.get("policy")},
        "candidate_failures": sum(not outcomes[t["task_id"]]["success"] for t in candidates),
        "candidate_failure_types": dict(Counter(outcomes[t["task_id"]]["failure_type"]
                                                for t in candidates if not outcomes[t["task_id"]]["success"])),
        "calibration_rate": args.calibration_rate,
        "seed": args.seed,
        "selected": len(selected),
        "selected_by_outcome": dict(Counter(t["evaluation_contract"]["mining"]["outcome"] for t in selected)),
        "selected_by_type": dict(Counter(t["evaluation_contract"]["task_type"] for t in selected)),
        "output_sha256": sha256_file(args.output),
        "note": "selection-biased exploration set; never report its success rate as evaluation",
    }
    args.output.with_suffix(".manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
