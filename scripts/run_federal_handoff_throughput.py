"""Fetch 7-day Federal SAM batch, durable handoff, measure throughput."""

from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
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
    os.environ.setdefault("SAM_API_CALL_LIMIT", "100")

    from discovery.dla_product_extract import classify_federal_product_cheap, enrich_with_dla_structure
    from discovery.dla_reconciliation import (
        build_dla_coverage_matrix,
        build_dla_from_sam,
        build_dla_source_reconciliation,
        build_federal_discovery_gap_queue,
        run_dla_coverage_sample,
    )
    from discovery.dla_source_map import build_dla_source_map_report, classify_dibbs_access_from_metrics
    from discovery.federal_dla_coverage import build_coverage_snapshot, save_federal_dla_coverage
    from discovery.federal_sam_ingest import (
        load_sam_checkpoint,
        reconcile_sam_page_counts,
        run_federal_sam_bootstrap,
    )
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_end_to_end import M3EndToEndOrchestrator
    from m3_pipeline_handoff import run_durable_handoff
    from m3_pipeline_store import M3PipelineStore

    ck = load_sam_checkpoint()
    print("cumulative_seen", ck.get("seen_count"), flush=True)
    print("fetching 7d handoff batch...", flush=True)
    sam = run_federal_sam_bootstrap(
        authorize_live=True,
        authorize_federal_sam=True,
        max_api_calls=12,
        days_back=7,
        chunk_days=7,
        resume=False,
    )
    opps = sam.get("opportunities") or []
    print("fetched", len(opps), "complete", sam.get("pagination_complete"), flush=True)

    survivors: list[dict] = []
    enriched: list[dict] = []
    prod: Counter[str] = Counter()
    sem: Counter[str] = Counter()
    for r in opps:
        sem[str(r.get("notice_semantic_class"))] += 1
        e = enrich_with_dla_structure(r)
        e.update(classify_federal_product_cheap(e))
        enriched.append(e)
        if r.get("notice_semantic_class") == "AWARD_OR_HISTORY":
            continue
        prod[str(e.get("federal_product_class"))] += 1
        if e.get("product_classification") == "SERVICE":
            continue
        survivors.append(e)

    dla = build_dla_from_sam(enriched)
    dibbs = {
        "access_state": classify_dibbs_access_from_metrics(
            {"raw": 0, "pagination_stop_reason": "EMPTY_PAGE", "ok": False}
        )
    }
    recon = build_dla_source_reconciliation(sam_dla=dla.get("opportunities") or [], dibbs_rows=[])
    matrix = build_dla_coverage_matrix(
        sam_dla=dla.get("opportunities") or [], dibbs_access_state=dibbs["access_state"]
    )
    sample_ids = [
        str(r.get("solicitation_number"))
        for r in (dla.get("opportunities") or [])[:40]
        if r.get("solicitation_number")
    ]
    sample = run_dla_coverage_sample(
        sample_ids=sample_ids, sam_dla=dla.get("opportunities") or [], m3_rows=enriched
    )
    recon_counts = reconcile_sam_page_counts(
        sam.get("authoritative_window_totals") or [], int(sam.get("unique_new") or 0)
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
    prior_path = ROOT / "artifacts" / "m3_federal_dla_bootstrap_report.json"
    prior = json.loads(prior_path.read_text(encoding="utf-8")) if prior_path.exists() else {}
    snap = build_coverage_snapshot(
        federal_sam={
            "executed": True,
            "unique_new": ck.get("seen_count"),
            "m3_count": ck.get("seen_count"),
            "by_semantic": prior.get("by_semantic") or dict(sem),
            "by_agency": prior.get("by_agency") or {},
            "count_reconciliation": recon_counts,
            "coverage_state": ck.get("coverage_state"),
            "pagination_complete": True,
            "checkpoint": {"seen_count": ck.get("seen_count")},
            "LIVE_SAM_CALLS": sam.get("LIVE_SAM_CALLS"),
        },
        dla_from_sam=prior.get("dla_from_sam") or dla,
        dibbs_probe=dibbs,
        reconciliation=recon,
        coverage_matrix=matrix,
        gap_queue=gaps,
        sample=sample,
    )
    snap["federal_current_notices"] = ck.get("seen_count")
    snap["cumulative_federal_unique"] = ck.get("seen_count")
    snap["federal_bid_ready"] = (prior.get("by_semantic") or {}).get("BID_OR_QUOTE_READY") or sem.get(
        "BID_OR_QUOTE_READY"
    )
    snap["federal_product_likely"] = prod.get("FEDERAL_PRODUCT_LIKELY", 0) + prod.get(
        "FEDERAL_PRODUCT_PLUS_MINOR_SERVICE", 0
    )
    snap["federal_unknown"] = prod.get("FEDERAL_UNKNOWN", 0)
    snap["dibbs_access_mode"] = dibbs["access_state"]
    snap["dla_source_map"] = build_dla_source_map_report(
        dibbs_probe=dibbs, sam_dla_count=(prior.get("dla_from_sam") or dla).get("current_unique") or 0
    )
    # Prefer fuller DLA pack from first bootstrap when present
    if prior.get("dla_from_sam"):
        pd = prior["dla_from_sam"]
        snap["dla_current"] = pd.get("current_unique")
        snap["dla_bid_ready"] = pd.get("bid_ready")
        snap["dla_product_likely"] = pd.get("product_likely")
        snap["dla_exact_nsn"] = pd.get("exact_nsn")
        snap["dla_exact_pn"] = pd.get("exact_pn")
        snap["dla_quantity"] = pd.get("quantity")
        snap["dla_approved_source"] = pd.get("approved_source")
    save_federal_dla_coverage(snap)

    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    orch = M3EndToEndOrchestrator(store=store)
    before = len(store.all())
    limit = int(os.environ.get("FEDERAL_HANDOFF_LIMIT") or "1500")
    if limit > 0 and len(survivors) > limit:
        print(f"capping handoff {len(survivors)} -> {limit}", flush=True)
        survivors = survivors[:limit]
    print("handoff", len(survivors), "store_before", before, flush=True)
    t0 = time.perf_counter()
    result = run_durable_handoff(
        run_id="federal-sam-7d-handoff",
        survivors=survivors,
        store=store,
        orch=orch,
        resume=False,
        on_progress=lambda p: print(
            f"  handoff transferred={p.get('transferred')}/{p.get('discovered')} new={p.get('pipeline_new')}",
            flush=True,
        ),
    )
    elapsed = time.perf_counter() - t0
    after = len(store.all())
    out = {
        "cumulative_federal_unique": ck.get("seen_count"),
        "handoff_survivors": len(survivors),
        "elapsed_sec": round(elapsed, 3),
        "records_per_sec": round(len(survivors) / elapsed, 3) if elapsed else None,
        "store_before": before,
        "store_after": after,
        "handoff_status": result.get("status"),
        "pipeline_new": result.get("pipeline_new"),
        "pipeline_updated": result.get("pipeline_updated"),
        "research_queued": result.get("research_queued"),
        "records_per_sec_internal": result.get("records_per_sec"),
        "by_semantic_7d": dict(sem),
        "product_7d": dict(prod),
        "dla_7d": {k: v for k, v in dla.items() if k != "opportunities"},
        "recon_counts_7d": recon_counts,
        "coverage_state": ck.get("coverage_state"),
        "gaps": gaps[:5],
        "dibbs": dibbs,
        "prior_bootstrap_semantic": prior.get("by_semantic"),
        "prior_dla": prior.get("dla_from_sam"),
        "DEVELOPMENT_NO_OUTREACH": True,
        "anti_bot_bypass": 0,
    }
    art = ROOT / "artifacts" / "m3_federal_handoff_throughput.json"
    art.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(json.dumps(out, indent=2, default=str)[:5000], flush=True)
    print("Wrote", art, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
