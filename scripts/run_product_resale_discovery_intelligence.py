"""Build + persist product-resale source intelligence + live validation (no Iowa primary)."""

from __future__ import annotations

import json
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
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


import os


def main() -> int:
    _load_dotenv()
    from product_resale_live_validation import run_live_validation_from_store
    from product_resale_source_intelligence import (
        build_persisted_payload,
        save_source_intelligence,
        source_coverage_audit,
        source_roi_ranking,
        diagnose_discovery_gaps,
    )

    # Prefer prior Federal enrichment campaign metrics if present
    enrichment_metrics = {}
    camp = ROOT / "artifacts" / "m3_federal_dla_product_intelligence_campaign.json"
    if camp.exists():
        try:
            data = json.loads(camp.read_text(encoding="utf-8"))
            enrichment_metrics = (data.get("campaign") or {}).get("metrics") or {}
        except Exception:
            pass

    print("=== Live validation (pipeline store) ===", flush=True)
    live = run_live_validation_from_store(top_n=20)
    print(
        json.dumps(
            {
                "raw_scanned": live["raw_scanned"],
                "product_candidates": live["product_candidates"],
                "top20_count": len(live["top20"]),
                "iowa_excluded": live["iowa_excluded"],
            },
            indent=2,
        ),
        flush=True,
    )
    for i, c in enumerate(live["top20"][:10], 1):
        print(f"{i}. [{c['score']}] {c['opportunity'][:90]} | {c['product']} | next={c['next_research_action'][:70]}", flush=True)

    gaps = diagnose_discovery_gaps(enrichment_metrics=enrichment_metrics)
    print("=== Biggest gap ===", flush=True)
    print(json.dumps(gaps["biggest_remaining_capability_gap"], indent=2), flush=True)

    payload = build_persisted_payload(enrichment_metrics=enrichment_metrics, live_validation=live)
    saved = save_source_intelligence(payload)
    out = {
        "coverage_counts": source_coverage_audit()["counts"],
        "roi_tiers": source_roi_ranking()["by_tier"],
        "live": live,
        "gaps": gaps,
        "durable_saved": saved.get("durable_saved"),
        "artifact_path": saved.get("artifact_path"),
        "next_state": "PRODUCT_RESALE_DISCOVERY_INTELLIGENCE_EXPANDED",
    }
    art = ROOT / "artifacts" / "m3_product_resale_discovery_intelligence_report.json"
    art.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print("Wrote", art, flush=True)
    return 0 if live.get("raw_target_met") and live.get("top20") else 1


if __name__ == "__main__":
    raise SystemExit(main())
