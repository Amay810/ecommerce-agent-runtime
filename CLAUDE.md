# Claude working agreement

Read the repository handoff first: [AGENTS.md](AGENTS.md). Keep changes inside
the e-commerce runtime, typed tools, SQLite state, retrieval, evaluation
harness, and documented external adapters. Do not modify `tau3-grpo` or
`fintool-rl`, add training infrastructure, commit credentials/models/logs, or
claim Rule/Oracle/CPU results as model results.

Before reporting a change, run the smallest relevant import/compile/test and
state whether it used CPU deterministic policies, a real model service, or an
external environment. Preserve frozen experiment artifacts and their original
code/scoring attribution.
