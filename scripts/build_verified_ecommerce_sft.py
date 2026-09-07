"""Build a strict, versioned M1 dataset from τ³ Retail train rollouts."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ecommerce_rag.verified_sft import (
    DatasetBuildConfig,
    build_verified_dataset,
    default_system_prompt,
    load_process_audits,
)


def load_tau3_contract(tau_root: Path) -> tuple[str, list[dict], set[str]]:
    sys.path.insert(0, str(tau_root / "src"))
    from tau2.environment.toolkit import get_tool_signatures
    from tau2.registry import registry

    environment = registry.get_env_constructor("retail")()
    signatures = get_tool_signatures(environment.tools)
    tools = [
        {"type": "function", "function": value.model_dump(exclude_none=True)}
        for value in signatures.values()
    ]
    split_path = tau_root / "data" / "tau2" / "domains" / "retail" / "split_tasks.json"
    splits = json.loads(split_path.read_text(encoding="utf-8"))
    return environment.get_policy(), tools, {str(value) for value in splits["train"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--process-audit", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--tau-root",
        type=Path,
        default=Path(os.environ["TAU_ROOT"]) if os.environ.get("TAU_ROOT") else None,
        help="Pinned Tau2 checkout; defaults to TAU_ROOT.",
    )
    parser.add_argument("--teacher-model", required=True)
    parser.add_argument(
        "--teacher-usage-rights",
        choices=("approved_open_weights", "approved_terms"),
        required=True,
    )
    parser.add_argument("--max-per-task", type=int, default=3)
    parser.add_argument("--max-per-structure", type=int, default=8)
    parser.add_argument("--reserved-task-ids", nargs="*", default=())
    parser.add_argument(
        "--allow-unaudited-pilot",
        action="store_true",
        help="Development-only: emit reward-verified candidates before process audit.",
    )
    args = parser.parse_args()
    if args.tau_root is None:
        parser.error("--tau-root or TAU_ROOT is required")

    config = DatasetBuildConfig(
        source_split="train",
        teacher_model=args.teacher_model,
        teacher_usage_rights=args.teacher_usage_rights,
        max_per_task=args.max_per_task,
        max_per_structure=args.max_per_structure,
        require_process_audit=not args.allow_unaudited_pilot,
        reserved_task_ids=frozenset(str(value) for value in args.reserved_task_ids),
    )
    policy, tools, train_ids = load_tau3_contract(args.tau_root)
    payload = json.loads(args.results.read_text(encoding="utf-8"))
    manifest = build_verified_dataset(
        payload=payload,
        source_results=args.results,
        output_dir=args.output_dir,
        system_prompt=default_system_prompt(policy, config),
        tools=tools,
        allowed_task_ids=train_ids,
        process_audits=load_process_audits(args.process_audit),
        config=config,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
