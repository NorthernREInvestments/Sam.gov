"""Audit why Socrata harvest (~2145 raw → ~21 net-new) collapsed under L23 dedup."""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any

from application_clock import now_utc
from phase_l.l23_full_population_funnel import canonical_id_for

log = logging.getLogger("govtracker.socrata_dedup_audit_v1")

BUILD = "20261016-m3-socrata-dedup-audit-v1"


def _harvest_records() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Best-effort load last expansion / anonymous replacement harvest artifacts."""
    from m3_data_root import data_path

    meta: dict[str, Any] = {}
    # Prefer anonymous replacement report harvest summary + try to rebuild from store
    for name in (
        "m3_anonymous_replacement_discovery_last_report.json",
        "m3_discovery_expansion_last_harvest.json",
    ):
        p = data_path(name)
        if p.exists():
            try:
                meta[name] = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
    return [], meta


def audit_socrata_dedup_against_l23(*, limit: int = 5000) -> dict[str, Any]:
    """
    Reconstruct Socrata-like live sources from structured registry, fetch live rows,
    compute canonical IDs, and classify why they would (or would not) be net-new.
    """
    from discovery.http_client import PublicProcurementHttpClient, RequestBudget
    from discovery.live_fetchers import StructuredOpenDataLiveFetcher
    from discovery.structured_source_registry import all_structured_live_candidates
    from phase_l.l23_full_population_funnel import load_store

    store = load_store()
    existing_ids = set(store.keys()) if isinstance(store, dict) else set()

    # Also index by solicitation+buyer for cross-source explanation
    sol_buyer_index: dict[str, str] = {}
    for cid, rec in (store.items() if isinstance(store, dict) else []):
        if not isinstance(rec, dict):
            continue
        sol = str(rec.get("solicitation_number") or rec.get("solicitation_event_id") or "").strip().lower()
        buyer = str(rec.get("buyer") or rec.get("agency") or "").strip().lower()
        if sol and buyer:
            sol_buyer_index[f"{buyer}|{sol}"] = str(cid)

    client = PublicProcurementHttpClient(
        budget=RequestBudget(
            max_total_requests=800,
            max_requests_per_source=8,
            max_pages_per_source=5,
            max_records_per_source=2000,
            max_runtime_seconds=600.0,
            max_retries=0,
            min_interval_seconds=0.2,
            timeout_seconds=20.0,
        ),
        authorize_live=True,
        user_agent="M3SocrataDedupAudit/1.0",
    )
    fetcher = StructuredOpenDataLiveFetcher()
    candidates = [c for c in (all_structured_live_candidates() or []) if isinstance(c, dict)]
    # Prefer NYC/LA/Montgomery + any socrata
    preferred = [
        c
        for c in candidates
        if "socrata" in str(c.get("source_id") or "").lower()
        or str(c.get("platform_family") or "") == "Socrata"
    ][:40]

    reasons: Counter = Counter()
    raw_total = 0
    unique_in_harvest = 0
    would_be_new = 0
    false_positive_suspects: list[dict[str, Any]] = []
    seen_cids: set[str] = set()
    per_source: dict[str, Any] = {}
    samples_new: list[dict[str, Any]] = []
    samples_dup: list[dict[str, Any]] = []

    for src in preferred:
        sid = str(src.get("source_id") or "structured")
        url = src.get("list_url")
        if not url:
            continue
        try:
            result = fetcher.fetch_listing(
                client,
                list_url=url,
                source_id=sid,
                max_pages=3,
                pagination_exhaust=False,
                pagination_safety_max_pages=3,
            )
            opps = result.get("opportunities") or []
            raw_total += len(opps)
            src_new = 0
            src_dup = 0
            for opp in opps:
                d = opp.to_dict() if hasattr(opp, "to_dict") else dict(opp or {})
                d.setdefault("source_id", sid)
                d.setdefault("platform", "Socrata")
                d.setdefault("agency", d.get("agency") or d.get("buyer") or src.get("name"))
                cid = canonical_id_for(d)
                if cid in seen_cids:
                    reasons["exact_canonical_id_within_harvest"] += 1
                    src_dup += 1
                    continue
                seen_cids.add(cid)
                unique_in_harvest += 1
                sol = str(d.get("solicitation_number") or "").strip().lower()
                buyer = str(d.get("agency") or d.get("buyer") or "").strip().lower()
                sb = f"{buyer}|{sol}" if sol and buyer else ""

                if cid in existing_ids:
                    reasons["exact_canonical_id_match_l23"] += 1
                    src_dup += 1
                    if len(samples_dup) < 15:
                        samples_dup.append(
                            {
                                "source_id": sid,
                                "canonical_id": cid,
                                "title": (d.get("title") or "")[:100],
                                "sol": d.get("solicitation_number"),
                                "buyer": d.get("agency") or d.get("buyer"),
                                "reason": "exact_canonical_id_match_l23",
                            }
                        )
                    continue
                if sb and sb in sol_buyer_index:
                    reasons["solicitation_buyer_cross_source_match"] += 1
                    src_dup += 1
                    # Potential false positive if titles differ substantially
                    existing = store.get(sol_buyer_index[sb]) or {}
                    t1 = str(d.get("title") or "").lower()[:80]
                    t2 = str(existing.get("title") or "").lower()[:80]
                    if t1 and t2 and t1 != t2 and t1 not in t2 and t2 not in t1:
                        false_positive_suspects.append(
                            {
                                "new_title": t1,
                                "existing_title": t2,
                                "sol": sol,
                                "buyer": buyer,
                                "existing_id": sol_buyer_index[sb],
                                "new_would_be_id": cid,
                            }
                        )
                        reasons["false_positive_suspect_title_mismatch"] += 1
                    continue

                # Expired?
                dl = str(d.get("deadline") or d.get("response_deadline") or "")
                if dl and dl[:10] < now_utc().isoformat()[:10]:
                    reasons["expired_deadline"] += 1
                    # still could be "new" historically — count separately
                reasons["would_be_net_new"] += 1
                would_be_new += 1
                src_new += 1
                if len(samples_new) < 20:
                    samples_new.append(
                        {
                            "source_id": sid,
                            "canonical_id": cid,
                            "title": (d.get("title") or "")[:100],
                            "sol": d.get("solicitation_number"),
                            "buyer": d.get("agency") or d.get("buyer"),
                            "deadline": d.get("deadline") or d.get("response_deadline"),
                        }
                    )
            per_source[sid] = {
                "raw": len(opps),
                "would_be_new": src_new,
                "duplicate_or_existing": src_dup,
                "ok": len(opps) > 0,
            }
        except Exception as exc:
            per_source[sid] = {"ok": False, "error": type(exc).__name__}
            reasons["source_fetch_error"] += 1

    _, harvest_meta = _harvest_records()
    anon = harvest_meta.get("m3_anonymous_replacement_discovery_last_report.json") or {}
    harvest_summary = (anon.get("harvest") or {}) if isinstance(anon, dict) else {}

    report = {
        "kind": "SocrataDedupAudit",
        "build": BUILD,
        "at": now_utc().isoformat(),
        "prior_reported": {
            "raw_opportunities": harvest_summary.get("raw_opportunities"),
            "unique_records": harvest_summary.get("unique_records"),
            "canonical_merge_new": (harvest_summary.get("canonical_merge") or {}).get("new"),
            "explanation": (
                "Harvest unique_records uses detail_url/external_id within-run dedupe; "
                "L23 merge uses canonical_id_for (buyer+sol / title+buyer+deadline). "
                "Most Socrata rows already existed under BidNet or prior OpenGov/Socrata IDs."
            ),
        },
        "live_reaudit": {
            "sources_attempted": len(per_source),
            "raw_total": raw_total,
            "unique_canonical_in_harvest": unique_in_harvest,
            "would_be_net_new_vs_l23": would_be_new,
            "reason_counts": dict(reasons),
            "false_positive_suspects": false_positive_suspects[:25],
            "false_positive_suspect_count": len(false_positive_suspects),
            "per_source": per_source,
            "samples_new": samples_new,
            "samples_existing_dup": samples_dup,
        },
        "canonical_id_rules": [
            "notice_url (opp/opportunity-details)",
            "sol|buyer|solicitation_number",
            "sol_only|sol|platform",
            "title|buyer|title|deadline",
            "title_only|title|deadline|platform",
        ],
        "recommended_fix_only_if_false_positives_confirmed": (
            "Do not weaken global dedup. Only split when false_positive_suspects show "
            "different solicitations sharing sol+buyer incorrectly."
        ),
    }
    try:
        from m3_data_root import data_path

        path = data_path("m3_socrata_dedup_audit_last_report.json")
        path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        report["persisted_path"] = str(path)
    except Exception:
        log.exception("persist dedup audit failed")
    return report
