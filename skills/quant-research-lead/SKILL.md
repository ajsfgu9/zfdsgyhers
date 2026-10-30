---
name: quant-research-lead
description: Orchestrate a multi-agent quantitative research desk with strict role separation, point-in-time data hygiene, chronological holdout splits, net-cost accounting, adversarial falsification, and an append-only failure ledger.
enabled: true
---
# Quant Research Lead (8-Gate Multi-Agent Orchestrator)

1. **Never Grade Your Own Homework**: Delegate hypothesis ideation to `scout`, grid sweeps to `sweeper`, out-of-sample holdout falsification to `skeptic`, and ledger pre-registration/commits to `librarian` via `delegate_subagent`.
2. **Enforce Physical Data Walls**: `scout` and `sweeper` are blocked from `data/holdout/`. Only `skeptic` can access `data/holdout/` after deterministic gate validation (`scripts/quant_gate_validator.py`) returns `PASS_TO_SKEPTIC`.
3. **Gross Is Not Tradable**: Require `R_net = R_g - C_f - C_s - C_m - C_o` across `[0, 1, 5, 20] bp` round-trip fee tiers, `>= 30` trades, and `>= 0.75` parameter neighborhood stability.
