"""Frozen ITER-23 NEW-20 regression corpus — do not substitute rows."""

from __future__ import annotations

import json
from typing import Any

from application_clock import now_utc

BUILD = "20261007-m3-same20-full-pipeline-recovery-v1"
CORPUS_FILE = "m3_same20_regression_corpus.json"
SOURCE_JOB = "ASR-5448d09c35f1"
SOURCE_RUN = "ASR-20261007182448"

# Exact 20 valid product-dominant opportunities from ITER-23 (stable_key order frozen)
SAME20_STABLE_KEYS: list[str] = [
    "44cfff919d0b34de",  # Annual Water Material Purchase Contract 2027
    "18fc084a01db4a42",  # Art Supplies and Related Materials
    "b40059ab7b97fcd6",  # Vehicle Items D1
    "45ff02d30816261f",  # ROBOTICS & AUTOMATION EQUIPMENT
    "ccb8dfe0e5772614",  # ITB #27-002 Welding Lab Equipment
    "2840b0c1639de64a",  # HVAC for Districts 4, 6, 7, 8 & 9 - FY27
    "85973e46e2291dbd",  # One (1) 4x4 Utility Vehicle
    "e512e902fcf8dd02",  # J061--Uninterruptible Power Supply St. Louis VA Medical Center
    "2318751f275b130f",  # CJCF HVAC Systems FY27 MRO
    "775f10be98d07352",  # LAW ENFORCEMENT DUTY GEAR AND NON-BALLISTIC TACTICAL EQUIPMENT
    "5d56659e7d8db873",  # Electrical Substation Replacement Project — Equipment Procurement Phase
    "602b9071cb8e3a58",  # One or More 2027 Ford Expedition SSV Vehicle(s) for MC Sheriff's Office
    "ea313d9636775882",  # Fire Equipment for Madison County Volunteer Fire Departments
    "c6395e4a8eb81a0e",  # BRADLEY LAKE — 3-PHASE POWER LONG LEAD EQUIPMENT
    "36329c383a367565",  # SUPPLY: WI-LACROSSE FWCO-TELEMETRY TAGS
    "ddd910bfbe938b8c",  # Radiologic Equipment
    "93cbf9d936c016bf",  # Light Rail Vehicle New and Used Axle Press
    "b2b485c8b56b01f2",  # Law Enforcement Duty Gear and Non-Ballistic Tactical Equipment
    "4aed58fd8b05a79c",  # TRANSFER STATION CLEAN GREEN WASTE MATERIAL
    "c74a381a298f1434",  # Public Works Fleet Services New Vehicle Purchases FY2026/27
]

BASELINE_ITER23 = {
    "VALID_PRODUCT_DOMINANT": 20,
    "VALID_LOCAL_PACKAGES": 3,
    "AUTHORITATIVE_PRODUCT_DOCS": 2,
    "LINES_READY": 3,
    "MATERIAL_PRODUCT_LINES": 22,
    "A_E_IDENTITY_OPPS": 1,
    "A_E_IDENTITY_LINES": 1,
    "REVENUE_READY_OPPS": 0,
    "PUBLIC_PRICE_READY_OPPS": 0,
    "PUBLIC_PRICED_LINES": 0,
    "PUBLIC_PRICE_COVERAGE_50": 0,
    "PUBLIC_PRICE_COVERAGE_75": 0,
    "CHANNEL_CLASSIFIED": 0,
    "POSITIVE_HEADROOM": 0,
    "CALL_TODAY": 0,
    "blockers": {
        "PACKAGE_INCOMPLETE": 17,
        "NO_AUTHORITATIVE_PRODUCT_DOC": 1,
        "NO_LINES": 0,
        "PARTIAL_LINES": 1,
        "IDENTITY_AMBIGUOUS": 0,
        "IDENTITY_WEAK": 0,
        "NO_REVENUE": 20,
        "NO_PUBLIC_PRICE": 20,
        "PUBLIC_COVERAGE_LOW": 0,
        "CHANNEL_UNKNOWN": 20,
        "NO_HEADROOM": 20,
        "TIMEOUT": 0,
        "OTHER": 1,
    },
    "runtime_s": 3646.1,
}


def freeze_corpus_from_report(report: dict[str, Any], store_by_sk: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build SAME20_REGRESSION_CORPUS payload from ITER-23 report + optional store rows."""
    store_by_sk = store_by_sk or {}
    by: dict[str, dict[str, Any]] = {}
    for t in report.get("top_recovered") or []:
        if isinstance(t, dict) and t.get("opportunity"):
            by[str(t["opportunity"])] = dict(t)
    for u in report.get("unrecovered_cases") or []:
        if not isinstance(u, dict):
            continue
        sk = str(u.get("opportunity") or "")
        if not sk:
            continue
        by.setdefault(sk, {})
        by[sk].update({k: v for k, v in u.items() if v is not None})

    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for sk in SAME20_STABLE_KEYS:
        r = by.get(sk) or {}
        sr = store_by_sk.get(sk) or {}
        if not r and not sr:
            missing.append(sk)
        title = str(r.get("title") or sr.get("title") or "")
        rows.append(
            {
                "stable_key": sk,
                "canonical_opportunity_id": str(
                    sr.get("canonical_opportunity_id") or r.get("canonical_opportunity_id") or sk
                ),
                "title": title,
                "buyer": r.get("buyer") or sr.get("buyer"),
                "deadline": sr.get("deadline") or r.get("deadline"),
                "source": "bidnet",
                "package_host": "bidnetdirect.com",
                "authoritative_url": sr.get("authoritative_url")
                or (sr.get("row_ref") or {}).get("detail_url")
                if isinstance(sr.get("row_ref"), dict)
                else sr.get("authoritative_url"),
                "selection_reason": "ITER23_NEW20_VALID_PRODUCT_DOMINANT",
                "product_dominant_proof": {
                    "counts_toward_new20": True,
                    "source_job": SOURCE_JOB,
                    "source_run": SOURCE_RUN,
                },
                "baseline_stage": {
                    "classification": r.get("classification") or r.get("product_signals"),
                    "documents_acquired": r.get("documents_acquired"),
                    "product_schedule": r.get("product_schedule"),
                    "extracted_lines": r.get("extracted_lines"),
                    "operator_status": r.get("operator_status") or r.get("why_no_lines"),
                },
            }
        )

    payload = {
        "build": BUILD,
        "name": "SAME20_REGRESSION_CORPUS",
        "source_job": SOURCE_JOB,
        "source_run": SOURCE_RUN,
        "frozen_at": now_utc().isoformat(),
        "stable_keys": list(SAME20_STABLE_KEYS),
        "opportunities": rows,
        "baseline": BASELINE_ITER23,
        "missing_from_report": missing,
        "corpus_size": len(rows),
        "substitute_forbidden": True,
    }
    return payload


def load_corpus() -> dict[str, Any]:
    from m3_data_root import data_path

    path = data_path(CORPUS_FILE)
    if not path.exists():
        return {"build": BUILD, "opportunities": [], "stable_keys": list(SAME20_STABLE_KEYS)}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"build": BUILD, "opportunities": [], "stable_keys": list(SAME20_STABLE_KEYS)}


def save_corpus(payload: dict[str, Any]) -> None:
    from m3_data_root import data_path

    path = data_path(CORPUS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def resolve_same20(
    store: dict[str, Any],
    corpus: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Resolve frozen SAME-20 items against the live store (no substitution)."""
    corpus = corpus or load_corpus()
    by_sk = {
        str(r.get("stable_key") or ""): r
        for r in (corpus.get("opportunities") or [])
        if isinstance(r, dict)
    }
    store_by_sk: dict[str, dict[str, Any]] = {}
    store_by_cid: dict[str, dict[str, Any]] = {}
    for k, v in store.items():
        if not isinstance(v, dict):
            continue
        cid = str(v.get("canonical_opportunity_id") or k)
        sk = str(v.get("stable_key") or "")
        store_by_cid[cid] = v
        if sk:
            store_by_sk[sk] = {**v, "canonical_opportunity_id": cid}

    out: list[dict[str, Any]] = []
    for sk in SAME20_STABLE_KEYS:
        frozen = by_sk.get(sk) or {"stable_key": sk, "title": "", "buyer": None}
        sr = store_by_sk.get(sk)
        if not sr:
            # match by title fallback (still same key identity)
            for cand in store_by_sk.values():
                if str(cand.get("stable_key") or "") == sk:
                    sr = cand
                    break
        sr = sr or {}
        cid = str(sr.get("canonical_opportunity_id") or frozen.get("canonical_opportunity_id") or sk)
        out.append(
            {
                "canonical_opportunity_id": cid,
                "stable_key": sk,
                "title": frozen.get("title") or sr.get("title"),
                "buyer": frozen.get("buyer") or sr.get("buyer"),
                "deadline": frozen.get("deadline") or sr.get("deadline"),
                "authoritative_url": frozen.get("authoritative_url") or sr.get("authoritative_url"),
                "classification": sr.get("classification") or "PRODUCT",
                "recovery_cohort": "same_20",
                "_corpus_frozen": True,
                "_store_row": sr,
            }
        )
    return out
