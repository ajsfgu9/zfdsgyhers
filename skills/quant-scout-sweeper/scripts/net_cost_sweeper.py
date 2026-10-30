#!/usr/bin/env python3
"""Deterministic Net-Cost Calculator and Parameter Neighborhood Sweep Evaluator."""

import argparse
import json
import sys
from typing import List, Dict, Any


def half_spread_bps(bid: float, ask: float) -> float:
    mid = (bid + ask) / 2.0
    if mid <= 0.0 or ask < bid:
        raise ValueError(f"Invalid order book quote: bid={bid}, ask={ask}")
    return ((ask - bid) / 2.0) / mid * 10000.0


def evaluate_fee_grid(
    gross_bps_per_trade: float,
    slippage_bps: float,
    half_spread_bp: float,
    impact_stress_bp: float,
    opex_bps: float,
    fee_columns_bps: List[float] = [0.0, 1.0, 5.0, 20.0],
) -> Dict[str, float]:
    results = {}
    total_friction_ex_fee = slippage_bps + half_spread_bp + impact_stress_bp + opex_bps
    for fee in fee_columns_bps:
        net = gross_bps_per_trade - fee - total_friction_ex_fee
        results[f"{int(fee)}bp"] = round(net, 4)
    return results


def evaluate_grid_stability(grid_2d: List[List[float]], center_r: int, center_c: int) -> Dict[str, Any]:
    rows = len(grid_2d)
    cols = len(grid_2d[0]) if rows > 0 else 0
    neighbors = []
    for r in range(max(0, center_r - 1), min(rows, center_r + 2)):
        for c in range(max(0, center_c - 1), min(cols, center_c + 2)):
            neighbors.append(float(grid_2d[r][c]))

    pos_count = sum(1 for val in neighbors if val > 0.0)
    ratio = pos_count / len(neighbors) if neighbors else 0.0
    mean_net = sum(neighbors) / len(neighbors) if neighbors else 0.0
    return {
        "neighbor_count": len(neighbors),
        "positive_neighbors": pos_count,
        "stability_ratio": round(ratio, 4),
        "neighborhood_mean_net_bps": round(mean_net, 4),
        "passes_plateau_gate": ratio >= 0.75 and grid_2d[center_r][center_c] > 0.0,
    }


def _self_test() -> None:
    grid_a = evaluate_fee_grid(12.0, slippage_bps=1.0, half_spread_bp=0.0, impact_stress_bp=0.0, opex_bps=0.0)
    assert grid_a["20bp"] == -9.0
    plateau = [[8.5, 11.2, 9.4], [10.1, 14.0, 12.3], [7.8, 9.9, -0.5]]
    stab = evaluate_grid_stability(plateau, 1, 1)
    assert stab["passes_plateau_gate"] is True


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        _self_test()
        print(json.dumps({"status": "self_test_passed"}))
        sys.exit(0)
