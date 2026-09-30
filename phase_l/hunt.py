"""Phase L cross-government hunt — access before pricing; unified ranking."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from application_clock import now_utc
from phase_i.hunt import cheap_product_screen, early_hard_reject
from phase_l.access_gate import ACCESS_YES, evaluate_phase_l_access
from phase_l.competition import annotate_competition, offer_bucket
from phase_l.economics import build_phase_l_economics, profit_tier
from phase_l.federal_public_search import public_sam_opportunity_search
from phase_l.normalize import (
    COOPERATIVE,
    FEDERAL,
    LOCAL,
    READY_FOR_OWNER_REVIEW,
    STATE,
    infer_source_level,
    normalize_opportunity,
)
from phase_l.registration_tracker import (
    EASY_REGISTRATION,
    REGISTER_BEFORE_BID,
    REGISTER_NOW_RECURRING_BUYER,
    VERIFY_REGISTRATION_TIMING,
    record_portal_sighting,
    recurring_registration_actions,
    reset_live_counts,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"

_QTY_RE = re.compile(r"\b(?:qty|quantity)[:\s#]*([\d,]+(?:\.\d+)?)\b|\b([\d,]+(?:\.\d+)?)\s*(EA|EACH|KT|KIT)\b", re.I)

# Mix targets from Phase L prompt
TARGET_TOTAL = 1000
TARGET_FEDERAL = 400
TARGET_STATE = 300
TARGET_LOCAL_COOP = 300


def _utc() -> str:
    return now_utc().isoformat()


def _write(name: str, payload: Any) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def _extract_qty_uom(row: dict[str, Any]) -> tuple[float | None, str | None]:
    if row.get("quantity") is not None:
        try:
            qty = float(row["quantity"])
            uom = row.get("uom") or "EA"
            return qty, str(uom).upper()
        except (TypeError, ValueError):
            pass
    blob = f"{row.get('title') or ''}\n{row.get('description') or ''}"
    m = _QTY_RE.search(blob)
    if not m:
        return None, None
    raw = m.group(1) or m.group(2)
    uom = (m.group(3) or "EA").upper()
    if uom == "EACH":
        uom = "EA"
    if uom == "KIT":
        uom = "KT"
    try:
        return float(str(raw).replace(",", "")), uom
    except ValueError:
        return None, None


def _row_from_canonical(c: Any) -> dict[str, Any]:
    if isinstance(c, dict):
        d = dict(c)
    else:
        d = c.to_dict() if hasattr(c, "to_dict") else dict(c)
    d["source_level"] = infer_source_level(d)
    d.setdefault("live_status", d.get("status") or "OPEN")
    return d


def discover_live_sources(
    *,
    authorize_live: bool = True,
    profile: str = "broad",
    max_sources: int | None = 40,
    include_federal_public: bool = True,
    federal_keyword_pages: int = 6,
) -> dict[str, Any]:
    """Pull live opportunities from discovery runner + public SAM search."""
    from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode

    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)

    rows: list[dict[str, Any]] = []
    source_failures: list[dict[str, Any]] = []
    discovery_meta: dict[str, Any] = {"federal_public": None, "live_runner": None, "profile": profile}

    if include_federal_public:
        print("[phase_l] federal public SAM search...", flush=True)
        fed = public_sam_opportunity_search(
            max_pages_per_keyword=federal_keyword_pages,
            page_size=100,
            max_total=max(TARGET_FEDERAL + 50, 500),
        )
        discovery_meta["federal_public"] = {
            "coverage": fed.get("discovery_coverage"),
            "count": fed.get("count"),
            "attempts": len(fed.get("attempts") or []),
        }
        rows.extend(fed.get("rows") or [])
        print(f"[phase_l] federal public rows={fed.get('count')} coverage={fed.get('discovery_coverage')}", flush=True)

    print(f"[phase_l] live_runner profile={profile} max_sources={max_sources}...", flush=True)
    try:
        from discovery.live_runner import run_live_discovery
        from discovery.selection import select_all_eligible_sources

        bundle = select_all_eligible_sources(include_blocked_accounted=True)
        eligible = list(bundle.get("eligible") or [])

        # Phase L.4 commercial_feed: platform-weighted BidNet/state/local scale-up
        # Phase L.14 non_bidnet / nonbidnet: park BidNet NETWORK, prefer OpenGov/IonWave/…
        if profile in {"non_bidnet", "nonbidnet", "l14", "l15", "structured"}:
            from phase_l.nonbidnet_expansion import (
                L14_DEFAULT_MAX_SOURCES,
                L14_KIND_CAPS,
                prioritize_candidates_nonbidnet,
            )
            from phase_l.bidnet_parked import park_bidnet_auth_history

            park_bidnet_auth_history()
            eff_max = max_sources if max_sources is not None else L14_DEFAULT_MAX_SOURCES
            # L.15: put structured_* sources first, then non-BidNet diversification
            structured = [c for c in eligible if str(c.get("source_id") or "").startswith("structured_")]
            rest = [c for c in eligible if not str(c.get("source_id") or "").startswith("structured_")]
            diversified_rest = prioritize_candidates_nonbidnet(
                rest, kind_caps=L14_KIND_CAPS, max_sources=eff_max, exclude_bidnet_network=True
            )
            diversified = structured + diversified_rest
            if max_sources:
                diversified = diversified[: max(eff_max, len(structured) + 8)]
            from phase_l.resilient_hunt import order_candidates_by_health

            # Keep structured sources at front; health-order the rest only
            tail = order_candidates_by_health(diversified[len(structured) :])
            diversified = structured + tail
            discovery_meta["l14_kind_caps"] = dict(L14_KIND_CAPS)
            discovery_meta["l14_sources_selected"] = len(diversified)
            discovery_meta["structured_sources_selected"] = len(structured)
            discovery_meta["bidnet_auth_history_parked"] = True
            discovery_meta["profile"] = "structured" if profile in {"l15", "structured"} else "non_bidnet"
        elif profile in {"commercial_feed", "l4", "commercial"}:
            from phase_l.commercial_feed_expansion import (
                L4_DEFAULT_MAX_SOURCES,
                L4_KIND_CAPS,
                prioritize_candidates_commercial,
            )

            eff_max = max_sources if max_sources is not None else L4_DEFAULT_MAX_SOURCES
            diversified = prioritize_candidates_commercial(
                eligible, kind_caps=L4_KIND_CAPS, max_sources=None
            )
            # L.11: health/yield order before capping so productive BidNet states aren't sliced away
            from phase_l.resilient_hunt import order_candidates_by_health

            diversified = order_candidates_by_health(diversified)[:eff_max]
            discovery_meta["l4_kind_caps"] = dict(L4_KIND_CAPS)
            discovery_meta["l4_sources_selected"] = len(diversified)
        else:
            # Diversify candidates: take from each kind (legacy broad profile)
            by_kind: dict[str, list] = {FEDERAL: [], STATE: [], LOCAL: [], COOPERATIVE: [], "NETWORK": []}
            for c in eligible:
                k = str(c.get("kind") or "LOCAL").upper()
                if k == "NETWORK":
                    by_kind["NETWORK"].append(c)
                elif k in by_kind:
                    by_kind[k].append(c)
                else:
                    by_kind[LOCAL].append(c)
            diversified = []
            # Prefer state/local/coop; federal non-SAM last (SAM via public search)
            for kind, cap in (
                (STATE, 16),
                (LOCAL, 12),
                ("NETWORK", 8),
                (COOPERATIVE, 6),
                (FEDERAL, 4),
            ):
                diversified.extend(by_kind.get(kind, [])[:cap])
            if max_sources:
                diversified = diversified[:max_sources]

        # L.11/L.14: resilient per-source watchdog (no hung hunt)
        use_resilient = profile in {
            "commercial_feed",
            "l4",
            "commercial",
            "resilient",
            "non_bidnet",
            "nonbidnet",
            "l14",
            "l15",
            "structured",
        } and authorize_live
        if use_resilient:
            from phase_l.resilient_hunt import run_resilient_live_discovery

            result = run_resilient_live_discovery(
                diversified,
                authorize_live=authorize_live,
                resume=True,
                source_timeout_s=75.0,
                max_pages=2,
            )
        else:
            result = run_live_discovery(
                session=None,
                profile="phase_l_commercial" if profile in {
                    "commercial_feed", "l4", "commercial", "non_bidnet", "nonbidnet", "l14"
                } else (
                    "tiny" if (max_sources or 40) <= 8 else "broad"
                ),
                preview=True,
                persist=False,
                authorize_live=authorize_live,
                candidates_override=diversified,
                max_sources=len(diversified),
                fetch_details=False,
                fetch_documents=False,
                source_wall_clock_s=75.0 if authorize_live else None,
            )
        metrics = result.get("metrics") or {}
        discovery_meta["live_runner"] = {
            "sources_attempted": metrics.get("sources_attempted") or result.get("completeness", {}).get("attempted_sources"),
            "sources_successful": metrics.get("sources_successful"),
            "sources_failed": metrics.get("sources_failed"),
            "sources_auth_blocked": metrics.get("sources_auth_blocked"),
            "sources_registration_blocked": metrics.get("sources_registration_blocked"),
            "sources_bot_blocked": metrics.get("sources_bot_blocked"),
            "sources_timed_out": metrics.get("sources_timed_out"),
            "unique_records": metrics.get("unique_records"),
            "raw_records": metrics.get("raw_records"),
            "error": result.get("error"),
            "run_status": result.get("run_status"),
            "resilient": use_resilient,
            "timed_out_sources": metrics.get("timed_out_sources"),
            "failed_sources": metrics.get("failed_sources"),
        }
        per_source = metrics.get("per_source") or {}
        for sid, meta in per_source.items():
            if not isinstance(meta, dict):
                continue
            st = str(meta.get("status") or meta.get("health") or meta.get("explicit_state") or "")
            if st and st not in {"SUCCESS", "HEALTHY_ZERO", "OK"}:
                source_failures.append(
                    {
                        "source_id": sid,
                        "status": st,
                        "error": meta.get("error") or meta.get("failure") or meta.get("stop_reason") or meta.get("source_stop_reason"),
                    }
                )
        opps = result.get("opportunities") or result.get("handoff_records") or []
        for o in opps:
            rows.append(_row_from_canonical(o))
        print(
            f"[phase_l] live_runner status={result.get('run_status')} "
            f"unique={metrics.get('unique_records')} raw={metrics.get('raw_records')} "
            f"opps={len(opps)} timed_out={metrics.get('sources_timed_out')}",
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001
        discovery_meta["live_runner"] = {"error": str(exc)[:300], "run_status": "FAILED"}
        source_failures.append({"source_id": "live_runner", "error": str(exc)[:300]})
        print(f"[phase_l] live_runner error: {exc}", flush=True)

    # Dedup
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for r in rows:
        key = str(
            r.get("notice_id")
            or r.get("external_id")
            or r.get("solicitation_number")
            or r.get("detail_url")
            or r.get("title")
            or ""
        ).lower()[:160]
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(r)

    return {
        "rows": unique,
        "source_failures": source_failures,
        "discovery_meta": discovery_meta,
        "retrieved_at": _utc(),
    }


def screen_and_rank(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Product screen → access gate → (only if YES) light economics → normalize."""
    funnel = {
        FEDERAL: Counter(),
        STATE: Counter(),
        LOCAL: Counter(),
        COOPERATIVE: Counter(),
        "TOTAL": Counter(),
    }
    open_offer_dist = Counter()
    prefiltered_offer_dist = Counter()
    normalized_rows: list[dict[str, Any]] = []
    accessible: list[dict[str, Any]] = []
    blockers = Counter()
    registration_actions: list[dict[str, Any]] = []
    seen_reg_keys: set[tuple[str, str]] = set()

    reset_live_counts()

    for raw in rows:
        level = infer_source_level(raw)
        for bucket in (level, "TOTAL"):
            funnel[bucket]["raw_live"] += 1

        if early_hard_reject(raw):
            for bucket in (level, "TOTAL"):
                funnel[bucket]["hard_rejected"] += 1
            continue

        screen = cheap_product_screen(raw)
        raw = dict(screen.get("row") or raw)
        raw["is_product"] = bool(screen.get("is_product"))
        if not screen.get("is_product"):
            for bucket in (level, "TOTAL"):
                funnel[bucket]["non_product"] += 1
            continue
        for bucket in (level, "TOTAL"):
            funnel[bucket]["tangible_products"] += 1

        qty, uom = _extract_qty_uom(raw)
        if qty is not None:
            raw["quantity"] = qty
        if uom:
            raw["uom"] = uom

        access = evaluate_phase_l_access(raw)
        for bucket in (level, "TOTAL"):
            funnel[bucket]["access_checked"] += 1

        our = access.get("our_bid_access")
        if our == ACCESS_YES:
            for bucket in (level, "TOTAL"):
                funnel[bucket]["access_yes"] += 1
            if access.get("is_easy_registration"):
                for bucket in (level, "TOTAL"):
                    funnel[bucket]["easy_registration_only"] += 1
        elif our == "NO":
            for bucket in (level, "TOTAL"):
                funnel[bucket]["inaccessible"] += 1
                funnel[bucket]["real_eligibility_blockers"] += 1
            if access.get("access_blocker"):
                blockers[str(access["access_blocker"]).split(";")[0][:80]] += 1
        else:
            for bucket in (level, "TOTAL"):
                funnel[bucket]["access_unknown_or_conditional"] += 1
            if access.get("access_blocker"):
                blockers[str(access["access_blocker"]).split(";")[0][:80]] += 1

        # Track easy-reg portals for recurring buyer intelligence
        if access.get("is_easy_registration") or (
            our == ACCESS_YES and access.get("vendor_registration_required")
        ):
            sid = str(raw.get("source_id") or access.get("portal_source") or "").strip()
            if sid and not sid.startswith("fed_sam"):
                portal_row = record_portal_sighting(
                    source_id=sid,
                    buyer_name=str(raw.get("agency") or raw.get("buyer_name") or sid),
                    jurisdiction=str(
                        raw.get("jurisdiction") or raw.get("state_code") or level
                    ),
                    registration_url=raw.get("registration_url") or raw.get("detail_url"),
                    registration_type=str(
                        access.get("registration_gate_type") or EASY_REGISTRATION
                    ),
                    is_relevant_product=True,
                    is_live=True,
                )
                rec_action = portal_row.get("recommended_action") or access.get(
                    "registration_action"
                )
                # Prefer recurring recommendation over per-opp REGISTER_BEFORE_BID
                if rec_action and rec_action != "NONE":
                    access = dict(access)
                    access["registration_action"] = rec_action

        # Expensive path only for YES — always attempt economics (may be pending)
        economics = None
        if our == ACCESS_YES:
            for bucket in (level, "TOTAL"):
                funnel[bucket]["economics_researched"] += 1
            economics = build_phase_l_economics(
                quantity=raw.get("quantity"),
                uom=raw.get("uom"),
                historical_unit_price=raw.get("historical_award_unit_price")
                or raw.get("historical_unit_price"),
                public_retail_unit_price=raw.get("public_retail_unit_price")
                or raw.get("public_retail_price"),
                public_retail_source=raw.get("public_retail_source"),
                freight=raw.get("freight"),
            )
            if economics.get("public_retail_unit_price") is not None:
                for bucket in (level, "TOTAL"):
                    funnel[bucket]["retail_price_found"] += 1
            if economics.get("meets_floor"):
                for bucket in (level, "TOTAL"):
                    funnel[bucket]["net_ge_10k"] += 1
                net = economics.get("expected_net_profit") or 0
                for thr, key in (
                    (25000, "net_ge_25k"),
                    (50000, "net_ge_50k"),
                    (75000, "net_ge_75k"),
                    (100000, "net_ge_100k"),
                ):
                    if net >= thr:
                        for bucket in (level, "TOTAL"):
                            funnel[bucket][key] += 1
            elif (
                economics.get("gross_retail_spread") is not None
                and economics["gross_retail_spread"] >= 10000
            ):
                for bucket in (level, "TOTAL"):
                    funnel[bucket]["gross_spread_ge_10k"] += 1

            reg_act = access.get("registration_action")
            if reg_act in {
                REGISTER_BEFORE_BID,
                REGISTER_NOW_RECURRING_BUYER,
                VERIFY_REGISTRATION_TIMING,
                "REGISTER_NOW",
            }:
                key = (str(raw.get("source_id") or ""), str(reg_act))
                if key not in seen_reg_keys:
                    seen_reg_keys.add(key)
                    registration_actions.append(
                        {
                            "action": reg_act,
                            "source_id": raw.get("source_id"),
                            "buyer": raw.get("agency") or raw.get("buyer_name"),
                            "title": raw.get("title"),
                            "solicitation": raw.get("solicitation_number")
                            or raw.get("external_id"),
                            "registration_gate_type": access.get("registration_gate_type"),
                            "is_easy_registration": access.get("is_easy_registration"),
                        }
                    )

        if raw.get("historical_award_unit_price") or raw.get("historical_award_price"):
            for bucket in (level, "TOTAL"):
                funnel[bucket]["historical_pricing_found"] += 1
        if raw.get("offer_count") is not None or raw.get("historical_offers_received") is not None:
            for bucket in (level, "TOTAL"):
                funnel[bucket]["offer_count_found"] += 1

        comp = annotate_competition(
            historical_offers_received=raw.get("historical_offers_received") or raw.get("offer_count"),
            historical_competition_type=raw.get("historical_competition_type"),
            competition_access_type=access.get("competition_access_type"),
        )
        if comp.get("historical_offers_received") is not None:
            b = offer_bucket(comp["historical_offers_received"])
            if comp.get("effective_competition_signal") == "PRE_FILTERED_COMPETITION":
                prefiltered_offer_dist[b] += 1
            elif comp.get("is_open_comparable"):
                open_offer_dist[b] += 1

        norm = normalize_opportunity(
            raw,
            access=access,
            economics=economics,
            competition=comp,
            runway_days=raw.get("runway_days"),
        )
        if access.get("registration_action"):
            norm["registration_action"] = access.get("registration_action")
            norm["registration_gate_type"] = access.get("registration_gate_type")
            norm["is_easy_registration"] = access.get("is_easy_registration")
            norm["submission_readiness"] = access.get("submission_readiness")
            norm["can_compete"] = access.get("can_compete")
        if norm.get("identity_match_type") not in {None, "UNKNOWN"}:
            for bucket in (level, "TOTAL"):
                funnel[bucket]["identity_resolved"] += 1

        normalized_rows.append(norm)
        if our == ACCESS_YES:
            accessible.append(norm)

    # Rank accessible: profit tier, then competition rank, then runway
    tier_rank = {
        "MONSTER": 6,
        "EXCEPTIONAL": 5,
        "EXCELLENT": 4,
        "STRONG": 3,
        "PASS": 2,
        "FAIL": 0,
        None: 0,
    }

    def sort_key(r: dict[str, Any]) -> tuple:
        econ = r.get("economics") or {}
        net = econ.get("expected_net_profit")
        return (
            1 if r.get("actionable_state") == READY_FOR_OWNER_REVIEW else 0,
            tier_rank.get(r.get("profit_tier"), 0),
            float(net) if isinstance(net, (int, float)) else -1,
            int((r.get("competition") or {}).get("competition_rank_score") or 0),
            float(r.get("runway_days") or 0),
        )

    accessible_sorted = sorted(accessible, key=sort_key, reverse=True)

    research = [
        r
        for r in normalized_rows
        if r.get("our_bid_access") in {"CONDITIONAL", "UNKNOWN"}
        and r.get("competition_access_type")
        in {"OPEN_MARKET", "UNRESTRICTED", "TOTAL_SMALL_BUSINESS"}
    ]
    research_sorted = sorted(research, key=sort_key, reverse=True)

    # Deduplicate recurring-buyer funnel (count unique portals, not per-opp increments)
    recurring_portals = [
        p
        for p in recurring_registration_actions()
        if p.get("recommended_action") == REGISTER_NOW_RECURRING_BUYER
    ]
    for bucket in (FEDERAL, STATE, LOCAL, COOPERATIVE, "TOTAL"):
        funnel[bucket]["recurring_buyers_needing_registration"] = (
            len(recurring_portals) if bucket == "TOTAL" else 0
        )
    # State/local split approximation from portal jurisdiction tags
    for p in recurring_portals:
        jur = str(p.get("jurisdiction") or "").upper()
        if jur == STATE or "STATE" in jur:
            funnel[STATE]["recurring_buyers_needing_registration"] += 1
        elif jur == LOCAL or any(x in jur for x in ("CITY", "COUNTY", "LOCAL", "SCHOOL")):
            funnel[LOCAL]["recurring_buyers_needing_registration"] += 1

    def funnel_row(counter: Counter) -> dict[str, int]:
        keys = [
            "raw_live",
            "tangible_products",
            "access_checked",
            "access_yes",
            "inaccessible",
            "access_unknown_or_conditional",
            "easy_registration_only",
            "real_eligibility_blockers",
            "recurring_buyers_needing_registration",
            "economics_researched",
            "identity_resolved",
            "historical_pricing_found",
            "offer_count_found",
            "retail_price_found",
            "gross_spread_ge_10k",
            "net_ge_10k",
            "net_ge_25k",
            "net_ge_50k",
            "net_ge_75k",
            "net_ge_100k",
        ]
        return {k: int(counter.get(k, 0)) for k in keys}

    return {
        "funnel": {
            "FEDERAL": funnel_row(funnel[FEDERAL]),
            "STATE": funnel_row(funnel[STATE]),
            "LOCAL": funnel_row(funnel[LOCAL]),
            "COOPERATIVE": funnel_row(funnel[COOPERATIVE]),
            "TOTAL": funnel_row(funnel["TOTAL"]),
        },
        "open_offer_distribution": dict(open_offer_dist),
        "prefiltered_offer_distribution": dict(prefiltered_offer_dist),
        "primary_blockers": blockers.most_common(15),
        "registration_actions": registration_actions,
        "recurring_buyers": recurring_portals,
        "normalized": normalized_rows,
        "accessible": accessible_sorted,
        "top20": accessible_sorted[:20],
        "research_queue": research_sorted[:50],
        "counts": {
            "raw": len(rows),
            "normalized": len(normalized_rows),
            "access_yes": len(accessible_sorted),
            "research_queue": len(research_sorted),
            "ready_for_owner_review": sum(
                1 for r in accessible_sorted if r.get("actionable_state") == READY_FOR_OWNER_REVIEW
            ),
            "economic_passes": sum(1 for r in accessible_sorted if r.get("meets_floor")),
            "economics_researched": int(funnel["TOTAL"].get("economics_researched", 0)),
            "easy_registration_only": int(funnel["TOTAL"].get("easy_registration_only", 0)),
            "registration_actions": len(registration_actions),
            "recurring_buyers": len(recurring_portals),
        },
    }


def source_mix_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(infer_source_level(r) for r in rows)
    local_coop = counts.get(LOCAL, 0) + counts.get(COOPERATIVE, 0)
    shortfalls = []
    if counts.get(FEDERAL, 0) < TARGET_FEDERAL:
        shortfalls.append(
            {
                "level": FEDERAL,
                "target": TARGET_FEDERAL,
                "actual": counts.get(FEDERAL, 0),
                "shortfall": TARGET_FEDERAL - counts.get(FEDERAL, 0),
            }
        )
    if counts.get(STATE, 0) < TARGET_STATE:
        shortfalls.append(
            {
                "level": STATE,
                "target": TARGET_STATE,
                "actual": counts.get(STATE, 0),
                "shortfall": TARGET_STATE - counts.get(STATE, 0),
            }
        )
    if local_coop < TARGET_LOCAL_COOP:
        shortfalls.append(
            {
                "level": "LOCAL+COOPERATIVE",
                "target": TARGET_LOCAL_COOP,
                "actual": local_coop,
                "shortfall": TARGET_LOCAL_COOP - local_coop,
            }
        )
    total = sum(counts.values())
    if total < TARGET_TOTAL:
        shortfalls.append(
            {
                "level": "TOTAL",
                "target": TARGET_TOTAL,
                "actual": total,
                "shortfall": TARGET_TOTAL - total,
            }
        )
    return {
        "counts": dict(counts),
        "local_cooperative_combined": local_coop,
        "total": total,
        "targets": {
            "total": TARGET_TOTAL,
            "federal": TARGET_FEDERAL,
            "state": TARGET_STATE,
            "local_cooperative": TARGET_LOCAL_COOP,
        },
        "shortfalls": shortfalls,
        "targets_met": not shortfalls,
    }


def run_phase_l_hunt(
    *,
    authorize_live: bool = True,
    max_sources: int | None = 40,
    seed_rows: list[dict[str, Any]] | None = None,
    profile: str = "broad",
) -> dict[str, Any]:
    discovered = discover_live_sources(
        authorize_live=authorize_live,
        max_sources=max_sources,
        include_federal_public=True,
        profile=profile,
    )
    rows = list(discovered.get("rows") or [])
    if seed_rows:
        rows.extend(seed_rows)

    # L.11/L.14.1: always merge prior accessible corpus when live crawl under-delivers;
    # never wipe last-known inventory. Label freshness explicitly.
    live_meta = (discovered.get("discovery_meta") or {}).get("live_runner") or {}
    live_unique = int(live_meta.get("unique_records") or 0)
    prior_path = OUT / "accessible_latest.json"
    if prior_path.exists():
        try:
            prior = json.loads(prior_path.read_text(encoding="utf-8"))
            prior_rows = list(prior.get("rows") or [])
            # Merge when live unique is thin OR always preserve prior as LAST_KNOWN when larger
            if len(prior_rows) > live_unique:
                print(
                    f"[phase_l] merging prior accessible={len(prior_rows)} "
                    f"(live_runner unique={live_unique})",
                    flush=True,
                )
                rows.extend(prior_rows)
                discovered.setdefault("discovery_meta", {})["prior_accessible_merged"] = len(prior_rows)
                from phase_l.public_artifact_types import LAST_KNOWN_RECENT, LIVE_FRESH

                discovered["discovery_meta"]["inventory_staleness"] = (
                    LIVE_FRESH if live_unique > 0 else LAST_KNOWN_RECENT
                )
                discovered["discovery_meta"]["live_runner_unique_vs_inventory"] = {
                    "live_runner_unique": live_unique,
                    "prior_accessible": len(prior_rows),
                    "note": "live_runner_unique is NOT total inventory",
                }
                discovered["discovery_meta"]["bidnet_zero_wipe_forbidden"] = True
        except Exception:
            pass

    mix = source_mix_report(rows)
    ranked = screen_and_rank(rows)
    payload = {
        "kind": "PhaseLHuntResult",
        "generated_at": _utc(),
        "phase": "L.1",
        "discovery_meta": discovered.get("discovery_meta"),
        "source_failures": discovered.get("source_failures"),
        "source_mix": mix,
        "funnel": ranked["funnel"],
        "open_offer_distribution": ranked["open_offer_distribution"],
        "prefiltered_offer_distribution": ranked["prefiltered_offer_distribution"],
        "primary_blockers": ranked["primary_blockers"],
        "registration_actions": ranked.get("registration_actions") or [],
        "recurring_buyers": ranked.get("recurring_buyers") or [],
        "counts": ranked["counts"],
        "top20": ranked["top20"],
        "research_queue": ranked.get("research_queue") or [],
        "accessible_count": len(ranked["accessible"]),
        "normalized_sample": ranked["normalized"][:50],
        "owner_action_queue": [],
    }

    # Enqueue portal registration actions into existing operator queue (no auto-register)
    try:
        from operator_action_queue import OperatorActionQueue, enqueue_portal_registration_action

        q = OperatorActionQueue()
        for ra in (ranked.get("registration_actions") or [])[:40]:
            enqueue_portal_registration_action(
                q,
                deal={
                    "solicitation_number": ra.get("solicitation"),
                    "title": ra.get("title"),
                    "deal_id": ra.get("solicitation") or ra.get("source_id"),
                },
                portal_source=str(ra.get("source_id") or "portal"),
                registration_url=None,
                registration_action=str(ra.get("action") or REGISTER_BEFORE_BID),
            )
        # Elevate recurring buyers
        for rb in (ranked.get("recurring_buyers") or [])[:20]:
            enqueue_portal_registration_action(
                q,
                deal={"deal_id": rb.get("buyer_id") or rb.get("source_id"), "title": rb.get("buyer_name")},
                portal_source=str(rb.get("portal") or rb.get("source_id") or "portal"),
                registration_url=rb.get("registration_url"),
                registration_action=REGISTER_NOW_RECURRING_BUYER,
                why="Recurring relevant product buyer — register now",
            )
        payload["owner_action_queue"] = q.export()
        _write("registration_actions_latest.json", {"actions": q.export(), "recurring": ranked.get("recurring_buyers")})
    except Exception as exc:  # noqa: BLE001
        payload["owner_action_queue_error"] = str(exc)[:200]

    _write("hunt_latest.json", payload)
    _write(
        "accessible_latest.json",
        {
            "kind": "PhaseLAccessible",
            "count": len(ranked["accessible"]),
            "rows": ranked["accessible"],
        },
    )
    return payload
