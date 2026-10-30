---
name: ai-quant-research-desk
description: Run a complete five-role AI quantitative research team (Lead, Scout, Sweeper, Skeptic, Librarian) from a single unified skill with isolated role workspaces.
enabled: true
---
# AI Quant Research Desk (Unified 5-Role Pipeline)

When given a market direction or asset theme:
1. Dispatch `delegate_subagent(role="librarian", ...)` to run `scripts/ledger_manager.py` for corpse-checking and pre-registration (`PRE_REGISTERED`, incrementing `K`).
2. Dispatch `delegate_subagent(role="scout", ...)` to formulate a falsifiable point-in-time signal and parameter grid without touching `data/holdout/`.
3. Dispatch `delegate_subagent(role="sweeper", ...)` to run `scripts/net_cost_sweeper.py` on `data/develop/` and `data/select/` and validate via `scripts/quant_gate_validator.py`.
4. If and only if Gate 4 returns `PASS_TO_SKEPTIC`, dispatch `delegate_subagent(role="skeptic", ...)` on the frontier model to execute the 5 Kill Vectors (`scripts/deflated_sharpe_auditor.py` + single-shot `data/holdout/` replay).
5. Dispatch `delegate_subagent(role="librarian", ...)` to commit `NO_EDGE` or `PAPER_CANDIDATE` (`paper_only = true`).
