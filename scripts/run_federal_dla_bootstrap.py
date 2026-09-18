"""ONE-SHOT Federal SAM bootstrap + DIBBS probe + DLA reconciliation (bounded).

Does NOT run full national BidNet discovery.
Does NOT bypass bot/auth.
Does NOT run full pytest.

Usage (from govtracker/):
  set SAM_FEDERAL_DISCOVERY_ENABLED=1
  set SAM_API_CALL_LIMIT=100
  python -m scripts.run_federal_dla_bootstrap
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_dotenv() -> None:
    for p in (ROOT / ".env", Path.cwd() / ".env"):
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


def main() -> int:
    _load_dotenv()
    os.environ.setdefault("SAM_FEDERAL_DISCOVERY_ENABLED", "1")
    if not os.environ.get("SAM_API_CALL_LIMIT"):
        os.environ["SAM_API_CALL_LIMIT"] = "100"
    days_back = int(os.environ.get("SAM_FEDERAL_DAYS_BACK") or "30")
    chunk_days = int(os.environ.get("SAM_FEDERAL_CHUNK_DAYS") or "7")
    max_calls = os.environ.get("SAM_FEDERAL_MAX_CALLS")
    max_api_calls = int(max_calls) if max_calls else None

    from discovery.dla_product_extract import classify_federal_product_cheap, enrich_with_dla_structure
    from discovery.dla_reconciliation import (
        build_dla_coverage_matrix,
        build_dla_from_sam,
        build_dla_source_reconciliation,
        build_federal_discovery_gap_queue,
        run_dla_coverage_sample,
    )
    from discovery.dla_source_map import build_dla_source_map_report, probe_dibbs_access
    from discovery.federal_dla_coverage import build_coverage_snapshot, save_federal_dla_coverage
    from discovery.federal_sam_ingest import reconcile_sam_page_counts, run_federal_sam_bootstrap

    print("=== DIBBS bounded probe (1) ===", flush=True)
    dibbs = probe_dibbs_access(authorize_live=True)
    print(json.dumps({k: v for k, v in dibbs.items() if k != "validation"}, default=str), flush=True)

    print("=== Federal SAM bootstrap ===", flush=True)

    def _prog(p: dict) -> None:
        print(
            f"  window={p.get('window')} page={p.get('page')} calls={p.get('calls')} "
            f"unique={p.get('unique')} batch={p.get('batch')} total={p.get('window_total')}",
            flush=True,
        )

    sam = run_federal_sam_bootstrap(
        authorize_live=True,
        authorize_federal_sam=True,
        max_api_calls=max_api_calls,
        days_back=days_back,
        chunk_days=chunk_days,
        resume=True,
        on_progress=_prog,
    )
    opps = sam.get("opportunities") or []
    enriched = []
    from collections import Counter

    prod = Counter()
    for r in opps:
        e = enrich_with_dla_structure(r)
        screen = classify_federal_product_cheap(e)
        e.update(screen)
        prod[str(screen.get("federal_product_class") or "UNKNOWN")] += 1
        enriched.append(e)

    recon_counts = reconcile_sam_page_counts(
        sam.get("authoritative_window_totals") or [], int(sam.get("unique_new") or 0)
    )
    dla_pack = build_dla_from_sam(enriched)
    recon = build_dla_source_reconciliation(sam_dla=dla_pack.get("opportunities") or [], dibbs_rows=[])
    matrix = build_dla_coverage_matrix(
        sam_dla=dla_pack.get("opportunities") or [],
        dibbs_access_state=dibbs.get("access_state"),
    )
    sample_ids = []
    for r in (dla_pack.get("opportunities") or [])[:40]:
        sol = r.get("solicitation_number") or r.get("external_id")
        if sol:
            sample_ids.append(str(sol))
    sample = run_dla_coverage_sample(
        sample_ids=sample_ids,
        sam_dla=dla_pack.get("opportunities") or [],
        m3_rows=enriched,
        dibbs_accessible=False,
    )
    gaps = build_federal_discovery_gap_queue(
        sam_result={
            **{k: v for k, v in sam.items() if k != "opportunities"},
            "count_reconciliation": recon_counts,
        },
        dibbs_probe=dibbs,
        recon=recon,
        matrix=matrix,
    )
    snap = build_coverage_snapshot(
        federal_sam={
            **{k: v for k, v in sam.items() if k != "opportunities"},
            "count_reconciliation": recon_counts,
            "federal_product_counts": dict(prod),
        },
        dla_from_sam=dla_pack,
        dibbs_probe=dibbs,
        reconciliation=recon,
        coverage_matrix=matrix,
        gap_queue=gaps,
        sample=sample,
    )
    snap["federal_product_likely"] = prod.get("FEDERAL_PRODUCT_LIKELY", 0) + prod.get(
        "FEDERAL_PRODUCT_PLUS_MINOR_SERVICE", 0
    )
    snap["federal_unknown"] = prod.get("FEDERAL_UNKNOWN", 0) + prod.get("UNKNOWN", 0)
    save_federal_dla_coverage(snap)

    out = {
        "dibbs": dibbs,
        "sam_executed": sam.get("executed"),
        "sam_error": sam.get("error"),
        "LIVE_SAM_CALLS": sam.get("LIVE_SAM_CALLS"),
        "unique_new": sam.get("unique_new"),
        "pagination_complete": sam.get("pagination_complete"),
        "coverage_state": sam.get("coverage_state"),
        "by_semantic": sam.get("by_semantic"),
        "by_agency": sam.get("by_agency"),
        "count_reconciliation": recon_counts,
        "federal_product_counts": dict(prod),
        "dla_from_sam": {k: v for k, v in dla_pack.items() if k != "opportunities"},
        "reconciliation": recon,
        "coverage_matrix_top": matrix[:15],
        "sample": sample,
        "gap_queue": gaps[:10],
        "dla_source_map": build_dla_source_map_report(
            dibbs_probe=dibbs, sam_dla_count=dla_pack.get("current_unique") or 0
        ),
        "anti_bot_bypass": 0,
        "DEVELOPMENT_NO_OUTREACH": True,
    }
    art = ROOT / "artifacts" / "m3_federal_dla_bootstrap_report.json"
    art.parent.mkdir(parents=True, exist_ok=True)
    art.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print("=== SUMMARY ===", flush=True)
    print(json.dumps(out, indent=2, default=str)[:8000], flush=True)
    print(f"Wrote {art}", flush=True)
    return 0 if sam.get("executed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
