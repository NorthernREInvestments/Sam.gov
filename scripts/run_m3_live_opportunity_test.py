"""M3 LIVE PROCUREMENT OPPORTUNITY TEST

Part A: existing source health (eligible + priority registry)
Part B: fresh product-resale discovery with novelty vs DB inventory

Uses existing discovery/pipeline gates. No outreach. No invented commercial data.
"""

from __future__ import annotations

import json
import sys
import traceback
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from application_clock import now_utc
from database import SessionLocal
from discovery.classify import classify_discovery_opportunity
from discovery.live_fetchers import get_live_fetcher
from discovery.live_runner import run_live_discovery
from discovery.selection import select_all_eligible_sources
from models import DiscoveredOpportunity
from national_discovery_funnel import stage1_ultra_cheap
from solicitation_identity import identity_key, normalize_title

ARTIFACTS = ROOT / "artifacts"
RUN_TAG = "m3_live_opportunity_test"
CLOSED = {"EXPIRED", "CANCELLED", "CANCELED", "AWARDED", "CLOSED"}
SERVICEISH = {"SERVICE", "CLEARLY_IRRELEVANT"}
PRODUCTISH = {"CORE_PRODUCT", "PRODUCT_PLUS_SERVICE", "LIKELY_PRODUCT_RESALE", "PRODUCT_RESALE"}


def _utc() -> str:
    return now_utc().isoformat()


def _write(name: str, payload: Any) -> Path:
    path = ARTIFACTS / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def _write_text(name: str, text: str) -> Path:
    path = ARTIFACTS / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _priority_bucket(cand: dict[str, Any]) -> int:
    sid = str(cand.get("source_id") or "")
    kind = str(cand.get("kind") or "").upper()
    state = str(cand.get("state_code") or cand.get("jurisdiction") or "").upper()
    name = str(cand.get("name") or cand.get("source_name") or "").lower()
    if sid.startswith("fed_") or kind == "FEDERAL" or "dla" in sid or "dibbs" in sid:
        return 0
    if state in {"NE", "WY"} or sid in {"state_ne", "state_wy"} or "_ne" in sid or "_wy" in sid:
        return 1
    if "nebraska" in name or "wyoming" in name:
        return 1
    if kind == "STATE" or sid.startswith("state_"):
        return 2
    if kind == "NETWORK" or sid.startswith("network_"):
        return 3
    if kind in {"LOCAL", "COUNTY", "CITY"} or "school" in name or "k12" in sid or "airport" in sid:
        return 4
    if kind == "COOPERATIVE" or sid.startswith("coop_"):
        return 5
    return 6


def _registry_to_candidate(row: dict[str, Any]) -> dict[str, Any] | None:
    sid = row.get("source_id")
    url = row.get("discovery_url") or row.get("list_url") or row.get("canonical_base_url")
    fam = row.get("adapter_family")
    if not sid or not url or not fam:
        return None
    if get_live_fetcher(fam) is None:
        return None
    health = str(row.get("health_state") or "").upper()
    if health in {"BLOCKED", "QUARANTINED", "UNAVAILABLE", "AUTH_REQUIRED", "BOT_PROTECTED", "REGISTRATION_REQUIRED"}:
        return None
    jur = str(row.get("jurisdiction") or "")
    kind = str(row.get("government_level") or row.get("entity_type") or "LOCAL").upper()
    if kind in {"STATE"}:
        kind = "STATE"
    elif kind in {"FEDERAL"}:
        kind = "FEDERAL"
    elif "COOP" in kind:
        kind = "COOPERATIVE"
    else:
        kind = "LOCAL"
    return {
        "source_id": sid,
        "name": row.get("source_name") or sid,
        "list_url": url,
        "adapter_family": fam,
        "kind": kind,
        "state_code": jur if len(jur) == 2 else None,
        "jurisdiction": jur or None,
        "platform_family": row.get("platform_family"),
        "adapter_status": row.get("adapter_status") or "UNVERIFIED_LIVE",
        "validation_candidate": True,
        "from_registry_expansion": True,
    }


def build_candidates() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Eligible pool + NE/WY registry extras + limited healthy expansion."""
    from procurement_source_registry import ProcurementSourceRegistry

    bundle = select_all_eligible_sources(include_blocked_accounted=True)
    eligible = list(bundle["eligible"])
    accounted = list(bundle["accounted_non_attempt"])
    pool_ids = {c["source_id"] for c in eligible}
    reg = ProcurementSourceRegistry()

    extras: list[dict[str, Any]] = []
    ne_wy_extras = 0
    other_extras = 0
    OTHER_EXTRA_CAP = 80

    for row in reg.all_sources():
        sid = row.get("source_id")
        if not sid or sid in pool_ids:
            continue
        cand = _registry_to_candidate(row)
        if not cand:
            continue
        is_ne_wy = _priority_bucket(cand) == 1
        if is_ne_wy:
            extras.append(cand)
            ne_wy_extras += 1
            pool_ids.add(sid)
        elif other_extras < OTHER_EXTRA_CAP:
            # Prefer sources that previously produced or look healthy
            productive_hint = bool(row.get("productive")) or str(row.get("health_state") or "").upper() in {
                "HEALTHY",
                "HEALTHY_PRODUCTION",
                "PARTIALLY_PRODUCTIVE",
            }
            if productive_hint or str(row.get("health_state") or "").upper() in {
                "DISCOVERED_UNVALIDATED",
                "UNKNOWN",
                "DEGRADED",
            }:
                extras.append(cand)
                other_extras += 1
                pool_ids.add(sid)

    combined = eligible + extras
    combined.sort(key=lambda c: (_priority_bucket(c), c["source_id"]))
    meta = {
        "eligible_count": len(eligible),
        "accounted_non_attempt_count": len(accounted),
        "ne_wy_registry_extras": ne_wy_extras,
        "other_registry_extras": other_extras,
        "combined_candidates": len(combined),
        "accounted_non_attempt": [
            {"source_id": a.get("source_id"), "accounted_reason": a.get("accounted_reason")}
            for a in accounted
        ],
    }
    return combined, meta


def snapshot_known_keys(session) -> dict[str, Any]:
    keys: set[str] = set()
    title_agency: set[str] = set()
    by_state = Counter()
    for row in session.query(
        DiscoveredOpportunity.canonical_key,
        DiscoveredOpportunity.title,
        DiscoveredOpportunity.agency,
        DiscoveredOpportunity.solicitation_number,
        DiscoveredOpportunity.external_id,
        DiscoveredOpportunity.preferred_source_id,
        DiscoveredOpportunity.state_code,
        DiscoveredOpportunity.status,
    ).yield_per(2000):
        if row.canonical_key:
            keys.add(row.canonical_key)
        ta = f"{normalize_title(row.title)}|{str(row.agency or '').lower()[:40]}"
        title_agency.add(ta)
        # identity-style keys
        rec = {
            "solicitation_number": row.solicitation_number,
            "external_id": row.external_id,
            "agency": row.agency,
            "source_id": row.preferred_source_id,
            "title": row.title,
        }
        try:
            keys.add(identity_key(rec))
        except Exception:
            pass
        st = (row.state_code or "").upper()
        if st:
            by_state[st] += 1
    return {
        "canonical_and_identity_keys": keys,
        "title_agency": title_agency,
        "total_known": len(keys),
        "by_state": dict(by_state),
        "db_count": session.query(DiscoveredOpportunity).count(),
    }


def _record_key(rec: dict[str, Any]) -> str:
    try:
        return identity_key(rec)
    except Exception:
        return f"src:{rec.get('source_id')}:{rec.get('external_id') or normalize_title(rec.get('title'))}"


def _state_of(rec: dict[str, Any]) -> str:
    st = str(rec.get("state_code") or "").upper()
    if st in {"NE", "WY"}:
        return st
    sid = str(rec.get("source_id") or "").lower()
    agency = str(rec.get("agency") or "").lower()
    # Exact source / network identity — do NOT substring-match "_ne" (matches nevada/new_*)
    if sid in {"state_ne", "network_bidnet_nebraska"} or sid.endswith("_ne") or sid.startswith("ne_"):
        return "NE"
    if sid in {"state_wy", "network_bidnet_wyoming"} or sid.endswith("_wy") or sid.startswith("wy_"):
        return "WY"
    if "nebraska" in agency or agency.strip() == "ne":
        return "NE"
    if "wyoming" in agency or agency.strip() == "wy":
        return "WY"
    if "nebraska" in sid:
        return "NE"
    if "wyoming" in sid:
        return "WY"
    return st or "UNK"


def classify_record(
    rec: dict[str, Any],
    *,
    known_keys: set[str],
    title_agency: set[str],
    seen_this_run: set[str],
) -> dict[str, Any]:
    key = _record_key(rec)
    ta = f"{normalize_title(rec.get('title'))}|{str(rec.get('agency') or '').lower()[:40]}"
    status = str(rec.get("status") or rec.get("deadline_status") or "OPEN").upper()
    cls = str(rec.get("product_classification") or rec.get("classification") or "").upper()
    if not cls:
        cls = str(
            classify_discovery_opportunity(
                title=str(rec.get("title") or ""),
                description=str(rec.get("description") or ""),
                status=status,
            ).get("classification")
            or "UNKNOWN"
        ).upper()
        rec["product_classification"] = cls

    stage1 = stage1_ultra_cheap(rec)
    is_dup_run = key in seen_this_run
    seen_this_run.add(key)
    previously_seen = key in known_keys or ta in title_agency

    category = None
    if is_dup_run:
        category = "F_DUPLICATE_EXCLUDED"
    elif previously_seen:
        category = "E_PREVIOUSLY_SEEN_EXCLUDED"
    elif status in CLOSED:
        category = "H_EXPIRED_CANCELLED_EXCLUDED"
    elif cls in SERVICEISH or (not stage1.get("survive") and stage1.get("reason") in {
        "obvious_non_product",
        "class_SERVICE",
        "class_CLEARLY_IRRELEVANT",
    }):
        category = "G_SERVICE_NON_CORE_EXCLUDED"
    elif not stage1.get("survive") and str(stage1.get("reason") or "").startswith("status_"):
        category = "H_EXPIRED_CANCELLED_EXCLUDED"
    elif cls == "UNKNOWN" or stage1.get("reason") == "ambiguous_product_preserved":
        category = "D_NEW_AMBIGUOUS_CLASSIFICATION"
    elif cls in PRODUCTISH or stage1.get("survive"):
        # Distinguish actionable vs needs evidence vs blocked via deadline/package
        dl_status = str(
            (rec.get("deadline_evaluation") or {}).get("status")
            or rec.get("deadline_status")
            or ""
        ).upper()
        pkg = str(rec.get("package_access") or "PUBLIC").upper()
        if dl_status in {"EXPIRED", "PAST"} or status in CLOSED:
            category = "C_NEW_PRODUCT_BLOCKED"
        elif pkg in {"AUTH_REQUIRED", "REGISTRATION_REQUIRED", "BOT_PROTECTED"}:
            category = "C_NEW_PRODUCT_BLOCKED"
        elif dl_status in {"UNKNOWN", ""} and not (rec.get("deadline") or rec.get("response_deadline")):
            category = "B_NEW_PRODUCT_NEEDS_EVIDENCE"
        else:
            category = "A_NEW_PRODUCT_ACTIONABLE"
    else:
        category = "G_SERVICE_NON_CORE_EXCLUDED"

    return {
        "category": category,
        "identity_key": key,
        "previously_seen": previously_seen,
        "duplicate_in_run": is_dup_run,
        "stage1": stage1,
        "classification": cls,
        "state": _state_of(rec),
    }


def summarize_source_health(per_source: dict[str, Any]) -> dict[str, Any]:
    buckets = Counter()
    details = []
    for sid, row in (per_source or {}).items():
        state = str(row.get("explicit_state") or row.get("source_stop_reason") or "").upper()
        ok = bool(row.get("ok"))
        raw = int(row.get("raw") or row.get("records_fetched") or 0)
        if not row.get("attempt", True) and row.get("selection_state"):
            label = str(row.get("selection_state")).upper()
        elif state in {"AUTH_REQUIRED", "REGISTRATION_REQUIRED"}:
            label = state
        elif state in {"BOT_PROTECTED", "BLOCKED"}:
            label = "BLOCKED"
        elif state in {"BACKOFF"}:
            label = "BACKOFF"
        elif ok and raw > 0:
            label = "HEALTHY_PRODUCTIVE"
        elif ok and raw == 0:
            label = "HEALTHY_ZERO"
        elif state in {"TECHNICAL_FAILURE", "SOURCE_EXCEPTION", "NO_FETCHER", "VALIDATION_FAILURE"}:
            label = "DEGRADED"
        elif state in {"UNAVAILABLE"}:
            label = "UNAVAILABLE"
        elif not ok:
            label = "DEGRADED"
        else:
            label = state or "UNKNOWN"
        buckets[label] += 1
        details.append(
            {
                "source_id": sid,
                "health": label,
                "ok": ok,
                "raw": raw,
                "product_candidates": row.get("product_candidates"),
                "stop": row.get("source_stop_reason"),
                "attempt": row.get("attempt", True),
            }
        )
    return {"buckets": dict(buckets), "details": details, "tested": len(details)}


def _card(rec: dict[str, Any], meta: dict[str, Any], pipeline_row: dict[str, Any] | None) -> dict[str, Any]:
    pr = pipeline_row or {}
    deadline = rec.get("deadline") or rec.get("response_deadline") or rec.get("deadline_raw")
    dl_eval = rec.get("deadline_evaluation") or pr.get("deadline_evaluation") or {}
    return {
        "opportunity_id": pr.get("canonical_id") or meta.get("identity_key") or rec.get("external_id"),
        "title": rec.get("title"),
        "agency": rec.get("agency"),
        "state": meta.get("state") or rec.get("state_code"),
        "source": rec.get("source_id") or rec.get("preferred_source_id"),
        "source_url": rec.get("detail_url") or rec.get("preferred_source_url") or rec.get("source_url"),
        "deadline": deadline,
        "deadline_runway": dl_eval.get("calendar_days_remaining")
        or dl_eval.get("runway_days")
        or dl_eval.get("status")
        or "UNKNOWN",
        "product_description": (rec.get("description") or "")[:400] or rec.get("title"),
        "solicitation_value": rec.get("estimated_value") or pr.get("government_revenue") or "UNKNOWN",
        "why_product_resale": meta.get("stage1", {}).get("reason")
        or f"classification={meta.get('classification')}",
        "classification": meta.get("classification"),
        "category": meta.get("category"),
        "what_m3_knows": {
            "classification": meta.get("classification"),
            "stage1_survive": meta.get("stage1", {}).get("survive"),
            "package_access": rec.get("package_access") or "UNKNOWN",
            "solicitation_number": rec.get("solicitation_number") or rec.get("external_id"),
        },
        "what_m3_does_not_know": [
            k
            for k, v in {
                "supplier_quote": pr.get("supplier_quote") or pr.get("acquisition_cost"),
                "freight": pr.get("freight"),
                "payment_terms": pr.get("payment_terms"),
                "lead_time": pr.get("lead_time"),
                "current_margin": pr.get("margin"),
            }.items()
            if v in (None, "", "UNKNOWN")
        ]
        or ["supplier_quote", "freight", "payment_terms", "lead_time", "margin"],
        "supplier_status": pr.get("supply_status") or pr.get("supplier_status") or "UNKNOWN",
        "historical_pricing_status": pr.get("historical_pricing_status") or "UNKNOWN_OR_NOT_QUERIED",
        "economics_status": pr.get("economics_status")
        or ("KNOWN_PARTIAL" if pr.get("government_revenue") else "UNKNOWN"),
        "capital_status": pr.get("funding_status") or pr.get("capital_status") or "UNKNOWN",
        "pursuit_readiness": pr.get("pursuit_readiness")
        or pr.get("readiness_dimensions")
        or pr.get("lifecycle")
        or "NOT_YET_EVALUATED",
        "next_action": pr.get("next_action")
        or pr.get("recommended_next_step")
        or "Open Deal Room / gather package evidence",
        "pipeline_present": bool(pipeline_row),
    }


def format_human_table(cards: list[dict[str, Any]]) -> str:
    lines = ["# NEW PRODUCT OPPORTUNITIES (LIVE TEST)", ""]
    if not cards:
        lines.append("_No genuinely new product opportunities survived this sweep._")
        return "\n".join(lines)
    for i, c in enumerate(cards, 1):
        lines.extend(
            [
                f"## {i}. {c.get('title')}",
                f"- **ID:** {c.get('opportunity_id')}",
                f"- **Agency / State:** {c.get('agency')} / {c.get('state')}",
                f"- **Source:** {c.get('source')}",
                f"- **URL:** {c.get('source_url')}",
                f"- **Deadline / runway:** {c.get('deadline')} / {c.get('deadline_runway')}",
                f"- **Value:** {c.get('solicitation_value')}",
                f"- **Why product-resale:** {c.get('why_product_resale')} ({c.get('classification')})",
                f"- **Category:** {c.get('category')}",
                f"- **Supplier:** {c.get('supplier_status')} | **Historical $:** {c.get('historical_pricing_status')}",
                f"- **Economics:** {c.get('economics_status')} | **Capital:** {c.get('capital_status')}",
                f"- **Pursuit readiness:** {c.get('pursuit_readiness')}",
                f"- **Next action:** {c.get('next_action')}",
                f"- **Unknowns:** {', '.join(c.get('what_m3_does_not_know') or [])}",
                "",
            ]
        )
    return "\n".join(lines)


def main() -> int:
    started = _utc()
    print(f"[{started}] Building candidates…", flush=True)
    candidates, cand_meta = build_candidates()
    _write(f"{RUN_TAG}_candidates.json", {"meta": cand_meta, "candidates": candidates})
    print(f"Candidates: {cand_meta}", flush=True)

    session = SessionLocal()
    try:
        snap = snapshot_known_keys(session)
        known_keys: set[str] = snap["canonical_and_identity_keys"]
        title_agency: set[str] = snap["title_agency"]
        print(f"Known keys snapshot: {snap['total_known']} (db={snap['db_count']})", flush=True)

        run_id = f"LOT-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        print(f"[{_utc()}] Starting live discovery run_id={run_id} sources={len(candidates)}", flush=True)

        def _on_source(metrics: dict[str, Any], sid: str) -> None:
            attempted = int(metrics.get("sources_attempted") or 0)
            if attempted % 10 == 0 or attempted <= 3:
                print(
                    f"[{_utc()}] progress sources={attempted}/{len(candidates)} "
                    f"raw={metrics.get('raw_records')} unique={metrics.get('unique_records')} "
                    f"last={sid} persist_fail={metrics.get('persist_failures')}",
                    flush=True,
                )
                _write(
                    f"{RUN_TAG}_progress.json",
                    {
                        "at": _utc(),
                        "sources_attempted": attempted,
                        "raw_records": metrics.get("raw_records"),
                        "unique_records": metrics.get("unique_records"),
                        "last_source": sid,
                        "persist_failures": metrics.get("persist_failures"),
                    },
                )

        live = run_live_discovery(
            session,
            profile="broad",
            preview=False,
            persist=True,
            authorize_live=True,
            candidates_override=candidates,
            on_source_complete=_on_source,
        )
        # Checkpoint in-memory live result before any further DB work
        _write(
            f"{RUN_TAG}_live_raw.json",
            {
                "run_id": run_id,
                "run_status": live.get("run_status"),
                "partial_reason": live.get("partial_reason"),
                "metrics": {
                    k: v
                    for k, v in (live.get("metrics") or {}).items()
                    if k != "per_source"
                },
                "per_source": (live.get("metrics") or {}).get("per_source"),
                "handoff_record_count": len(live.get("handoff_records") or []),
                "completeness": live.get("completeness"),
                "LIVE_API_REQUESTS": live.get("LIVE_API_REQUESTS"),
            },
        )

        if live.get("error"):
            print("LIVE ERROR", live.get("error"), flush=True)
            _write(f"{RUN_TAG}_error.json", live)
            return 1

        try:
            session.commit()
        except Exception as exc:
            print(f"commit after live failed (continuing with in-memory results): {exc}", flush=True)
            try:
                session.rollback()
            except Exception:
                pass

        metrics = live.get("metrics") or {}
        per_source = metrics.get("per_source") or {}
        health = summarize_source_health(per_source)

        # Apply registry health (existing mechanism)
        try:
            from procurement_source_registry import ProcurementSourceRegistry
            from m3_discovery_service import _restore_registry_from_db, _persist_registry

            reg = ProcurementSourceRegistry()
            _restore_registry_from_db(reg)
            before_health = Counter(s.get("health_state") for s in reg.all_sources())
            reg.apply_discovery_health(per_source)
            _persist_registry(reg)
            after_health = Counter(s.get("health_state") for s in reg.all_sources())
            registry_health = {
                "before": dict(before_health),
                "after": dict(after_health),
            }
        except Exception as exc:
            registry_health = {"error": str(exc)}

        from m3_discovery_service import _records_from_live_result, _apply_incremental_filter

        raw_records = _records_from_live_result(live, discovery_run_id=run_id)
        filtered = _apply_incremental_filter(raw_records)

        seen_this_run: set[str] = set()
        categorized: dict[str, list[dict[str, Any]]] = defaultdict(list)
        classified_rows: list[dict[str, Any]] = []

        for rec in raw_records:
            meta = classify_record(
                rec,
                known_keys=known_keys,
                title_agency=title_agency,
                seen_this_run=seen_this_run,
            )
            row = {"record": rec, "meta": meta}
            categorized[meta["category"]].append(row)
            classified_rows.append(row)

        # Stage 0/1 survivors among NEW product categories
        new_product_cats = {
            "A_NEW_PRODUCT_ACTIONABLE",
            "B_NEW_PRODUCT_NEEDS_EVIDENCE",
            "C_NEW_PRODUCT_BLOCKED",
            "D_NEW_AMBIGUOUS_CLASSIFICATION",
        }
        new_product = [r for r in classified_rows if r["meta"]["category"] in new_product_cats]
        stage1_survivors = [
            r
            for r in new_product
            if r["meta"]["stage1"].get("survive")
            and r["meta"]["category"] != "C_NEW_PRODUCT_BLOCKED"
        ]

        # Durable handoff for stage1 survivors (existing pipeline gates)
        survivors_payload = []
        for r in stage1_survivors:
            rec = dict(r["record"])
            rec["cheap_screen_survive"] = True
            rec["product_classification"] = r["meta"]["classification"]
            survivors_payload.append(rec)

        handoff_result: dict[str, Any] = {}
        pipeline_by_key: dict[str, dict[str, Any]] = {}
        if survivors_payload:
            try:
                from m3_pipeline_store import M3PipelineStore
                from m3_discovery_service import restore_pipeline_store_from_db, _persist_pipeline_store
                from m3_end_to_end import M3EndToEndOrchestrator
                from m3_pipeline_handoff import run_durable_handoff

                store = M3PipelineStore()
                restore_pipeline_store_from_db(store)
                orch = M3EndToEndOrchestrator(store=store)
                handoff_result = run_durable_handoff(
                    run_id=run_id,
                    survivors=survivors_payload,
                    store=store,
                    orch=orch,
                    discovery_metrics=metrics,
                    resume=True,
                )
                _persist_pipeline_store(store)
                for row in store.all():
                    for k in (
                        row.get("canonical_id"),
                        row.get("identity_key"),
                        identity_key(row) if row.get("title") else None,
                    ):
                        if k:
                            pipeline_by_key[str(k)] = row
            except Exception as exc:
                handoff_result = {"error": str(exc), "trace": traceback.format_exc()}

        # Build operator cards for A/B/D (and blocked C briefly)
        cards = []
        for r in new_product:
            meta = r["meta"]
            if meta["category"] not in {
                "A_NEW_PRODUCT_ACTIONABLE",
                "B_NEW_PRODUCT_NEEDS_EVIDENCE",
                "D_NEW_AMBIGUOUS_CLASSIFICATION",
            }:
                continue
            ik = meta["identity_key"]
            pr = pipeline_by_key.get(ik)
            if not pr:
                # try title match
                t = normalize_title(r["record"].get("title"))
                for row in pipeline_by_key.values():
                    if normalize_title(row.get("title")) == t:
                        pr = row
                        break
            cards.append(_card(r["record"], meta, pr))

        # Freshness counts
        cat_counts = {k: len(v) for k, v in categorized.items()}
        ne_rows = [r for r in classified_rows if r["meta"]["state"] == "NE"]
        wy_rows = [r for r in classified_rows if r["meta"]["state"] == "WY"]

        def _state_breakdown(rows: list[dict[str, Any]]) -> dict[str, int]:
            c = Counter(r["meta"]["category"] for r in rows)
            new_prod = sum(c[k] for k in new_product_cats)
            return {
                "total_retrieved": len(rows),
                "new_product": new_prod,
                "previously_seen": c.get("E_PREVIOUSLY_SEEN_EXCLUDED", 0),
                "duplicates": c.get("F_DUPLICATE_EXCLUDED", 0),
                "expired_cancelled": c.get("H_EXPIRED_CANCELLED_EXCLUDED", 0),
                "service_non_core": c.get("G_SERVICE_NON_CORE_EXCLUDED", 0),
                "by_category": dict(c),
            }

        deeper = int(handoff_result.get("research_queued") or handoff_result.get("deep_research_queued") or 0)
        readiness_reached = sum(
            1
            for c in cards
            if c.get("pipeline_present")
            and c.get("pursuit_readiness") not in (None, "NOT_YET_EVALUATED", "")
        )

        report = {
            "run_id": run_id,
            "started_at": started,
            "completed_at": _utc(),
            "candidate_meta": cand_meta,
            "live_run_status": live.get("run_status"),
            "partial_reason": live.get("partial_reason"),
            "completeness": live.get("completeness"),
            "metrics_summary": {
                "sources_attempted": metrics.get("sources_attempted"),
                "sources_successful": metrics.get("sources_successful"),
                "sources_failed": metrics.get("sources_failed"),
                "raw_records": metrics.get("raw_records"),
                "unique_records": metrics.get("unique_records"),
                "CORE_PRODUCT": metrics.get("CORE_PRODUCT"),
                "PRODUCT_PLUS_SERVICE": metrics.get("PRODUCT_PLUS_SERVICE"),
                "UNKNOWN": metrics.get("UNKNOWN"),
                "SERVICE": metrics.get("SERVICE"),
                "expired": metrics.get("expired"),
                "LIVE_API_REQUESTS": live.get("LIVE_API_REQUESTS"),
            },
            "source_health": health,
            "registry_health": registry_health,
            "freshness": {
                "total_live_candidates_retrieved": len(raw_records),
                "after_incremental_filter": len(filtered),
                "previously_seen": cat_counts.get("E_PREVIOUSLY_SEEN_EXCLUDED", 0),
                "duplicates": cat_counts.get("F_DUPLICATE_EXCLUDED", 0),
                "expired_cancelled": cat_counts.get("H_EXPIRED_CANCELLED_EXCLUDED", 0),
                "service_non_core": cat_counts.get("G_SERVICE_NON_CORE_EXCLUDED", 0),
                "new_product_candidates": len(new_product),
                "new_product_stage1_survivors": len(stage1_survivors),
                "new_product_deeper_research_queued": deeper,
                "new_product_pursuit_readiness": readiness_reached,
                "category_counts": cat_counts,
            },
            "nebraska": _state_breakdown(ne_rows),
            "wyoming": _state_breakdown(wy_rows),
            "handoff": {
                "status": handoff_result.get("status"),
                "pipeline_new": handoff_result.get("pipeline_new"),
                "pipeline_updated": handoff_result.get("pipeline_updated"),
                "research_queued": deeper,
                "error": handoff_result.get("error"),
            },
            "new_opportunity_cards": cards,
            "blockers": [
                x
                for x in [
                    live.get("partial_reason"),
                    handoff_result.get("error"),
                    registry_health.get("error") if isinstance(registry_health, dict) else None,
                ]
                if x
            ],
            "snapshot_before": {
                "total_known_keys": snap["total_known"],
                "db_count": snap["db_count"],
                "by_state": snap["by_state"],
            },
            "no_outreach": True,
            "no_paid_aggregators": True,
            "SAM": live.get("SAM"),
            "OpenAI": live.get("OpenAI"),
            "paid": live.get("paid"),
        }

        _write(f"{RUN_TAG}_report.json", report)
        _write(
            f"{RUN_TAG}_classified.json",
            {
                "counts": cat_counts,
                "samples": {
                    k: [
                        {
                            "title": r["record"].get("title"),
                            "agency": r["record"].get("agency"),
                            "source_id": r["record"].get("source_id"),
                            "state": r["meta"]["state"],
                            "classification": r["meta"]["classification"],
                            "identity_key": r["meta"]["identity_key"],
                            "url": r["record"].get("detail_url"),
                        }
                        for r in v[:25]
                    ]
                    for k, v in categorized.items()
                },
            },
        )
        _write(f"{RUN_TAG}_source_health.json", health)
        human = format_human_table(cards)
        _write_text(f"{RUN_TAG}_new_opportunities.md", human)

        print(human.encode("utf-8", errors="replace").decode("utf-8"), flush=True)
        print("\n=== FRESHNESS ===", json.dumps(report["freshness"], indent=2), flush=True)
        print("=== NE ===", json.dumps(report["nebraska"], indent=2), flush=True)
        print("=== WY ===", json.dumps(report["wyoming"], indent=2), flush=True)
        print("=== HEALTH BUCKETS ===", health["buckets"], flush=True)
        print(f"Artifacts under artifacts/{RUN_TAG}_*", flush=True)
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
