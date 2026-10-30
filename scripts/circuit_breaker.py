#!/usr/bin/env python3
"""Automated Governance Circuit Breaker & Incident-to-Eval Scorer (@code-review-governance)."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any, Dict, List


TIER_2_PATHS = (
    ".github/workflows/",
    "Dockerfile",
    "docker-compose.yml",
    "systemd/",
    "ouroboros/tools.py",
    "scripts/circuit_breaker.py",
    "CODEOWNERS",
    "REVIEW.md",
    "AGENTS.md",
)

TIER_0_PATHS = (
    "docs/",
    "tests/",
    "README.md",
)


def classify_diff_paths(changed_files: List[str], breaker_tripped: bool = False) -> Dict[str, Any]:
    """Determines required human approvals from the changed file paths (the diff sets the risk tier)."""
    tier = "Tier 0"
    reasons: List[str] = []

    for fpath in changed_files:
        if any(fpath == p or fpath.startswith(p) for p in TIER_2_PATHS):
            tier = "Tier 2"
            reasons.append(f"{fpath} matches Always-Human Tier-2 path")
        elif not any(fpath == p or fpath.startswith(p) for p in TIER_0_PATHS) and tier != "Tier 2":
            tier = "Tier 1"
            reasons.append(f"{fpath} modifies runtime logic or skills")

    if breaker_tripped and tier == "Tier 0":
        tier = "Tier 1"
        reasons.append("Circuit breaker active: Tier 0 elevated to Tier 1 (1 human required)")

    required_humans = {"Tier 0": 0, "Tier 1": 1, "Tier 2": 2}[tier]
    return {
        "risk_tier": tier,
        "required_human_approvals": required_humans,
        "reasons": reasons,
    }


def evaluate_circuit_breakers(telemetry: Dict[str, Any]) -> Dict[str, Any]:
    """Evaluates the 4 automated circuit breakers defined in @code-review-governance."""
    merged = max(1, int(telemetry.get("merged_prs_7d", 1)))
    reverted = int(telemetry.get("reverted_prs_7d", 0))
    revert_rate_7d = reverted / merged
    baseline_rate = float(telemetry.get("baseline_30d_revert_rate", 0.02))
    sev_escapes = int(telemetry.get("sev1_sev2_escapes_from_tier0_tier1", 0))
    mean_rounds = float(telemetry.get("mean_rounds_to_pass", 1.0))
    max_rounds = int(telemetry.get("max_pr_rounds_observed", 1))
    hotfixes_14d: Dict[str, int] = telemetry.get("module_hotfixes_14d", {})

    tripped_breakers: List[str] = []
    actions: List[str] = []

    # 1. 7-Day PR Revert Rate Breaker
    if revert_rate_7d > 0.03 or revert_rate_7d > (1.25 * baseline_rate):
        tripped_breakers.append("revert_rate_breaker")
        actions.append("Suspend Tier 0 (0-human merges); elevate all Low-risk PRs to Tier 1.")

    # 2. Sev-1 / Sev-2 Incident Escape Breaker
    if sev_escapes >= 1:
        tripped_breakers.append("sev_incident_escape_breaker")
        actions.append("Freeze Tier 0 in affected subsystem and promote directory to Tier 2.")

    # 3. PR Review Convergence (rounds-to-pass) Breaker
    if max_rounds > 3 or mean_rounds > 1.8:
        tripped_breakers.append("convergence_loop_breaker")
        actions.append("Halt automated re-review on stalled PRs and escalate for human architecture review.")

    # 4. Directory Hotfix Churn Breaker
    quarantined_modules = [mod for mod, count in hotfixes_14d.items() if count >= 4]
    if quarantined_modules:
        tripped_breakers.append("directory_hotfix_churn_breaker")
        actions.append(
            f"Entropy Quarantine on {', '.join(quarantined_modules)}: require Tier 2 review."
        )

    return {
        "status": "TRIPPED" if tripped_breakers else "HEALTHY",
        "revert_rate_7d": round(revert_rate_7d, 4),
        "tripped_breakers": tripped_breakers,
        "automated_actions": actions,
    }


def score_incident_evals(evals_path: pathlib.Path) -> Dict[str, Any]:
    """Scores reviewer bug recall and harmful false-positive rate on incident_evals.jsonl."""
    records = [
        json.loads(line)
        for line in evals_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not records:
        return {"recall": 1.0, "harmful_fp_rate": 0.0, "cases": 0}
    recalled = sum(1 for r in records if r.get("reviewer_flagged") is True)
    false_pos = sum(1 for r in records if r.get("false_positive_on_clean_pair") is True)
    return {
        "cases": len(records),
        "recall": round(recalled / len(records), 4),
        "harmful_fp_rate": round(false_pos / len(records), 4),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run governance circuit breaker & eval checks.")
    parser.add_argument(
        "--evals",
        default="evals/incident_evals.jsonl",
        help="Path to Golden Incident JSONL dataset.",
    )
    parser.add_argument(
        "--telemetry",
        default="evals/telemetry_window.json",
        help="Path to rolling telemetry JSON.",
    )
    parser.add_argument(
        "--changed-files",
        nargs="*",
        default=[],
        help="Optional list of changed files to classify into Tier 0/1/2.",
    )
    args = parser.parse_args()

    telemetry = json.loads(pathlib.Path(args.telemetry).read_text(encoding="utf-8"))
    breaker_report = evaluate_circuit_breakers(telemetry)
    eval_report = score_incident_evals(pathlib.Path(args.evals))
    diff_report = (
        classify_diff_paths(args.changed_files, breaker_tripped=(breaker_report["status"] == "TRIPPED"))
        if args.changed_files
        else None
    )

    summary = {
        "circuit_breaker": breaker_report,
        "incident_eval_scores": eval_report,
        "diff_classification": diff_report,
    }
    print(json.dumps(summary, indent=2))
    if eval_report["recall"] < 1.0 or eval_report["harmful_fp_rate"] > 0.10:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
