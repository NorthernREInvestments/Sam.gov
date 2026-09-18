"""ONE controlled Federal/DLA enrichment campaign + handoff throughput check.

Uses pipeline-store Federal/DLA rows when SAM search budget is exhausted.
Description recovery via noticedesc does NOT require search quota.
Does NOT bypass DIBBS/auth. Does NOT run full pytest.
"""

from __future__ import annotations

import json
import os
import sys
import time
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


def _load_sample_from_store(limit: int) -> list[dict]:
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_pipeline_store import M3PipelineStore

    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    rows = [r for r in store._rows.values() if isinstance(r, dict)]
    dla = [
        r
        for r in rows
        if r.get("is_dla")
        or str(r.get("solicitation_number") or "").upper().startswith(("SPE", "SPR", "SPM"))
        or "dla" in str(r.get("agency") or "").lower()
    ]
    # Prefer bid-ready / open
    dla.sort(
        key=lambda r: (
            0 if (r.get("bid_quote_ready") or r.get("notice_semantic_class") == "BID_OR_QUOTE_READY") else 1,
            0 if r.get("is_dla") else 1,
            str(r.get("deadline") or "9999"),
        )
    )
    out = []
    for r in dla:
        # Ensure notice_id present for noticedesc synthesis
        if not r.get("notice_id") and r.get("external_id"):
            r = dict(r)
            r["notice_id"] = r["external_id"]
        out.append(r)
        if len(out) >= limit * 3:  # extra pool for selector
            break
    if len(out) < limit:
        fed = [
            r
            for r in rows
            if str(r.get("jurisdiction") or "").upper() == "FEDERAL"
            or "sam" in str(r.get("source_id") or "").lower()
        ]
        for r in fed:
            if r in out:
                continue
            if not r.get("notice_id") and r.get("external_id"):
                r = dict(r)
                r["notice_id"] = r["external_id"]
            out.append(r)
            if len(out) >= limit * 3:
                break
    return out


def main() -> int:
    _load_dotenv()
    os.environ.setdefault("SAM_FEDERAL_DISCOVERY_ENABLED", "1")
    limit = int(os.environ.get("FEDERAL_ENRICH_LIMIT") or "40")

    from discovery.federal_dla_coverage import load_federal_dla_coverage, save_federal_dla_coverage
    from discovery.federal_dla_enrichment import run_federal_dla_enrichment_campaign
    from discovery.federal_sam_ingest import run_federal_sam_bootstrap
    from m3_end_to_end import M3EndToEndOrchestrator
    from m3_pipeline_handoff import run_durable_handoff
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db

    print("=== SAM bounded pull for enrichment sample ===", flush=True)
    sam = run_federal_sam_bootstrap(
        authorize_live=True,
        authorize_federal_sam=True,
        max_api_calls=5,
        days_back=7,
        chunk_days=7,
        resume=False,
    )
    opps = sam.get("opportunities") or []
    sample_source = "sam_bootstrap"
    if not opps:
        print(
            f"SAM bootstrap empty error={sam.get('error')} — falling back to pipeline store",
            flush=True,
        )
        opps = _load_sample_from_store(limit)
        sample_source = "pipeline_store"
    dlaish = [o for o in opps if o.get("is_dla")] or opps
    print(
        f"source={sample_source} fetched={len(opps)} dlaish={len(dlaish)} complete={sam.get('pagination_complete')}",
        flush=True,
    )

    print("=== Enrichment campaign ===", flush=True)
    camp = run_federal_dla_enrichment_campaign(
        opps,
        authorize_live=True,
        limit=limit,
        fetch_documents=True,
        max_document_fetches_per_opp=1,
        prefer_dla=True,
    )
    print(json.dumps({"metrics": camp["metrics"], "rates": camp["rates"]}, indent=2), flush=True)
    print("evidence_chains:", json.dumps(camp.get("evidence_chains") or [], indent=2, default=str)[:4000], flush=True)

    cov = load_federal_dla_coverage()
    cov["enrichment_campaign"] = {
        "metrics": camp["metrics"],
        "rates": camp["rates"],
        "evidence_chains": camp.get("evidence_chains"),
        "sample_size": camp["metrics"]["sample_size"],
        "sample_source": sample_source,
    }
    cov["descriptions_recovered"] = camp["metrics"]["descriptions_recovered"]
    cov["packages_discovered"] = camp["metrics"]["packages_discovered"]
    cov["packages_recovered"] = camp["metrics"]["packages_recovered"]
    cov["commercial_research_ready"] = camp["metrics"]["commercial_research_ready"]
    cov["historical_research_ready"] = camp["metrics"]["historical_research_ready"]
    cov["dla_exact_nsn_campaign"] = camp["metrics"]["exact_nsn"]
    cov["dla_exact_pn_campaign"] = camp["metrics"]["exact_pn"]
    cov["dla_quantity_campaign"] = camp["metrics"]["quantity"]
    cov["dla_approved_source_campaign"] = camp["metrics"]["approved_source"]
    cov["dibbs_access_mode"] = cov.get("dibbs_access_mode") or "BOT_BLOCKED_AUTOMATION"
    cov["dla_coverage_state"] = cov.get("dla_coverage_state") or "SAM_RECONCILED"
    save_federal_dla_coverage(cov)

    survivors = []
    for r in camp.get("opportunities") or []:
        if r.get("notice_semantic_class") == "AWARD_OR_HISTORY":
            continue
        survivors.append(r)
    handoff_n = min(len(survivors), int(os.environ.get("FEDERAL_HANDOFF_LIMIT") or "100"))
    survivors = survivors[:handoff_n]
    store = M3PipelineStore()
    restore_pipeline_store_from_db(store)
    orch = M3EndToEndOrchestrator(store=store)
    before = len(store._rows)
    t0 = time.perf_counter()
    result = run_durable_handoff(
        run_id="federal-enrich-handoff",
        survivors=survivors,
        store=store,
        orch=orch,
        resume=False,
    )
    elapsed = time.perf_counter() - t0
    rps = len(survivors) / elapsed if elapsed and survivors else None
    handoff_out = {
        "survivors": len(survivors),
        "elapsed_sec": round(elapsed, 3),
        "records_per_sec": round(rps, 3) if rps else None,
        "store_before": before,
        "store_after": len(store._rows),
        "status": result.get("status"),
        "pipeline_new": result.get("pipeline_new"),
        "records_per_sec_internal": result.get("records_per_sec"),
    }
    print("=== Handoff ===", flush=True)
    print(json.dumps(handoff_out, indent=2), flush=True)

    print("=== 5K synthetic handoff fixture ===", flush=True)
    import tempfile

    tmp = Path(tempfile.mkdtemp()) / "pipe.json"
    fix_store = M3PipelineStore(path=tmp, durable=False)
    fix_orch = M3EndToEndOrchestrator(store=fix_store)
    synth = [
        {
            "external_id": f"enrich-scale-{i}",
            "source_id": "fed_sam_contract_opportunities",
            "title": f"Scale fixture {i}",
            "solicitation_number": f"SCALE-{i}",
            "status": "OPEN",
            "jurisdiction": "FEDERAL",
            "product_classification": "UNKNOWN",
        }
        for i in range(5000)
    ]
    t1 = time.perf_counter()
    fix_res = run_durable_handoff(
        run_id="enrich-scale-5k",
        survivors=synth,
        store=fix_store,
        orch=fix_orch,
        resume=False,
    )
    e5 = time.perf_counter() - t1
    scale5 = {
        "n": 5000,
        "elapsed_sec": round(e5, 3),
        "records_per_sec": round(5000 / e5, 3) if e5 else None,
        "status": fix_res.get("status"),
        "store_count": len(fix_store._rows),
    }
    print(json.dumps(scale5, indent=2), flush=True)

    # 25k file-only upsert scale (timing durable path without remote merge)
    print("=== 25K file-primary save fixture ===", flush=True)
    tmp25 = Path(tempfile.mkdtemp()) / "pipe25.json"
    s25 = M3PipelineStore(path=tmp25, durable=False)
    for i in range(25000):
        cid = f"scale25-{i}"
        s25._rows[cid] = {
            "canonical_id": cid,
            "external_id": cid,
            "source_id": "fed_sam_contract_opportunities",
            "title": f"Scale25 {i}",
            "status": "OPEN",
        }
    t25 = time.perf_counter()
    s25.save(durable_write=True, skip_remote_merge=True)
    e25 = time.perf_counter() - t25
    scale25 = {
        "n": 25000,
        "elapsed_sec": round(e25, 3),
        "records_per_sec": round(25000 / e25, 3) if e25 else None,
        "path": str(tmp25),
    }
    print(json.dumps(scale25, indent=2), flush=True)

    out = {
        "sample_source": sample_source,
        "sam_error": sam.get("error"),
        "sam_unique_new": sam.get("unique_new"),
        "campaign": {k: camp[k] for k in ("metrics", "rates", "evidence_chains") if k in camp},
        "handoff": handoff_out,
        "scale_5k": scale5,
        "scale_25k_save": scale25,
        "anti_bot_bypass": 0,
        "DEVELOPMENT_NO_OUTREACH": True,
        "dibbs_state": "BOT_BLOCKED_AUTOMATION",
        "dla_coverage_state": "SAM_RECONCILED",
        "pipeline_before_rps": 0.82,
    }
    art = ROOT / "artifacts" / "m3_federal_dla_product_intelligence_campaign.json"
    art.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print("Wrote", art, flush=True)
    return 0 if camp.get("executed") and camp["metrics"]["sample_size"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
