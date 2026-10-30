#!/usr/bin/env python3
"""Deterministic gate validator for quantitative research experiments."""

import argparse
import json
import sys


def compute_net_bps(gross_bps: float, fee_bps: float, slippage_bps: float, impact_bps: float, opex_bps: float) -> float:
    return gross_bps - fee_bps - slippage_bps - impact_bps - opex_bps


def validate_experiment(payload: dict) -> dict:
    failures = []
    checks = {}

    pre_reg = bool(payload.get("pre_registered", False))
    snap_hash = str(payload.get("snapshot_sha256", "")).strip()
    checks["provenance_and_preregistration"] = pre_reg and len(snap_hash) >= 8
    if not checks["provenance_and_preregistration"]:
        failures.append("evidence_gap: missing pre-registration or snapshot_sha256")

    max_feature_lag_sec = float(payload.get("max_feature_minus_decision_sec", 0.0))
    chronological_split = bool(payload.get("chronological_split_locked", False))
    checks["chronology_clean"] = chronological_split and (max_feature_lag_sec <= 0.0)
    if not checks["chronology_clean"]:
        failures.append("lookahead: feature timestamp exceeds decision timestamp or split unlocked")

    round_trips = int(payload.get("holdout_round_trips", 0))
    checks["sample_power_ge_30"] = round_trips >= 30
    if not checks["sample_power_ge_30"]:
        failures.append(f"underpowered: holdout_round_trips={round_trips} (< 30 minimum)")

    grid_cells = payload.get("neighbor_net_bps", [])
    if not grid_cells:
        checks["neighborhood_stability"] = False
        failures.append("isolated_lucky_cell: no parameter neighborhood grid provided")
    else:
        positive_cells = sum(1 for v in grid_cells if float(v) > 0.0)
        stability_ratio = positive_cells / len(grid_cells)
        checks["neighborhood_stability"] = stability_ratio >= 0.75
        checks["stability_ratio"] = round(stability_ratio, 4)
        if not checks["neighborhood_stability"]:
            failures.append(f"isolated_lucky_cell: stability_ratio={stability_ratio:.2f} (< 0.75)")

    gross_bps = float(payload.get("gross_bps_per_trade", 0.0))
    slippage_bps = float(payload.get("slippage_bps_per_trade", 1.0))
    impact_bps = float(payload.get("impact_bps_per_trade", 1.0))
    opex_bps = float(payload.get("opex_bps_per_trade", 0.0))
    target_fee_bps = float(payload.get("account_fee_bps_round_trip", 5.0))

    fee_grid = {
        f"{fee}bp": round(compute_net_bps(gross_bps, float(fee), slippage_bps, impact_bps, opex_bps), 4)
        for fee in (0, 1, 5, 20)
    }
    target_net = compute_net_bps(gross_bps, target_fee_bps, slippage_bps, impact_bps, opex_bps)
    checks["fee_grid_net_bps"] = fee_grid
    checks["target_account_net_bps"] = round(target_net, 4)
    checks["cost_robust"] = target_net > 0.0 and fee_grid["5bp"] > 0.0
    if not checks["cost_robust"]:
        failures.append(f"cost_sensitive: target_net_bps={target_net:.2f}, 5bp_grid={fee_grid['5bp']:.2f}")

    paper_only = bool(payload.get("paper_only", True))
    live_keys_exposed = bool(payload.get("live_keys_in_env", False))
    checks["safety_gate"] = paper_only and (not live_keys_exposed)
    if not checks["safety_gate"]:
        failures.append("risk_limit: paper_only must be true and live keys must be absent")

    verdict = "PASS_TO_SKEPTIC" if not failures else "FAIL_CLOSED"
    return {"verdict": verdict, "failure_classes": failures, "checks": checks}


def _self_test() -> None:
    passing_sample = {
        "pre_registered": True,
        "snapshot_sha256": "a94f8c12e019b77c",
        "chronological_split_locked": True,
        "max_feature_minus_decision_sec": 0.0,
        "holdout_round_trips": 64,
        "neighbor_net_bps": [14.2, 12.8, 15.1, 11.9, 13.5, 10.4, 12.0, 9.8, -1.2],
        "gross_bps_per_trade": 30.0,
        "slippage_bps_per_trade": 1.0,
        "impact_bps_per_trade": 1.0,
        "opex_bps_per_trade": 0.0,
        "account_fee_bps_round_trip": 5.0,
        "paper_only": True,
        "live_keys_in_env": False,
    }
    res_pass = validate_experiment(passing_sample)
    assert res_pass["verdict"] == "PASS_TO_SKEPTIC"
    failing_sample = dict(passing_sample, gross_bps_per_trade=12.0, account_fee_bps_round_trip=20.0, holdout_round_trips=18)
    res_fail = validate_experiment(failing_sample)
    assert res_fail["verdict"] == "FAIL_CLOSED"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=str)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        _self_test()
        print(json.dumps({"status": "self_test_passed"}))
        sys.exit(0)
    with open(args.input, "r", encoding="utf-8") as f:
        print(json.dumps(validate_experiment(json.load(f)), indent=2))
