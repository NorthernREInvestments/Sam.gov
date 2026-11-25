"""Owner-authorized single-use live pilot — max 10 NextBid credits (Lane A≤6, Lane B≤4).

Does not enable recurring schedules. Always restores killswitch in ``finally``.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from m3.config import get_settings
from m3.research.classify import assess_product_presence, classify_requirement
from m3.research.full_text_store import load_full_text, store_full_text
from m3.research.nextbid_client import NextBidClient
from m3.research.nextbid_controller import (
    LIVE_PILOT_ALLOWED_ENDPOINTS,
    LIVE_PILOT_CREDIT_CAP,
    LIVE_PILOT_SCOPE,
    NextBidBudgetExhausted,
    NextBidUnauthorized,
    acquisition_locked,
    append_ledger,
    controller_status,
    new_job_id,
    refresh_account_usage,
    revoke_owner_authorization,
    write_owner_authorization,
)
from m3.research.nextbid_inventory import load_inventory, upsert_inventory
from m3.research.nextbid_killswitch import clear_emergency_stop, engage_emergency_stop
from m3.research.nextbid_offline_recovery import load_recovery_package
from m3.research.nextbid_procurement_pipeline import (
    extract_from_retained_text,
    plan_cheapest_retrieval,
)
from m3.research.owner_queues import QUEUE_KEYS, empty_queues, queues_root, write_queues
from m3.research.procurement_requirements import (
    assess_reseller_eligibility,
    assess_sourcing_readiness,
    extract_requirements,
)

LANE_A_CAP = 6
LANE_B_CAP = 4
PILOT_NOTE = "owner_live_pilot_10_credit_single_use_20261125"
FEDERAL_SKIP = frozenset({"sam", "dibbs", "sam.gov", "dla", "gsa"})
_SERVICE_TITLE = re.compile(
    r"(?i)\b(services?|consulting|construction|renovation|installation|maintenance|"
    r"janitorial|custodial|staffing|professional\s+services|design[- ]build)\b"
)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(msg: str) -> None:
    print(f"[live-pilot] {msg}", flush=True)


def _report_dir() -> Path:
    p = Path("TEMP") / "LIVE_PILOT_10"
    p.mkdir(parents=True, exist_ok=True)
    return p


def authorize_and_unlock_pilot() -> dict[str, Any]:
    write_owner_authorization(
        authorized=True,
        approved_allocation_calls=LIVE_PILOT_CREDIT_CAP,
        max_additional_calls=LIVE_PILOT_CREDIT_CAP,
        note=PILOT_NOTE,
        scope=LIVE_PILOT_SCOPE,
        test_call_cap=LIVE_PILOT_CREDIT_CAP,
        allowed_endpoints=sorted(LIVE_PILOT_ALLOWED_ENDPOINTS),
        daily_limit_override=LIVE_PILOT_CREDIT_CAP,
        reset_test_used=True,
    )
    os.environ["NEXTBID_ENABLED"] = "true"
    os.environ["NEXTBID_EMERGENCY_STOP"] = "false"
    get_settings.cache_clear()
    clear_emergency_stop(owner_authorized=True)
    assert acquisition_locked() is False, "pilot unlock failed"
    return controller_status()


def relock_pilot(*, reason: str) -> None:
    engage_emergency_stop(reason=reason)
    revoke_owner_authorization()
    os.environ["NEXTBID_ENABLED"] = "false"
    os.environ["NEXTBID_EMERGENCY_STOP"] = "true"
    get_settings.cache_clear()


def _is_physical_product_candidate(title: str, text: str = "") -> bool:
    if _SERVICE_TITLE.search(title or ""):
        return False
    presence = assess_product_presence(title, description=text[:4000] or None)
    if presence.get("notice_type") in {"ASSET_SALE", "FORECAST"}:
        return False
    clf = classify_requirement(title, description=text[:4000] or None)
    req = str(clf.get("requirement_class") or "")
    return req in {
        "product_supply",
        "product_with_incidental_services",
        "mixed_product_service",
        "product_with_significant_installation",
        "unknown_mixed",
    } or presence.get("product_presence") in {"EXPLICIT_PRODUCT", "LIKELY_PRODUCT", "POSSIBLE_PRODUCT"}


def _card_from_opportunity(
    *,
    nb_id: str,
    title: str,
    row: dict[str, Any],
    doc: str,
    desc: str,
    lane: str,
) -> dict[str, Any]:
    req = extract_requirements(title, description=desc or None, document_text=doc or None)
    presence = assess_product_presence(title, description=desc, document_text=doc)
    elig = assess_reseller_eligibility(
        title=title,
        text=f"{desc}\n{doc}"[:8000],
        source=str(row.get("source") or ""),
        evidence_completeness="DOCUMENT" if len(doc) >= 200 else "LISTING",
        product_presence=presence.get("product_presence"),
    )
    sourcing = assess_sourcing_readiness(
        title=title, requirements=req, eligibility_state=elig["eligibility_state"]
    )
    pipeline = extract_from_retained_text(
        title=title,
        description=desc or None,
        document_text=doc or None,
        agency=row.get("buyer") or row.get("agency"),
        solicitation_number=row.get("solicitation_number"),
        official_url=row.get("official_url") or row.get("url"),
        deadline_raw=row.get("deadline_at") or row.get("response_deadline"),
    )
    strict = (
        sourcing.get("sourcing_state") == "SUPPLIER_RESEARCH_READY"
        and int(req.get("complete_line_items") or 0) >= 1
        and sourcing.get("identity_level") in {"EXACT", "SPECIFIED"}
        and elig.get("eligibility_state") != "BLOCKED"
        and (len(doc) + len(desc)) >= 200
    )
    return {
        "nb_id": nb_id,
        "title": title,
        "agency": row.get("buyer") or row.get("agency"),
        "state": row.get("state"),
        "solicitation_number": row.get("solicitation_number"),
        "source": row.get("source"),
        "official_url": row.get("official_url") or row.get("url"),
        "deadline_at": row.get("deadline_at") or row.get("response_deadline"),
        "open_status": "OPEN STATUS UNVERIFIED",
        "lane": lane,
        "eligibility_state": elig.get("eligibility_state"),
        "eligibility_reasons": elig.get("eligibility_reasons"),
        "identity_level": sourcing.get("identity_level"),
        "sourcing_state": sourcing.get("sourcing_state"),
        "line_items": req.get("line_items") or [],
        "complete_line_items": req.get("complete_line_items"),
        "requirements_state": req.get("requirements_state"),
        "quote_ready_blockers": req.get("quote_ready_blockers"),
        "owner_bucket": "supplier_pricing" if strict else "needs_one_more",
        "supplier_quote_ready": strict,
        "pipeline": pipeline,
        "retained_text_chars": len(doc) + len(desc),
        "next_action": (
            "Request supplier quotes for verified line items; confirm lead time and drop-ship."
            if strict
            else "Obtain the specific missing document/fact listed in blockers before quoting."
        ),
        "updated_at": _utc(),
    }


def _select_lane_a_targets(limit: int = 12) -> list[dict[str, Any]]:
    """Fast title/deadline prefilter, then plan_cheapest only for top candidates."""
    inv = load_inventory()
    rough: list[tuple[int, str, dict[str, Any]]] = []
    for nb, row in inv.items():
        src = str(row.get("source") or "").lower()
        if src in FEDERAL_SKIP:
            continue
        title = str(row.get("title") or "")
        if nb == "NB00375309":
            continue  # technical example — do not spend more pilot budget here
        if _SERVICE_TITLE.search(title):
            continue
        if not re.search(
            r"(?i)\b(purchase|furnish|supply|equipment|vehicle|truck|pager|motor|"
            r"salt|propane|generator|trailer|pump|radio|computer|laptop|printer|"
            r"commodit|materials?|goods)\b",
            title,
        ):
            continue
        deadline = str(row.get("deadline_at") or row.get("response_deadline") or "")
        score = 0
        if deadline and deadline[:4] >= "2026":
            score += 15
        if re.search(r"(?i)\b(purchase|furnish)\b", title):
            score += 10
        ft_chars = 0
        try:
            ft = load_full_text(nb) or {}
            ft_chars = int(ft.get("full_text_chars") or len(str(ft.get("full_text") or "")))
        except Exception:
            ft_chars = 0
        if ft_chars >= 8000:
            continue  # already has substantial retained body
        if ft_chars < 200:
            score += 10
        rough.append((score, nb, row))
    rough.sort(key=lambda x: (-x[0], x[1]))

    scored: list[tuple[int, dict[str, Any]]] = []
    for score, nb, row in rough[:80]:
        title = str(row.get("title") or "")
        plan = plan_cheapest_retrieval(nb)
        proposed = plan.get("proposed_metered_call")
        if not proposed:
            continue
        if proposed.get("endpoint") not in {"get_opportunity", "read_file", "list_documents"}:
            continue
        s2 = score
        if proposed.get("endpoint") == "read_file":
            s2 += 30
        elif proposed.get("endpoint") == "get_opportunity":
            s2 += 20
        scored.append(
            (
                s2,
                {
                    "nb_id": nb,
                    "title": title,
                    "row": row,
                    "proposed": proposed,
                    "plan_stop": plan.get("stop_without_call"),
                },
            )
        )
        if len(scored) >= limit:
            break
    scored.sort(key=lambda x: (-x[0], x[1]["nb_id"]))
    return [s[1] for s in scored[:limit]]


def _process_retained(nb_id: str, row: dict[str, Any], *, lane: str) -> dict[str, Any]:
    ft = load_full_text(nb_id) or {}
    pkg = load_recovery_package(nb_id) or {}
    title = str(row.get("title") or ft.get("title") or pkg.get("title") or "")
    doc = str(ft.get("full_text") or pkg.get("document_text") or "")
    desc = str(pkg.get("description") or pkg.get("summary") or "")
    return _card_from_opportunity(nb_id=nb_id, title=title, row=row, doc=doc, desc=desc, lane=lane)


def _execute_proposed(client: NextBidClient, proposed: dict[str, Any]) -> dict[str, Any]:
    ep = proposed["endpoint"]
    nb = proposed["nb_id"]
    if ep == "get_opportunity":
        return {"endpoint": ep, "response": client.get_opportunity(nb)}
    if ep == "read_file":
        return {"endpoint": ep, "response": client.read_file(nb, proposed["path"])}
    if ep == "list_documents":
        return {"endpoint": ep, "response": client.list_documents(nb)}
    if ep == "list_files":
        return {"endpoint": ep, "response": client.list_files(nb)}
    raise NextBidUnauthorized(f"pilot_endpoint_not_scheduled:{ep}")


def _ingest_response(nb_id: str, endpoint: str, response: dict[str, Any], row: dict[str, Any]) -> None:
    """Persist useful text into full_text / inventory without duplicating paid store (already done)."""
    body = response.get("data") if isinstance(response.get("data"), dict) else response
    text_bits: list[str] = []
    if isinstance(body, dict):
        for k in ("description", "summary", "ai_summary", "document_text", "content", "text", "markdown"):
            v = body.get(k)
            if isinstance(v, str) and v.strip():
                text_bits.append(v)
        # read_file bodies often nest
        file_obj = body.get("file") if isinstance(body.get("file"), dict) else None
        if file_obj:
            for k in ("content", "text", "markdown", "body"):
                v = file_obj.get(k)
                if isinstance(v, str) and v.strip():
                    text_bits.append(v)
        opp = body.get("opportunity") if isinstance(body.get("opportunity"), dict) else body
        if isinstance(opp, dict):
            for k in ("description", "summary", "title"):
                v = opp.get(k)
                if isinstance(v, str) and v.strip():
                    text_bits.append(v)
            # merge useful fields into inventory
            patch = {"nb_id": nb_id}
            for fk in ("title", "response_deadline", "deadline_at", "solicitation_number", "official_url", "url"):
                if opp.get(fk):
                    patch[fk if fk != "url" else "official_url"] = opp.get(fk)
            if opp.get("buyer") or opp.get("agency"):
                patch["buyer"] = opp.get("buyer") or opp.get("agency")
            upsert_inventory([patch])
    joined = "\n\n".join(text_bits).strip()
    if len(joined) >= 80:
        prior = load_full_text(nb_id) or {}
        prior_text = str(prior.get("full_text") or "")
        if len(joined) > len(prior_text):
            store_full_text(
                nb_id,
                joined,
                source=f"live_pilot_{endpoint}",
                title=row.get("title"),
            )


def run_lane_a(*, client: NextBidClient, credit_cap: int) -> dict[str, Any]:
    used = 0
    results: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for target in _select_lane_a_targets():
        if used >= credit_cap:
            break
        nb = target["nb_id"]
        proposed = target["proposed"]
        cost = int(proposed.get("max_credit_cost") or 1)
        if used + cost > credit_cap:
            skipped.append({"nb_id": nb, "reason": "would_exceed_lane_a_cap"})
            continue
        # Re-check local evidence immediately before spend
        plan = plan_cheapest_retrieval(nb)
        proposed = plan.get("proposed_metered_call")
        if not proposed:
            skipped.append({"nb_id": nb, "reason": plan.get("stop_without_call") or "no_call"})
            # Still refresh offline card
            results.append(_process_retained(nb, target["row"], lane="A_offline_reuse"))
            continue
        _log(f"LANE A spend {proposed['endpoint']} {nb} ({used + 1}/{credit_cap})")
        try:
            out = _execute_proposed(client, proposed)
            used += cost
            _ingest_response(nb, proposed["endpoint"], out["response"], target["row"])
            card = _process_retained(nb, target["row"], lane="A")
            card["metered_call"] = {
                "endpoint": proposed["endpoint"],
                "path": proposed.get("path"),
                "credits": cost,
            }
            results.append(card)
        except (NextBidBudgetExhausted, NextBidUnauthorized) as exc:
            skipped.append({"nb_id": nb, "reason": str(exc)})
            break
        except Exception as exc:
            skipped.append({"nb_id": nb, "reason": f"{type(exc).__name__}:{exc}"})
            # Fail-fast on unexpected API behavior
            break
    return {"lane": "A", "credits_used": used, "cap": credit_cap, "results": results, "skipped": skipped}


def run_lane_b(*, client: NextBidClient, credit_cap: int) -> dict[str, Any]:
    used = 0
    screened: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    if credit_cap <= 0:
        return {"lane": "B", "credits_used": 0, "cap": 0, "screened": [], "results": [], "skipped": ["no_budget"]}

    params = {
        "tradelines": "commodities",
        "sources": "opengov,bonfire,planetbids,calprocure,publicpurchase,instantmarkets",
        "open": "true",
        "page": 1,
        "page_size": 25,
    }
    _log(f"LANE B search page_size=25 ({used + 1}/{credit_cap})")
    try:
        data = client.search(params)
        used += 1
    except Exception as exc:
        return {
            "lane": "B",
            "credits_used": used,
            "cap": credit_cap,
            "screened": [],
            "results": [],
            "skipped": [{"reason": f"search_failed:{type(exc).__name__}:{exc}"}],
        }

    items = []
    if isinstance(data, dict):
        raw = data.get("data") if isinstance(data.get("data"), (list, dict)) else data
        if isinstance(raw, dict):
            items = raw.get("opportunities") or raw.get("items") or raw.get("results") or []
        elif isinstance(raw, list):
            items = raw
        if not items and isinstance(data.get("opportunities"), list):
            items = data["opportunities"]

    inv = load_inventory()
    product_hits: list[dict[str, Any]] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        nb = str(it.get("nb_id") or it.get("id") or "")
        title = str(it.get("title") or "")
        src = str(it.get("source") or "").lower()
        screened.append({"nb_id": nb, "title": title[:120], "source": src})
        if not nb or src in FEDERAL_SKIP:
            continue
        if nb in inv:
            continue  # dedupe — already inventoried
        if not _is_physical_product_candidate(title, str(it.get("description") or "")):
            continue
        # Persist listing fields without extra spend
        upsert_inventory(
            [
                {
                    "nb_id": nb,
                    "title": title,
                    "source": it.get("source"),
                    "state": it.get("state"),
                    "buyer": it.get("buyer") or it.get("agency"),
                    "deadline_at": it.get("response_deadline") or it.get("deadline_at"),
                    "solicitation_number": it.get("solicitation_number") or it.get("solicitationNumber"),
                    "official_url": it.get("url") or it.get("official_url"),
                    "description": it.get("description") or it.get("summary"),
                }
            ]
        )
        desc = str(it.get("description") or it.get("summary") or "")
        if len(desc) >= 80:
            store_full_text(nb, desc, source="live_pilot_search_listing", title=title)
        product_hits.append({"nb_id": nb, "title": title, "item": it, "desc_chars": len(desc)})

    # Spend remaining Lane B credits only on justified get_opportunity for thin listings
    for hit in product_hits:
        if used >= credit_cap:
            break
        nb = hit["nb_id"]
        if hit["desc_chars"] >= 500:
            # Listing already useful — process offline, no detail spend
            row = load_inventory().get(nb) or hit["item"]
            results.append(_process_retained(nb, row, lane="B_listing_only"))
            continue
        _log(f"LANE B get_opportunity {nb} ({used + 1}/{credit_cap})")
        try:
            resp = client.get_opportunity(nb)
            used += 1
            row = load_inventory().get(nb) or hit["item"]
            _ingest_response(nb, "get_opportunity", resp, row)
            card = _process_retained(nb, load_inventory().get(nb) or row, lane="B")
            card["metered_call"] = {"endpoint": "get_opportunity", "credits": 1}
            results.append(card)
        except (NextBidBudgetExhausted, NextBidUnauthorized) as exc:
            skipped.append({"nb_id": nb, "reason": str(exc)})
            break
        except Exception as exc:
            skipped.append({"nb_id": nb, "reason": f"{type(exc).__name__}:{exc}"})
            break

    return {
        "lane": "B",
        "credits_used": used,
        "cap": credit_cap,
        "listings_returned": len(items or []),
        "product_hits": len(product_hits),
        "screened": screened,
        "results": results,
        "skipped": skipped,
    }


def publish_owner_queues(cards: list[dict[str, Any]]) -> dict[str, int]:
    pricing = [c for c in cards if c.get("supplier_quote_ready")]
    needs = [c for c in cards if not c.get("supplier_quote_ready")]
    # Merge prior offline ready set that still qualifies under same bar
    offline_path = get_settings().staged_inventory_root() / "owner" / "supplier_quote_ready_offline.jsonl"
    if offline_path.exists():
        for line in offline_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            nb = row.get("nb_id")
            if not nb or any(c.get("nb_id") == nb for c in pricing):
                continue
            # Reevaluate quickly from retained text
            inv_row = load_inventory().get(nb) or row
            card = _process_retained(nb, inv_row, lane="offline_reeval")
            if card.get("supplier_quote_ready"):
                pricing.append(card)
            else:
                needs.append(card)

    def _row(c: dict[str, Any]) -> dict[str, Any]:
        lines = c.get("line_items") or []
        return {
            "nb_id": c.get("nb_id"),
            "title": c.get("title"),
            "agency": c.get("agency"),
            "solicitation_number": c.get("solicitation_number"),
            "official_url": c.get("official_url"),
            "deadline_at": c.get("deadline_at"),
            "open_status": c.get("open_status") or "OPEN STATUS UNVERIFIED",
            "line_items": lines,
            "complete_line_items": c.get("complete_line_items"),
            "eligibility_state": c.get("eligibility_state"),
            "sourcing_state": c.get("sourcing_state"),
            "next_action": c.get("next_action"),
            "lane": c.get("lane"),
            "disposition": (
                "verified_bid_opportunities"
                if c.get("supplier_quote_ready")
                else "verified_product_information_needed"
            ),
            "queue": (
                "awaiting_supplier_quotations"
                if c.get("supplier_quote_ready")
                else "missing_document"
            ),
            "unresolved": not bool(c.get("supplier_quote_ready")),
        }

    # Merge into existing queues — never wipe unrelated dispositions.
    merged = empty_queues()
    root = queues_root()
    for qname in QUEUE_KEYS:
        qp = root / f"{qname}.json"
        if qp.exists():
            try:
                prior = json.loads(qp.read_text(encoding="utf-8"))
                merged[qname] = list(prior.get("rows") or [])
            except Exception:
                merged[qname] = []

    def _upsert(qname: str, rows: list[dict[str, Any]]) -> None:
        by_id = {
            str(r.get("nb_id") or r.get("source_record_id") or i): r
            for i, r in enumerate(merged.get(qname) or [])
        }
        for r in rows:
            key = str(r.get("nb_id") or "")
            if key:
                by_id[key] = r
        merged[qname] = list(by_id.values())

    _upsert("awaiting_supplier_quotations", [_row(c) for c in pricing])
    _upsert("verified_bid_opportunities", [_row(c) for c in pricing])
    _upsert("verified_product_information_needed", [_row(c) for c in needs[:50]])
    _upsert("missing_document", [_row(c) for c in needs[:50]])
    _upsert("promising_awaiting_verification", [_row(c) for c in needs[:50]])
    summary = write_queues(merged, build="live_pilot_10")
    out = get_settings().staged_inventory_root() / "owner"
    out.mkdir(parents=True, exist_ok=True)
    (out / "supplier_pricing_live.jsonl").write_text(
        "".join(json.dumps(c, default=str) + "\n" for c in pricing), encoding="utf-8"
    )
    (out / "needs_one_more_live.jsonl").write_text(
        "".join(json.dumps(c, default=str) + "\n" for c in needs[:100]), encoding="utf-8"
    )
    return {"pricing": len(pricing), "needs_one_more": len(needs), **summary}


def run_live_pilot() -> dict[str, Any]:
    """Execute the owner-authorized 10-credit single-use pilot and relock."""
    t0 = datetime.now(timezone.utc)
    report_dir = _report_dir()
    usage_before = refresh_account_usage(allow_when_locked=True)
    before = {
        "remaining": usage_before.get("remaining"),
        "used": usage_before.get("used"),
        "allowance": usage_before.get("allowance"),
    }
    assert int(before["remaining"] or 0) - LIVE_PILOT_CREDIT_CAP >= 1000, "reserve would be breached"
    job_id = new_job_id("live_pilot_10")
    client = NextBidClient(job_id=job_id)
    lane_a: dict[str, Any] = {}
    lane_b: dict[str, Any] = {}
    error: str | None = None
    unlocked = False
    try:
        st = authorize_and_unlock_pilot()
        unlocked = True
        append_ledger(
            {
                "op": "live_pilot_start",
                "job_id": job_id,
                "allocation": LIVE_PILOT_CREDIT_CAP,
                "lane_a_cap": LANE_A_CAP,
                "lane_b_cap": LANE_B_CAP,
                "usage_before": before,
                "controller": {
                    "limits": st.get("limits"),
                    "budget_state": st.get("budget_state"),
                },
            }
        )
        lane_a = run_lane_a(client=client, credit_cap=LANE_A_CAP)
        remaining_total = LIVE_PILOT_CREDIT_CAP - int(lane_a.get("credits_used") or 0)
        lane_b_cap = min(LANE_B_CAP, remaining_total)
        lane_b = run_lane_b(client=client, credit_cap=lane_b_cap)
    except Exception as exc:
        error = f"{type(exc).__name__}:{exc}"
        _log(f"PILOT ERROR {error}")
    finally:
        if unlocked or not acquisition_locked():
            relock_pilot(reason=f"live_pilot_complete:{error or 'ok'}")
        assert acquisition_locked(), "killswitch must be restored"

    usage_after = refresh_account_usage(allow_when_locked=True)
    after = {
        "remaining": usage_after.get("remaining"),
        "used": usage_after.get("used"),
        "allowance": usage_after.get("allowance"),
    }
    credits_consumed = None
    if before.get("remaining") is not None and after.get("remaining") is not None:
        credits_consumed = int(before["remaining"]) - int(after["remaining"])

    cards = list(lane_a.get("results") or []) + list(lane_b.get("results") or [])
    queue_summary = publish_owner_queues(cards)

    report = {
        "ok": error is None and acquisition_locked(),
        "error": error,
        "job_id": job_id,
        "started_at": t0.isoformat(),
        "finished_at": _utc(),
        "usage_before": before,
        "usage_after": after,
        "credits_consumed_account": credits_consumed,
        "lane_a_credits": lane_a.get("credits_used"),
        "lane_b_credits": lane_b.get("credits_used"),
        "lane_a": {k: v for k, v in lane_a.items() if k != "results"}
        | {"result_count": len(lane_a.get("results") or [])},
        "lane_b": {k: v for k, v in lane_b.items() if k not in {"results", "screened"}}
        | {
            "result_count": len(lane_b.get("results") or []),
            "screened_count": len(lane_b.get("screened") or []),
        },
        "best_opportunities": [
            {
                "nb_id": c.get("nb_id"),
                "title": c.get("title"),
                "agency": c.get("agency"),
                "lines": [
                    {
                        "qty": li.get("quantity"),
                        "uom": li.get("unit_of_measure"),
                        "desc": (li.get("description") or "")[:80],
                    }
                    for li in (c.get("line_items") or [])[:6]
                ],
                "deadline": c.get("deadline_at"),
                "open_status": c.get("open_status"),
                "supplier_quote_ready": c.get("supplier_quote_ready"),
                "next_action": c.get("next_action"),
                "lane": c.get("lane"),
            }
            for c in sorted(
                cards,
                key=lambda x: (
                    0 if x.get("supplier_quote_ready") else 1,
                    -(int(x.get("complete_line_items") or 0)),
                ),
            )[:15]
        ],
        "queue_summary": queue_summary,
        "acquisition_locked": acquisition_locked(),
        "scheduler_not_activated": True,
        "reserve_floor": 1000,
        "reserve_intact": int(after.get("remaining") or 0) >= 1000,
        "cards": cards,
        "lane_a_full": lane_a,
        "lane_b_full": lane_b,
    }
    (report_dir / "PILOT_REPORT.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    (report_dir / "PILOT_REPORT.md").write_text(
        _render_md(report), encoding="utf-8"
    )
    append_ledger(
        {
            "op": "live_pilot_complete",
            "job_id": job_id,
            "credits_consumed_account": credits_consumed,
            "lane_a": lane_a.get("credits_used"),
            "lane_b": lane_b.get("credits_used"),
            "locked": acquisition_locked(),
            "error": error,
        }
    )
    return report


def _render_md(report: dict[str, Any]) -> str:
    lines = [
        "# M3 Live Pilot Report (10-credit single-use)",
        "",
        f"- OK: **{report.get('ok')}**",
        f"- Credits before/after: {report.get('usage_before')} → {report.get('usage_after')}",
        f"- Account credits consumed: **{report.get('credits_consumed_account')}**",
        f"- Lane A / B: {report.get('lane_a_credits')} / {report.get('lane_b_credits')}",
        f"- Killswitch locked: **{report.get('acquisition_locked')}**",
        f"- Reserve intact (≥1000): **{report.get('reserve_intact')}**",
        "",
        "## Best opportunities",
        "",
        "| Agency / Solicitation | Products | Qty/UOM | Deadline / Status | Supplier Quote | Next Action |",
        "|---|---|---|---|---|---|",
    ]
    for c in report.get("best_opportunities") or []:
        prods = "; ".join(
            f"{x.get('qty')} {x.get('uom')} {(x.get('desc') or '')[:40]}" for x in (c.get("lines") or [])[:3]
        ) or (c.get("title") or "")[:60]
        lines.append(
            f"| {c.get('agency')} / {c.get('nb_id')} | {prods} | "
            f"{(c.get('lines') or [{}])[0].get('qty') if c.get('lines') else '—'} | "
            f"{c.get('deadline')} / {c.get('open_status')} | "
            f"{'YES' if c.get('supplier_quote_ready') else 'NO'} | {c.get('next_action')} |"
        )
    lines.extend(["", f"Error: {report.get('error')}", ""])
    return "\n".join(lines)
