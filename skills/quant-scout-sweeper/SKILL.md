---
name: quant-scout-sweeper
description: Generate falsifiable quantitative trading hypotheses (Scout) and execute parameter neighborhood sweeps with mandatory net-of-cost accounting across multi-tier fee and slippage grids (Sweeper).
enabled: true
---
# Quant Scout and Sweeper

- **Data Firewall**: Operate strictly on `data/develop/` (first 70%) and `data/select/` (next 15%). Never access `data/holdout/`.
- **Net-Cost Arithmetic**: Execute `python3 skills/quant-scout-sweeper/scripts/net_cost_sweeper.py` to evaluate `R_net = R_g - C_f - C_s - C_m - C_o` across `0bp`, `1bp`, `5bp`, and `20bp` fee tiers and verify `>= 0.75` parameter plateau stability.
