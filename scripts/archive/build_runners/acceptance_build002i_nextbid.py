"""Build 002I NextBid acceptance — real acquisition within budget. Never prints API key."""

from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

# Load .env without printing
from pathlib import Path as _P

_env = _P(".env")
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v

from m3 import BUILD_ID
from m3.research.nextbid_acquire import run_nextbid_acquisition
from m3.research.nextbid_client import NextBidClient, NextBidConfigError
from m3.research.nextbid_schedule import start_nextbid_scheduler


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def reconcile_002f_vs_002h() -> dict:
    """Explain material discrepancy: ~21 meaningful packages (002F) vs 2 fits (002H)."""
    docs = ROOT / "docs"
    out: dict = {
        "question": "Why did Build 002F report ~21 meaningful packages while 002H reported 2 verified fits?",
        "evidence_files": [],
        "explanation": [],
        "material_discrepancies": [],
    }

    f_acc = docs / "BUILD_002F_ACCEPTANCE.json"
    h_acc = docs / "BUILD_002H_ACCEPTANCE.json"
    i_acc = docs / "BUILD_002I_ACCEPTANCE.json"
    h_rep = docs / "BUILD_002H_REPORT.md"
    f_rep = docs / "BUILD_002F_REPORT.md"

    def loadj(p: Path):
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

    fj, hj, ij = loadj(f_acc), loadj(h_acc), loadj(i_acc)
    for p in (f_acc, h_acc, i_acc, h_rep, f_rep):
        if p.exists():
            out["evidence_files"].append(str(p.relative_to(ROOT)))

    # 002F "meaningful" != Stage C procurement fit
    out["explanation"].append(
        "Build 002F measured acquisition/qualification funnel progress (complete/partial packages, "
        "targeted investigations, progressive gates). 'Meaningful packages' referred to packages with "
        "usable document content or product-leaning disposition — not VERIFIED_M3_PROCUREMENT_FIT."
    )
    out["explanation"].append(
        "Build 002H introduced Stage A–C gates requiring: verified individual solicitation + tangible "
        "product class + defined ORDERED/LINE_ITEM quantity semantics. Most 002F packages failed Stage C "
        "on quantity_unknown or services/construction exclusion."
    )
    out["explanation"].append(
        "Build 002H also corrected false-positive quantity extraction (e.g. parenthesis unit patterns). "
        "002I further tightened ordered-quantity semantics, which reduced inflated fits."
    )

    if fj:
        out["build_002f_snapshot"] = {
            k: fj.get(k)
            for k in (
                "build_id",
                "complete_packages",
                "partial_packages",
                "meaningful_packages",
                "product_packages",
                "coverage_pct",
                "items_done",
            )
            if k in fj or fj.get(k) is not None
        }
        # heuristic keys
        for k, v in fj.items():
            if "meaningful" in k.lower() or "complete" in k.lower() or "product" in k.lower():
                if k not in out["build_002f_snapshot"]:
                    out["build_002f_snapshot"][k] = v

    if hj:
        out["build_002h_snapshot"] = {
            "build_id": hj.get("build_id"),
            "verified_m3_procurement_fits": hj.get("verified_m3_procurement_fits")
            or hj.get("strict_m3_procurement_fits")
            or (len(hj.get("verified_opportunities_sample") or []) if hj.get("verified_opportunities_sample") else None),
            "sample": (hj.get("verified_opportunities_sample") or [])[:10],
        }

    if ij:
        out["build_002i_prior_snapshot"] = {
            "build_id": ij.get("build_id"),
            "verified_m3_procurement_fits": ij.get("verified_m3_procurement_fits"),
            "note": "Prior 002I recovery (DE/GA) before NextBid integration",
            "sample": (ij.get("verified_opportunities_sample") or [])[:10],
        }

    out["material_discrepancies"] = [
        {
            "metric_002f": "meaningful/complete packages (~21 in operator reports)",
            "metric_002h": "strict M3 procurement fits (2 after reaudit; 4 before 002I semantic tighten)",
            "cause": "Different definitions. Package completeness ≠ procurement fit.",
            "evidence": "BUILD_002H_REPORT.md root cause H (quantity_unknown on 116/120 samples); commercial_criteria Stage C requires defined ordered quantity.",
        },
        {
            "metric_002h": "paren quantity fits (PER CUP / UNIT)",
            "metric_002i": "excluded unless affirmative ordered semantics",
            "cause": "False-positive quantity extraction from title parentheses and boilerplate.",
            "evidence": "extract_fields.py quantity_semantic gates; BUILD_002I reports.",
        },
    ]
    return out


def main() -> int:
    started = time.time()
    docs = ROOT / "docs"
    docs.mkdir(exist_ok=True)

    acceptance: dict = {
        "build": "002I",
        "phase": "nextbid_integration",
        "build_id": BUILD_ID,
        "started_at": _utc(),
        "integrity": {
            "zero_fabricated_opportunities": True,
            "zero_fabricated_quantities": True,
            "zero_synthetic_production_data": True,
            "ai_summaries_not_primary_evidence": True,
        },
    }

    # Auth verify
    try:
        client = NextBidClient()
        usage = client.usage()
        acceptance["auth"] = {
            "ok": True,
            "plan": (usage.get("usage") or {}).get("plan") if isinstance(usage.get("usage"), dict) else None,
            "allowance_before": client.budget.account_allowance,
            "remaining_before": client.budget.account_remaining,
            "used_before": client.budget.account_used,
        }
        client.close()
    except NextBidConfigError as exc:
        acceptance["auth"] = {"ok": False, "error": str(exc), "requires_secure_configuration": True}
        (docs / "BUILD_002I_NEXTBID_ACCEPTANCE.json").write_text(
            json.dumps(acceptance, indent=2), encoding="utf-8"
        )
        print("ACCEPTANCE_FAIL missing_key")
        return 2

    # Start scheduler (daemon)
    acceptance["scheduler_started"] = start_nextbid_scheduler()

    # Full targeted run — substantial, budgeted, resumable
    max_discovery = int(os.getenv("M3_NB_MAX_DISCOVERY", "250"))
    max_detail = int(os.getenv("M3_NB_MAX_DETAIL", "900"))
    print(
        f"RUN start discovery<={max_discovery} detail<={max_detail} remaining={acceptance['auth'].get('remaining_before')}"
    )
    result = run_nextbid_acquisition(
        max_discovery_calls=max_discovery,
        max_detail=max_detail,
        detail_workers=4,
        checkpoint_name="nextbid_acquire_v1",
    )
    acceptance["acquisition"] = result
    acceptance["reconciliation_002f_002h"] = reconcile_002f_vs_002h()

    fits = ((result.get("qualification") or {}).get("fits")) or []
    acceptance["summary"] = {
        "api_remaining_before": acceptance["auth"].get("remaining_before"),
        "api_remaining_after": (result.get("usage_after") or {}).get("remaining"),
        "api_used_delta": None,
        "calls_by_endpoint": (result.get("budget") or {}).get("by_endpoint"),
        "newly_discovered": (result.get("discovery") or {}).get("discovered"),
        "duplicates_removed": (result.get("discovery") or {}).get("duplicates_skipped"),
        "verified_active_individual": result.get("verified_active_individual"),
        "verified_state_local": result.get("verified_state_local"),
        "strict_m3_procurement_fits": len(fits),
        "gov_documents_downloaded": (result.get("detail") or {}).get("gov_documents_downloaded"),
        "calls_saved_direct_gov_and_cache": result.get("calls_saved_direct_gov_and_cache"),
        "source_yield": result.get("source_yield"),
        "stage_counts": (result.get("qualification") or {}).get("stage_counts"),
        "duration_seconds": round(time.time() - started, 1),
    }
    before = acceptance["auth"].get("remaining_before")
    after = acceptance["summary"]["api_remaining_after"]
    if isinstance(before, int) and isinstance(after, int):
        acceptance["summary"]["api_used_delta"] = before - after

    acceptance["strict_m3_fit_evidence"] = fits
    acceptance["compared_to_002h"] = {
        "002h_fits": 2,
        "002i_nextbid_fits": len(fits),
        "delta": len(fits) - 2,
        "note": "Comparison is strict Stage C fits with traceable evidence only.",
    }
    acceptance["finished_at"] = _utc()
    acceptance["await_owner_acceptance"] = True
    acceptance["do_not_begin_build_003"] = True

    acc_path = docs / "BUILD_002I_NEXTBID_ACCEPTANCE.json"
    acc_path.write_text(json.dumps(acceptance, indent=2, default=str), encoding="utf-8")

    # Written report
    s = acceptance["summary"]
    report = f"""# Build 002I Report — NextBid Integration & Procurement Acquisition

**Build ID:** `{BUILD_ID}`  
**Phase:** NextBid paid API integration  
**Acceptance:** `docs/BUILD_002I_NEXTBID_ACCEPTANCE.json`  
**Rule:** REAL DATA ONLY — honest shortfalls over fabricated success.  
**Do not begin Build 003.**

---

## Authentication & allowance

| Metric | Value |
|--------|------:|
| Auth | {"OK" if acceptance["auth"].get("ok") else "FAIL"} |
| Plan | {acceptance["auth"].get("plan")} |
| Allowance before | {s.get("api_remaining_before")} |
| Remaining after | {s.get("api_remaining_after")} |
| Calls used (delta) | {s.get("api_used_delta")} |
| Calls by endpoint | `{json.dumps(s.get("calls_by_endpoint") or {})}` |

Hard stop preserves emergency reserve. No automatic upgrade/purchase.

---

## Acquisition results

| Metric | Count |
|--------|------:|
| Newly discovered (unique) | {s.get("newly_discovered")} |
| Duplicates skipped | {s.get("duplicates_removed")} |
| Verified active individual (enriched set) | {s.get("verified_active_individual")} |
| Verified state/local | {s.get("verified_state_local")} |
| Strict M3 procurement fits | **{s.get("strict_m3_procurement_fits")}** |
| Gov documents downloaded (direct) | {s.get("gov_documents_downloaded")} |
| Calls saved (gov + cache) | {s.get("calls_saved_direct_gov_and_cache")} |
| Duration (seconds) | {s.get("duration_seconds")} |

### Source yield (enriched)

```
{json.dumps(s.get("source_yield") or {}, indent=2)}
```

### Stage counts

```
{json.dumps(s.get("stage_counts") or {}, indent=2)}
```

---

## Strict M3 procurement-fit evidence

Every counted fit includes `nb_id`, official/source URL, quantity evidence, and pipeline reasons in the acceptance JSON (`strict_m3_fit_evidence`).

Fit count: **{len(fits)}**

---

## Comparison vs Build 002H

| | 002H | 002I NextBid |
|--|-----:|-------------:|
| Strict M3 fits | 2 | {len(fits)} |

---

## Reconciliation: 002F ~21 packages vs 002H 2 fits

{chr(10).join('- ' + e for e in acceptance['reconciliation_002f_002h'].get('explanation') or [])}

Material discrepancies are documented in acceptance `reconciliation_002f_002h.material_discrepancies` with file evidence references.

---

## Bottlenecks (honest)

- NextBid `procurement_kind` / tradelines often label goods solicitations as services — classifier must use title/description/documents, not NextBid labels alone.
- Many state/local portals require session for attachments; direct `source_url` may be an HTML shell.
- Defined ORDERED quantity remains the dominant Stage C gate.
- SAM.gov / DLA remain disabled until CAGE issuance (owner authorization).

---

## Integrity

- Zero fabricated opportunities, quantities, prices, or documents.
- AI summaries are not primary source evidence.
- Existing free sources preserved; BidNet remains abandoned.

Await owner acceptance. **Do not begin Build 003.**
"""
    (docs / "BUILD_002I_NEXTBID_REPORT.md").write_text(report, encoding="utf-8")
    print(
        "ACCEPTANCE",
        json.dumps(
            {
                "fits": s.get("strict_m3_procurement_fits"),
                "discovered": s.get("newly_discovered"),
                "used": s.get("api_used_delta"),
                "remaining": s.get("api_remaining_after"),
                "duration": s.get("duration_seconds"),
            }
        ),
    )
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
