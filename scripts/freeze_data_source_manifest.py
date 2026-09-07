# -*- coding: utf-8 -*-
"""Build the P0 data-source freeze manifest from the audited sources ledger."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from ecommerce_rag.tau3_retail_v1 import EXPECTED_SPLITS, TAU2_COMMIT, validate_tau2_checkout

MS_SWIFT_FULL_COMMIT = "f2797138dba0e224cfff735cd89a528a08d8732a"
MS_SWIFT_TAG = "v4.2.2"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sources",
        type=Path,
        default=Path("docs/verified_ecommerce_agent_learning_v2_sources.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/data_source_manifest.json"),
    )
    parser.add_argument(
        "--tau-root",
        type=Path,
        default=Path(os.environ["TAU_ROOT"]) if os.environ.get("TAU_ROOT") else None,
        help="Pinned Tau2 checkout; defaults to TAU_ROOT.",
    )
    parser.add_argument(
        "--skip-tau-validate",
        action="store_true",
        help="Keep prior splits when the local τ³ checkout is unavailable.",
    )
    args = parser.parse_args(argv)

    if not args.skip_tau_validate and args.tau_root is None:
        parser.error("--tau-root or TAU_ROOT is required unless --skip-tau-validate is set")

    sources_doc = json.loads(args.sources.read_text(encoding="utf-8"))
    tau_status = {
        "validated": False,
        "commit": TAU2_COMMIT,
        "splits": EXPECTED_SPLITS,
    }
    if not args.skip_tau_validate:
        splits = validate_tau2_checkout(args.tau_root)
        tau_status = {"validated": True, "commit": TAU2_COMMIT, "splits": splits}

    # Patch ms-swift full commit into the sources ledger when still null.
    for framework in sources_doc.get("frameworks") or []:
        if framework.get("id") == "ms_swift" and not framework.get("full_commit"):
            framework["full_commit"] = MS_SWIFT_FULL_COMMIT
            framework["resolved_at"] = datetime.now(timezone.utc).date().isoformat()

    entries = []
    for source in sources_doc.get("sources") or []:
        entries.append(
            {
                "id": source["id"],
                "kind": source.get("kind"),
                "license": source.get("license"),
                "pin": source.get("pin"),
                "allowed_uses": source.get("allowed_uses"),
                "forbidden_uses": source.get("forbidden_uses"),
                "touches_tau3_test": source["id"]
                in {"tau3_retail_v1_0_1", "apigen_mt_5k"},
                "gate": source.get("gate"),
                "default_in_main_experiment": source["id"]
                in {
                    "tau3_retail_v1_0_1",
                    "native_synthetic_retail_db",
                },
            }
        )

    manifest = {
        "schema_version": "1.0",
        "frozen_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "project_model": sources_doc.get("project_model"),
        "plan_doc": "docs/current_status.md",
        "archived_plan_doc": (
            "https://github.com/Amay810/ecommerce-agentic-rag-archive/"
            "blob/main/docs/verified_ecommerce_agent_learning_v2_plan.md"
        ),
        "sources_ledger": "docs/verified_ecommerce_agent_learning_v2_sources.json",
        "tau3_retail": tau_status,
        "frameworks": {
            "ms_swift": {
                "tag": MS_SWIFT_TAG,
                "full_commit": MS_SWIFT_FULL_COMMIT,
                "release_short_sha": "f279713",
                "gate": (
                    "CUDA/PyTorch/vLLM lock still pending NSCC inspection; "
                    "do not start training until that lock exists."
                ),
            }
        },
        "open_gates": [],
        "active_gates": [
            "phase1_write_gate_measurement",
        ],
        "deferred_gates": [
            "user_simulator_model_version",
            "nl_assertions_judge_model",
            "nscc_cuda_pytorch_vllm_lock",
        ],
        "closed_gates": [
            "teacher_model_version",
            "apigen_terms_acceptance",
            "bfcl_commit_pin",
            "ecom_bench_commit_pin",
        ],
        "next_package": "Phase 1 write-gate measurement (ask_user vs premature write)",
        "sources": entries,
    }

    args.sources.write_text(
        json.dumps(sources_doc, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    args.output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {args.output}")
    print(f"updated {args.sources} ms_swift.full_commit={MS_SWIFT_FULL_COMMIT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
