"""Validate canonical Tau2 arms and emit a deterministic paired-trial audit."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


EXPECTED_REWARDS = {"base": 77, "step9": 82, "grpo37": 88}
REQUIRED_FIELDS = {
    "task_id",
    "trial",
    "seed",
    "messages",
    "reward_info",
    "termination_reason",
}


def _records(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("simulations"), list):
        raise ValueError(f"{path} is not a Tau2 simulations payload")
    rows = [row for row in payload["simulations"] if isinstance(row, dict)]
    if len(rows) != 160:
        raise ValueError(f"{path}: expected 160 records, got {len(rows)}")
    for index, row in enumerate(rows):
        missing = REQUIRED_FIELDS - set(row)
        if missing:
            raise ValueError(f"{path}: record {index} missing {sorted(missing)}")
        if not isinstance(row["messages"], list) or not row["messages"]:
            raise ValueError(f"{path}: record {index} has no messages")
        if not isinstance(row["reward_info"], dict):
            raise ValueError(f"{path}: record {index} has no reward_info object")
    return rows


def _key(row: dict[str, Any]) -> tuple[str, Any]:
    return (str(row["task_id"]), row["trial"])


def _load_causes(path: Path) -> dict[tuple[str, Any], dict[str, Any]]:
    if not path.exists():
        return {}
    causes: dict[tuple[str, Any], dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        cause = json.loads(line)
        key = (str(cause["task_id"]), cause["trial"])
        if key in causes:
            raise ValueError(f"duplicate first-causal key: {key}")
        causes[key] = cause
    return causes


def _outcome(row: dict[str, Any]) -> str:
    return "pass" if row["reward_info"].get("reward") == 1 else "fail"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    rows = {
        arm: _records(args.input_dir / filename)
        for arm, filename in {
            "base": "base_results.json",
            "step9": "step9_results.json",
            "grpo37": "step37_results.json",
        }.items()
    }
    arms = {arm: {_key(row): row for row in values} for arm, values in rows.items()}
    for arm, mapping in arms.items():
        if len(mapping) != 160:
            raise ValueError(f"{arm}: duplicate (task_id, trial) keys")
        if sum(_outcome(row) == "pass" for row in mapping.values()) != EXPECTED_REWARDS[arm]:
            raise ValueError(f"{arm}: reward aggregate differs from frozen expectation")

    base_keys = set(arms["base"])
    for arm in ("step9", "grpo37"):
        keys = set(arms[arm])
        if keys != base_keys:
            raise ValueError(f"base/{arm}: pairing-key intersection is not exactly 160")
        if any(arms["base"][key]["seed"] != arms[arm][key]["seed"] for key in base_keys):
            raise ValueError(f"base/{arm}: trial seed mismatch")

    causes = {
        "base": _load_causes(args.output_dir / "base_first_causal.jsonl"),
        "grpo37": _load_causes(args.output_dir / "grpo37_first_causal.jsonl"),
    }
    paired: list[dict[str, Any]] = []
    migration: dict[str, Counter[str]] = {
        "base_to_step9": Counter(),
        "base_to_grpo37": Counter(),
    }
    for key in sorted(base_keys):
        base = arms["base"][key]
        step9 = arms["step9"][key]
        grpo37 = arms["grpo37"][key]
        base_outcome = _outcome(base)
        step9_outcome = _outcome(step9)
        grpo37_outcome = _outcome(grpo37)
        migration["base_to_step9"][f"{base_outcome}_to_{step9_outcome}"] += 1
        migration["base_to_grpo37"][f"{base_outcome}_to_{grpo37_outcome}"] += 1
        paired.append(
            {
                "task_id": key[0],
                "trial": key[1],
                "seed": base["seed"],
                "base_reward": base["reward_info"].get("reward"),
                "step9_reward": step9["reward_info"].get("reward"),
                "grpo37_reward": grpo37["reward_info"].get("reward"),
                "base_to_step9": f"{base_outcome}_to_{step9_outcome}",
                "base_to_grpo37": f"{base_outcome}_to_{grpo37_outcome}",
                "base_first_causal": causes["base"].get(key),
                "grpo37_first_causal": causes["grpo37"].get(key),
            }
        )

    delta = migration["base_to_grpo37"]["fail_to_pass"] - migration["base_to_grpo37"]["pass_to_fail"]
    if delta != 11:
        raise ValueError(f"Base→GRPO37 delta check failed: {delta}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    with (args.output_dir / "paired_trials.jsonl").open("w", encoding="utf-8") as handle:
        for row in paired:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    summary = {
        "records": {arm: len(mapping) for arm, mapping in arms.items()},
        "reward1": {
            arm: sum(_outcome(row) == "pass" for row in mapping.values())
            for arm, mapping in arms.items()
        },
        "key_intersection": {"base_step9": 160, "base_grpo37": 160},
        "seed_agreement": {"base_step9": 160, "base_grpo37": 160},
        "migration": {name: dict(counts) for name, counts in migration.items()},
        "grpo37_delta_check": delta,
    }
    (args.output_dir / "paired_migration_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
