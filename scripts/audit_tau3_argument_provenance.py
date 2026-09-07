"""Run the diagnostic-only Argument Provenance Auditor v1 on JSON/JSONL."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ecommerce_rag.diagnostics.argument_provenance import audit_trajectory, first_causal_error


def _load_records(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    payload = json.loads(text)
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        simulations = payload.get("simulations")
        if isinstance(simulations, list):
            return [dict(item) for item in simulations if isinstance(item, dict)]
        return [payload]
    raise ValueError(f"unsupported input payload: {path}")


def _load_schemas(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("schema file must be a JSON object keyed by tool name")
    return {str(key): dict(value) for key, value in payload.items() if isinstance(value, dict)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--arm", choices=("base", "grpo37"), required=True)
    parser.add_argument("--schemas", type=Path)
    args = parser.parse_args()

    records = _load_records(args.input)
    schemas = _load_schemas(args.schemas)
    audits: list[dict[str, Any]] = []
    first_causes: list[dict[str, Any]] = []
    manual: list[dict[str, Any]] = []
    for record in records:
        rows = audit_trajectory(record, tool_schemas=schemas)
        audits.extend(row.as_dict() for row in rows)
        cause = first_causal_error(record, rows)
        if cause is not None:
            first_causes.append(cause)
            if cause.get("confidence") == "low" or cause.get("subcategory") == "manual_review_required":
                manual.append(cause)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {
        f"{args.arm}_argument_audit.jsonl": audits,
        f"{args.arm}_first_causal.jsonl": first_causes,
        f"{args.arm}_manual_review.jsonl": manual,
    }
    for filename, rows in outputs.items():
        target = args.output_dir / filename
        with target.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    print(json.dumps({"status": "PASS", "arm": args.arm, "records": len(records), "argument_rows": len(audits), "first_causal_rows": len(first_causes), "manual_review_rows": len(manual)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
