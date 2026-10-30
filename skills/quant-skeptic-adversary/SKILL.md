---
name: quant-skeptic-adversary
description: Act as an adversarial falsifier (Skeptic) to break surviving quantitative trading strategies across out-of-sample holdout periods, worst-drawdown regimes, cross-universe transfers, and multiple-testing Deflated Sharpe Ratio haircuts.
enabled: true
---
# Quant Skeptic Adversary

- **Context Isolation**: Ignore narrative prose from Scout/Sweeper. Inspect only the structured JSON `handoff_payload` (`hypothesis`, frozen parameters, `cumulative_k`, and sweep grid).
- **5 Kill Vectors**: Run `python3 skills/quant-skeptic-adversary/scripts/deflated_sharpe_auditor.py` (veto if `DSR < 0.95`), execute a single-shot replay on `data/holdout/`, dissect worst-drawdown regimes, test cross-universe transfer, and stress `+1 bar` execution latency.
