#!/usr/bin/env python3
"""Deflated Sharpe Ratio (DSR) and Multiple-Testing Haircut Auditor."""

import argparse
import json
import math
import sys

EULER_MASCHERONI = 0.5772156649015329


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    if p <= 0.0 or p >= 1.0:
        raise ValueError("Probability p must be in (0, 1)")
    if p < 0.5:
        return -norm_ppf(1.0 - p)
    t = math.sqrt(-2.0 * math.log(1.0 - p))
    c0, c1, c2 = 2.515517, 0.802853, 0.010328
    d1, d2, d3 = 1.432788, 0.189269, 0.001308
    return t - ((c2 * t + c1) * t + c0) / (((d3 * t + d2) * t + d1) * t + 1.0)


def expected_max_sharpe(num_trials_k: int, std_sr_across_trials: float = 1.0) -> float:
    if num_trials_k <= 1:
        return 0.0
    z1 = norm_ppf(1.0 - 1.0 / num_trials_k)
    z2 = norm_ppf(1.0 - 1.0 / (num_trials_k * math.e))
    return std_sr_across_trials * ((1.0 - EULER_MASCHERONI) * z1 + EULER_MASCHERONI * z2)


def compute_deflated_sharpe(
    observed_sr_non_annualized: float,
    sample_length_t: int,
    num_trials_k: int,
    skewness: float = 0.0,
    kurtosis: float = 3.0,
    std_sr_trials: float = 0.5,
) -> dict:
    sr_benchmark = expected_max_sharpe(num_trials_k, std_sr_trials)
    variance_term = 1.0 - (skewness * observed_sr_non_annualized) + (((kurtosis - 1.0) / 4.0) * (observed_sr_non_annualized ** 2))
    if variance_term <= 0.0:
        variance_term = 1e-6
    denom = math.sqrt(variance_term / (sample_length_t - 1.0))
    z_score = (observed_sr_non_annualized - sr_benchmark) / denom
    dsr_prob = norm_cdf(z_score)
    return {
        "observed_sr_per_period": round(observed_sr_non_annualized, 4),
        "benchmark_max_sr_null": round(sr_benchmark, 4),
        "num_trials_k": num_trials_k,
        "sample_length_t": sample_length_t,
        "dsr_z_score": round(z_score, 4),
        "dsr_probability": round(dsr_prob, 4),
        "passes_dsr_95_gate": dsr_prob >= 0.95,
    }


def _self_test() -> None:
    res_strong = compute_deflated_sharpe(0.25, 252, 3, 0.0, 3.0, 0.1)
    assert res_strong["passes_dsr_95_gate"] is True
    res_mined = compute_deflated_sharpe(0.12, 120, 200, -1.5, 8.0, 0.15)
    assert res_mined["passes_dsr_95_gate"] is False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        _self_test()
        print(json.dumps({"status": "self_test_passed"}))
        sys.exit(0)
