#!/usr/bin/env python3
"""Append-Only Quantitative Research Failure Ledger Manager."""

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from typing import Dict, Any, List


def compute_mechanism_hash(signal_family: str, asset_universe: str, horizon_bucket: str) -> str:
    canonical = f"{signal_family.strip().lower()}|{asset_universe.strip().lower()}|{horizon_bucket.strip().lower()}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def load_ledger(ledger_path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(ledger_path):
        return []
    with open(ledger_path, "r", encoding="utf-8") as f:
        return [json.loads(line.strip()) for line in f if line.strip()]


def append_ledger_entry(ledger_path: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    rows = load_ledger(ledger_path)
    mech_hash = entry["mechanism_hash"]
    decision = entry.get("decision", "PRE_REGISTERED")
    if decision == "PRE_REGISTERED":
        prior_dead = [r for r in rows if r.get("mechanism_hash") == mech_hash and r.get("decision") == "NO_EDGE"]
        if prior_dead and not entry.get("new_structural_catalyst"):
            raise RuntimeError("Blocked retest of dead NO_EDGE branch")
    snap = entry["snapshot_sha256"]
    prior_pre_regs = [r for r in rows if r.get("snapshot_sha256") == snap and r.get("decision") == "PRE_REGISTERED"]
    cumulative_k = len(prior_pre_regs) + (1 if decision == "PRE_REGISTERED" else 0)
    record = {
        "experiment_id": entry["experiment_id"],
        "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "snapshot_sha256": snap,
        "hypothesis": entry["hypothesis"],
        "mechanism_hash": mech_hash,
        "cumulative_k": cumulative_k,
        "decision": decision,
        "failure_class": entry.get("failure_class", "none"),
        "paper_only": True,
    }
    with open(ledger_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    return record


def _self_test() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        lf = os.path.join(tmpdir, "research_ledger.jsonl")
        mh = compute_mechanism_hash("rsi_mean_reversion", "us_large_cap", "3d_5d")
        r1 = append_ledger_entry(lf, {"experiment_id": "001", "snapshot_sha256": "s1", "hypothesis": "h1", "mechanism_hash": mh, "decision": "PRE_REGISTERED"})
        assert r1["cumulative_k"] == 1
        append_ledger_entry(lf, {"experiment_id": "001", "snapshot_sha256": "s1", "hypothesis": "h1", "mechanism_hash": mh, "decision": "NO_EDGE", "failure_class": "cost_sensitive"})
        blocked = False
        try:
            append_ledger_entry(lf, {"experiment_id": "002", "snapshot_sha256": "s1", "hypothesis": "h1", "mechanism_hash": mh, "decision": "PRE_REGISTERED"})
        except RuntimeError:
            blocked = True
        assert blocked is True


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        _self_test()
        print(json.dumps({"status": "self_test_passed"}))
        sys.exit(0)
