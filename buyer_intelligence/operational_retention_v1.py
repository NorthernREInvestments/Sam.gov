"""Bounded operational retention — keep actionable knowledge, compact noise.

Does NOT bulk-delete the canonical L23 store. Requires an explicit verified
checkpoint before any discard pass. Fingerprints of discarded rows are retained
to prevent re-processing.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from application_clock import now_utc
from m3_data_root import data_path

BUILD = "20261013-m3-operational-retention-v1"
OUT = "_og_commercial_research_pipeline_out"
POLICY = f"{OUT}/m3_operational_retention_v1_policy.json"
FINGERPRINTS = f"{OUT}/m3_operational_retention_v1_fingerprints.json"
AUDIT = f"{OUT}/m3_operational_retention_v1_audit.json"

RETAIN_CLASSES = {
    "LIVE_ACTIONABLE",
    "QUALIFIED_NEXT_ACTION",
    "SUPPLIER_QUOTE",
    "HISTORICAL_GOV_PRICE",
    "VERIFIED_RETAIL_PRICE",
    "FINANCING_EVIDENCE",
    "SUBMITTED_BID",
    "AWARD_FULFILLMENT",
    "SOURCE_ACCESS_IMPROVEMENT",
    "AUDIT_CHECKPOINT",
}

DISCARD_CANDIDATE_CLASSES = {
    "EXPIRED_NO_RESEARCH_VALUE",
    "INELIGIBLE_SET_ASIDE",
    "DUPLICATE_SUPERSEDED",
    "UNUSABLE_SERVICE",
    "METADATA_ONLY_REJECT_AFTER_RECOVERY",
    "REDUNDANT_SOURCE_SNAPSHOT",
    "TEMP_EXTRACTION_AFTER_DURABLE",
}


def retention_policy() -> dict[str, Any]:
    policy = {
        "kind": "OperationalRetentionPolicy",
        "build": BUILD,
        "updated_at": now_utc().isoformat(),
        "retain": sorted(RETAIN_CLASSES),
        "discard_candidates": sorted(DISCARD_CANDIDATE_CLASSES),
        "hard_rules": [
            "Never bulk-delete L23 without tested migration + reconciliation + backup",
            "Never delete evidence for active bids, awards, required records, or ongoing research",
            "Preserve full-cohort audit checkpoint before any retention pass",
            "Keep minimal fingerprints/reason codes/timestamps for rejected rows",
            "Goal is bounded operational storage — not zero persistence",
        ],
        "required_checkpoint_before_discard": [
            "M3_Backups/pre_funnel_full_cohort_*",
            f"{OUT}/m3_fcf_v1_scorecard.json",
            f"{OUT}/m3_fcf_v1_classifications.json",
        ],
        "auto_discard_enabled": False,
        "NOTE": "Discard passes are owner-gated. This module records policy + fingerprints only.",
    }
    path = Path(data_path(POLICY))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(policy, indent=2), encoding="utf-8")
    return policy


def record_fingerprint(
    *,
    opportunity_id: str,
    reason_code: str,
    class_name: str,
    deadline: str | None = None,
) -> dict[str, Any]:
    path = Path(data_path(FINGERPRINTS))
    bag: dict[str, Any]
    if path.exists():
        try:
            bag = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            bag = {"kind": "OperationalRetentionFingerprints", "build": BUILD, "by_id": {}}
    else:
        bag = {"kind": "OperationalRetentionFingerprints", "build": BUILD, "by_id": {}}
    bag.setdefault("by_id", {})[str(opportunity_id)] = {
        "opportunity_id": str(opportunity_id),
        "reason_code": reason_code,
        "class": class_name,
        "deadline": deadline,
        "recorded_at": now_utc().isoformat(),
    }
    bag["count"] = len(bag["by_id"])
    bag["updated_at"] = now_utc().isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bag, indent=2), encoding="utf-8")
    return bag["by_id"][str(opportunity_id)]


def verify_cohort_checkpoint_readable() -> dict[str, Any]:
    """Confirm the full-cohort audit artifacts exist and parse."""
    checks = []
    for rel in (
        f"{OUT}/m3_fcf_v1_scorecard.json",
        f"{OUT}/m3_fcf_v1_classifications.json",
        f"{OUT}/m3_wddr_v1_report.json",
    ):
        p = Path(data_path(rel))
        ok = False
        err = None
        size = 0
        if p.exists():
            size = p.stat().st_size
            try:
                json.loads(p.read_text(encoding="utf-8"))
                ok = True
            except Exception as exc:
                err = f"{type(exc).__name__}"
        checks.append({"path": rel, "exists": p.exists(), "readable": ok, "bytes": size, "error": err})
    backup_root = Path(data_path("..")) / "M3_Backups"
    # also check sibling OneDrive backup convention
    alt = Path(r"C:\Users\M_gra\OneDrive\Desktop\Sam.Gov App\M3_Backups")
    backups = []
    for root in (backup_root, alt):
        if root.exists():
            for d in sorted(root.glob("pre_funnel_full_cohort_*"), reverse=True)[:3]:
                backups.append({"path": str(d), "exists": True})
    out = {
        "kind": "OperationalRetentionCheckpointVerify",
        "build": BUILD,
        "at": now_utc().isoformat(),
        "cohort_artifacts": checks,
        "backups_found": backups,
        "ok": all(c["readable"] for c in checks),
        "auto_discard_enabled": False,
    }
    Path(data_path(AUDIT)).parent.mkdir(parents=True, exist_ok=True)
    Path(data_path(AUDIT)).write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out
