# P9 Phase C0 — Canonical Monorepo Baseline

This document records the single pre-split integration baseline. It is an
integration record, not a Repo A/Repo B split output; the split itself is
explicitly out of scope for C0.

## Base and scope

- Base: `origin/main` at `e7efe4f7c66b52ee97b396e54e6381df2502d8e5`.
- Branch/worktree: `split/canonical-integration` at
  `/tmp/ecommerce-canonical-integration`.
- Canonical package names remain unchanged. No `ecommerce_rag.grpo` to
  `tau3_grpo` rename, A/B relocation, or AutoDL verifier/contrast import was
  performed.

## Integrated changes

### A diagnostics

Ported the CPU-only transaction contract audit, its CLI, tests, and contract
documentation:

```text
ecommerce_rag/diagnostics/__init__.py
ecommerce_rag/diagnostics/transaction_audit.py
scripts/audit_transaction_contracts.py
tests/test_transaction_audit.py
docs/transaction_contracts.md
```

The audit was checked against the canonical 15-tool MCP surface and current
`READ_TOOLS`/`WRITE_TOOLS`; import compatibility passed.

### B diagnostics

Ported the diagnostic-only argument provenance auditor, paired-audit builder,
CLIs, and tests:

```text
ecommerce_rag/diagnostics/argument_provenance.py
scripts/audit_tau3_argument_provenance.py
scripts/build_tau3_paired_audit.py
tests/test_argument_provenance.py
```

These files have no GRPO reward, Tau2, VERL, torch, or model-training import.

### Local tracked delta

- `nscc/train_tau3_grpo_v1.pbs`: retained the durable stdout/stderr/combined
  `tee` logs, `GRPO_LOG_DIR`, and `PYTHONFAULTHANDLER` behavior; the patch was
  manually reconciled against the newer `origin/main` PBS file.
- `tests/test_tau3_grpo_integration.py`: retained the behavior-level launcher
  persistence assertions without depending on one exact shell argument layout.

### Dockerfile

Removed the stale `RUN python -m ecommerce_rag.data_loader` line. No missing
module was created to satisfy that dangling entrypoint.

### AutoDL experiments

The `dcec71e` and `948e217` verifier/contrast experiments remain evidence-only
and are excluded from the canonical tree. Their branch/hash preservation is
recorded in the C0 handoff; no verifier, contrast, or AutoDL-only source was
copied.

## Validation

- `compileall`: PASS for `ecommerce_rag`, `scripts`, and `tests`.
- AST parsing: PASS for the repository Python sources.
- Targeted CPU tests: `73 passed, 2 skipped, 3 subtests passed`.
- Full CPU pytest: `310 passed, 4 skipped, 3 subtests passed`; four failures
  are external frozen-Tau2 boundary failures, not canonical source failures:
  two missing external `addict` runtime dependency failures and two circular
  imports in the frozen external Tau2 checkout.
- `python scripts/train_tau3_grpo.py --check-only`: PASS for one-step and
  nine-step artifact checks.
- No GPU, VERL, vLLM, flashinfer, endpoint, or online training run was
  attempted in C0.

## Dependency/path audit

- GRPO package -> non-GRPO local package: none; the package uses stdlib and
  internal GRPO-relative imports.
- Non-GRPO/script/test -> GRPO: only the existing explicit Tau3 launcher,
  audit, test, and Tau3 adapter entrypoints; no new cross-boundary import was
  introduced by diagnostics.
- Non-import split rewrites required in C0: none; canonical package paths are
  intentionally still unchanged.
- Hardcoded operational paths remain only in existing NSCC/AutoDL launch
  recipes (Tau2 checkout, model, conda, and cache defaults); they are not
  canonical local runtime imports.

## Final manifest partition

The following counts are the exact partition of the post-integration tracked
tree; the report itself is integration-only and classified `EXCLUDE` from a
future A/B split:

| class | count |
| --- | ---: |
| `A_ONLY` | 94 |
| `B_ONLY` | 38 |
| `BOTH_COPY` | 4 |
| `REWRITE_PER_REPO` | 13 |
| `GENERATE_PER_REPO` | 1 |
| `EXCLUDE` | 4 |
| `UNDECIDED` | 0 |
| tracked | 154 |
| coverage | `154 / 154`, PASS |

The four newly ported A-side files are `transaction_audit.py`, its CLI, its
test, and `transaction_contracts.md`; the four newly ported B-side files are
`argument_provenance.py`, its CLI, the paired-audit builder, and its test.
`ecommerce_rag/diagnostics/__init__.py` is the fourth `BOTH_COPY` file.

## C0 exit condition

This baseline is ready for a later, separately authorized A/B split after the
single canonical integration commit is created. C0 stops here.
