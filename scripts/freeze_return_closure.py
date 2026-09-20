"""Write the small, explicit P0 freeze manifest for return-closure trials."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from ecommerce_rag import config


def sha256_file(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], text=True).strip()
    except Exception:
        return "unavailable"


def build_manifest(
    tasks: Path,
    index: Path | None,
    scoring_version: str = "return-closure-v1",
    test_result: str | None = None,
) -> dict[str, Any]:
    today = os.getenv("ERAG_SIMULATED_TODAY", "2026-07-20")
    index_hash = None
    if index and index.exists():
        digest = hashlib.sha256()
        for path in sorted(p for p in index.rglob("*") if p.is_file()):
            digest.update(str(path.relative_to(index)).encode("utf-8"))
            digest.update(path.read_bytes())
        index_hash = digest.hexdigest()
    if scoring_version not in {"return-closure-v1", "return-closure-v2"}:
        raise ValueError(f"unsupported scoring version: {scoring_version}")
    manifest_version = "return-closure-freeze-v2" if scoring_version == "return-closure-v2" else "return-closure-freeze-v1"
    manifest: dict[str, Any] = {
        "manifest_version": manifest_version,
        "scoring_version": scoring_version,
        "runtime_commit": git("rev-parse", "HEAD"),
        "branch": git("branch", "--show-current"),
        "worktree_dirty": bool(git("status", "--porcelain")),
        "simulated_today": today,
        "date_semantics": "days_since_delivery = (simulated_today - delivered_at).days; the existing tool also preserves the policy's quality_issue exception; this first-version trial filters all tasks to quality_issue=false, opened=false, and seven-day logic",
        "policy_path": str(config.POLICY_DATA_PATH),
        "policy_sha256": sha256_file(config.POLICY_DATA_PATH),
        "retrieval_index_path": str(index) if index else None,
        "retrieval_index_sha256": index_hash,
        "tasks_path": str(tasks),
        "tasks_sha256": sha256_file(tasks),
        "seed": {"users": 1000, "orders": 10000, "seed": 20260720},
        "runtime": {
            "runtime_version": "system-v1",
            "prompt_version": "ecommerce-native-v1",
            "compact_context": os.getenv("ERAG_CONTEXT_COMPACTION", "1"),
            "max_generation_retries": 1,
        },
        "model": {
            "backend": "openai-compatible-native-tools",
            "wire_protocol": "OpenAI-compatible; this does not select an OpenAI-hosted model",
            "model": os.getenv("ARAG_LLM_MODEL", os.getenv("ERAG_LLM_MODEL", "Qwen/Qwen3-4B-Instruct-2507")),
            "base_url": os.getenv("ARAG_LLM_BASE_URL", os.getenv("ERAG_LLM_BASE_URL")),
            "temperature": 0,
        },
        "budget": {"max_steps": 8, "repeats": 1, "max_generation_retries": 1, "cost_threshold": {"avg_tool_calls_ratio": 1.2}},
        "user_simulator": "ecommerce_rag.harness.UserSimulator:v1",
        "scorer": f"ecommerce_rag.harness.grade:{scoring_version}",
        "model_experiment_status": "not_run_until_explicit_endpoint_is_configured",
    }
    if test_result is not None:
        manifest["test_evidence"] = test_result
    if scoring_version == "return-closure-v2":
        manifest["scoring_contract"] = {
            "required_facts": [
                "successful_nonempty_get_policy_result",
                "successful_check_return_eligibility_with_order_and_eligible_fields",
            ],
            "write_confirmation": "successful_create_return_request_requires_request_issued_then_positive_user_response",
            "optional_tools": ["get_order"],
            "tool_order": "no_fixed_order; only required facts, permitted tools, and terminal state are scored",
            "answer_promotion_blockers": [
                "hard_verification_pass_false",
                "answer_fact_pass_false",
                "unsupported_high_risk_claims_nonempty",
                "contradicted_claims_nonempty",
                "omitted_required_facts_nonempty",
            ],
            "citation_binding": "diagnostic_only_unless_an_answer_blocker_is_present",
        }
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", type=Path, default=Path("ecommerce_rag/data/return_closure_tasks.jsonl"))
    parser.add_argument("--index", type=Path)
    parser.add_argument("--output", type=Path, default=Path("docs/experiments/return_closure_freeze_v1.json"))
    parser.add_argument(
        "--scoring-version",
        choices=("return-closure-v1", "return-closure-v2"),
        default="return-closure-v1",
    )
    parser.add_argument("--test-result", help="Recorded final test command result, for example '290 passed'")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest(args.tasks, args.index, args.scoring_version, args.test_result)
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
