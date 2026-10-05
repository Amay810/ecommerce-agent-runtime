"""Summarize research-find-v1 harness reports with task-level bootstrap CIs.

One report gives success by split and task type plus the failure attribution.
Two reports on the same tasks also give a paired difference (second - first)
with discordant counts; resampling is over task IDs, never trajectories.
"""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def load_rows(path: Path) -> list[dict[str, Any]]:
    report = json.loads(path.read_text(encoding="utf-8"))
    rows = [row for row in report["details"] if row.get("scoring_version") == "research-find-v1"]
    if not rows:
        raise SystemExit(f"{path}: no research-find-v1 rows")
    return rows


def bootstrap_ci(values: list[float], *, samples: int, seed: int) -> list[float] | None:
    if len(values) < 2:
        return None
    rng = random.Random(seed)
    means = sorted(sum(rng.choice(values) for _ in values) / len(values) for _ in range(samples))
    return [round(means[int(0.025 * samples)], 4), round(means[int(0.975 * samples) - 1], 4)]


def per_task(rows: list[dict[str, Any]]) -> dict[str, float]:
    grouped: dict[str, list[bool]] = defaultdict(list)
    for row in rows:
        grouped[row["task_id"]].append(bool(row["success"]))
    return {task_id: sum(v) / len(v) for task_id, v in grouped.items()}


def summarize(rows: list[dict[str, Any]], *, samples: int, seed: int) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        task_type = (row.get("answer_diagnostics") or {}).get("task_type") or "unknown"
        groups["all"].append(row)
        groups[f"split={row.get('split')}"].append(row)
        groups[f"type={task_type}"].append(row)
        groups[f"split={row.get('split')}/type={task_type}"].append(row)
    result = {}
    for name in sorted(groups):
        members = groups[name]
        scores = list(per_task(members).values())
        answerable = [r for r in members if (r.get("answer_diagnostics") or {}).get("expected_product_id")]
        result[name] = {
            "tasks": len(scores),
            "trajectories": len(members),
            "success": round(sum(scores) / len(scores), 4),
            "success_ci95": bootstrap_ci(scores, samples=samples, seed=seed),
            "failure_types": dict(Counter(r["failure_type"] for r in members if r["failure_type"]).most_common()),
            "gold_retrieved_rate": (round(sum(bool(r["answer_diagnostics"].get("gold_retrieved")) for r in answerable)
                                          / len(answerable), 4) if answerable else None),
            "avg_search_calls": round(sum(r["answer_diagnostics"].get("search_calls", 0) for r in members) / len(members), 3),
            "avg_get_product_calls": round(sum(r["answer_diagnostics"].get("get_product_calls", 0) for r in members) / len(members), 3),
        }
    return result


def paired(first: list[dict[str, Any]], second: list[dict[str, Any]], *, samples: int, seed: int) -> dict[str, Any]:
    a, b = per_task(first), per_task(second)
    common = sorted(set(a) & set(b))
    if not common:
        raise SystemExit("reports share no task IDs")
    deltas = [b[t] - a[t] for t in common]
    return {
        "tasks": len(common),
        "first_success": round(sum(a[t] for t in common) / len(common), 4),
        "second_success": round(sum(b[t] for t in common) / len(common), 4),
        "delta": round(sum(deltas) / len(deltas), 4),
        "delta_ci95": bootstrap_ci(deltas, samples=samples, seed=seed),
        "second_better_tasks": sum(d > 0 for d in deltas),
        "first_better_tasks": sum(d < 0 for d in deltas),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path, help="one report, or two reports to pair (first, second)")
    parser.add_argument("--split", help="restrict to one split, e.g. exploration")
    parser.add_argument("--bootstrap", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=20261005)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if len(args.reports) > 2:
        raise SystemExit("pass one report or two reports")

    loaded = []
    for path in args.reports:
        rows = load_rows(path)
        if args.split:
            rows = [row for row in rows if row.get("split") == args.split]
        loaded.append(rows)
    summary: dict[str, Any] = {
        "reports": [str(p) for p in args.reports],
        "split_filter": args.split,
        "summaries": [summarize(rows, samples=args.bootstrap, seed=args.seed) for rows in loaded],
    }
    if len(loaded) == 2:
        summary["paired"] = {"all": paired(*loaded, samples=args.bootstrap, seed=args.seed)}
    text = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
