"""Recover detail/documents for one OpenGov canonical opportunity."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

from application_clock import now_utc
from bidnet_recovery.states import (
    AUTH_CHALLENGE,
    AUTH_REQUIRED,
    BLOCKER_EXPIRED,
    DEADLINE_CONFIRMED,
    DEADLINE_NOT_AVAILABLE,
    DETAIL_NOT_AVAILABLE,
    DETAIL_RECOVERED,
    DOCUMENTS_NOT_AVAILABLE,
    DOCUMENTS_RECOVERED,
    ECONOMICS_READY,
    EXPIRED,
    GOV_VALUE_FOUND,
    NOT_PRODUCT,
    NO_GOV_VALUE_EVIDENCE,
    NO_PUBLIC_COST_EVIDENCE,
    PRODUCT_IDENTIFIED,
    PRODUCT_IDENTITY_UNCLEAR,
    PUBLIC_COST_FOUND,
    RECOVERY_BLOCKED,
    SOURCE_ERROR,
    DISCOVERED,
)
from opengov_discovery.parse import parse_detail_html
from universe_pass.classify import (
    CONSTRUCTION,
    ELIGIBLE_FOR_PROFIT,
    PURE_SERVICE,
    classify_universe_opportunity,
)

log = logging.getLogger("govtracker.opengov_recovery")


def is_opengov_rec(rec: dict[str, Any]) -> bool:
    plat = str(rec.get("platform") or rec.get("platform_family") or "").lower()
    if "opengov" in plat:
        return True
    rr = rec.get("row_ref") if isinstance(rec.get("row_ref"), dict) else {}
    if "opengov" in str(rr.get("source_id") or "").lower():
        return True
    url = str(rec.get("authoritative_url") or rr.get("detail_url") or rr.get("source_url") or "")
    return "opengov.com" in url.lower()


def detail_url_for(rec: dict[str, Any]) -> str | None:
    rr = rec.get("row_ref") if isinstance(rec.get("row_ref"), dict) else {}
    for key in ("authoritative_url", "detail_url", "source_url"):
        u = rec.get(key) or rr.get(key)
        if u and str(u).startswith("http"):
            return str(u)
    return None


def _parse_dt(val: Any) -> datetime | None:
    if not val:
        return None
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def recover_one(
    cid: str,
    rec: dict[str, Any],
    *,
    auth_client: Any | None = None,
    fetch_live: bool = True,
    html_fixture: str | None = None,
) -> dict[str, Any]:
    now = now_utc().isoformat()
    recovery = dict(rec.get("opengov_recovery") or rec.get("bidnet_recovery") or {})
    recovery.setdefault("state", DISCOVERED)
    recovery.setdefault("attempt_count", 0)
    recovery.setdefault("blockers", [])
    recovery["last_recovery_attempt"] = now
    recovery["platform"] = "OpenGov"

    url = detail_url_for(rec)
    if not url and not html_fixture:
        recovery["state"] = RECOVERY_BLOCKED
        recovery["blocker"] = DETAIL_NOT_AVAILABLE
        recovery["blockers"] = [DETAIL_NOT_AVAILABLE]
        rec["opengov_recovery"] = recovery
        return {"ok": False, "state": RECOVERY_BLOCKED, "blocker": DETAIL_NOT_AVAILABLE}

    recovery["attempt_count"] = int(recovery.get("attempt_count") or 0) + 1
    used_auth = False
    parsed: dict[str, Any]

    if html_fixture is not None:
        parsed = parse_detail_html(html_fixture, detail_url=url)
    elif fetch_live and auth_client is not None and getattr(auth_client, "is_authenticated", False):
        try:
            used_auth = True
            body = auth_client.fetch_html(url)
            parsed = parse_detail_html(body, detail_url=url)
            recovery["detail_content_hash"] = hashlib.sha1(body.encode("utf-8", errors="ignore")).hexdigest()[:16]
            recovery["fetch_mode"] = "authenticated"
        except Exception as exc:
            if "AUTH_CHALLENGE" in str(exc):
                recovery["state"] = RECOVERY_BLOCKED
                recovery["blocker"] = AUTH_CHALLENGE
                recovery["blockers"] = [AUTH_CHALLENGE]
                rec["opengov_recovery"] = recovery
                return {"ok": False, "state": RECOVERY_BLOCKED, "blocker": AUTH_CHALLENGE, "auth_challenge": True}
            log.warning("OpenGov detail fetch failed %s: %s", cid, type(exc).__name__)
            recovery["state"] = RECOVERY_BLOCKED
            recovery["blocker"] = SOURCE_ERROR
            recovery["blockers"] = [SOURCE_ERROR]
            rec["opengov_recovery"] = recovery
            return {"ok": False, "state": RECOVERY_BLOCKED, "blocker": SOURCE_ERROR}
    elif fetch_live:
        try:
            from discovery.http_client import PublicProcurementHttpClient, RequestBudget

            cli = PublicProcurementHttpClient(
                budget=RequestBudget(
                    max_total_requests=100,
                    max_requests_per_source=20,
                    max_pages_per_source=3,
                    max_records_per_source=50,
                    max_runtime_seconds=600.0,
                    max_retries=1,
                    min_interval_seconds=0.4,
                    timeout_seconds=28.0,
                ),
                authorize_live=True,
                user_agent="M3OpenGovRecovery/1.0",
            )
            resp = cli.get(url, source_id=f"opengov_recovery_{cid[:12]}")
            if resp.status_code in {401, 403}:
                recovery["state"] = RECOVERY_BLOCKED
                recovery["blocker"] = AUTH_REQUIRED
                recovery["blockers"] = [AUTH_REQUIRED]
                rec["opengov_recovery"] = recovery
                return {"ok": False, "state": RECOVERY_BLOCKED, "blocker": AUTH_REQUIRED}
            parsed = parse_detail_html(resp.text or "", detail_url=url)
            recovery["fetch_mode"] = "anonymous"
        except Exception as exc:
            log.warning("OpenGov anonymous fetch failed %s: %s", cid, type(exc).__name__)
            recovery["state"] = RECOVERY_BLOCKED
            recovery["blocker"] = SOURCE_ERROR
            recovery["blockers"] = [SOURCE_ERROR]
            rec["opengov_recovery"] = recovery
            return {"ok": False, "state": RECOVERY_BLOCKED, "blocker": SOURCE_ERROR}
    else:
        parsed = {"parse_ok": False}

    if not parsed.get("parse_ok"):
        recovery["state"] = RECOVERY_BLOCKED
        recovery["blocker"] = DETAIL_NOT_AVAILABLE
        recovery["blockers"] = [DETAIL_NOT_AVAILABLE]
        rec["opengov_recovery"] = recovery
        return {"ok": False, "state": RECOVERY_BLOCKED, "blocker": DETAIL_NOT_AVAILABLE}

    improved = bool(parsed.get("material_improvement"))
    if parsed.get("deadline"):
        rec["deadline"] = parsed["deadline"]
        rr = rec.setdefault("row_ref", {}) if isinstance(rec.get("row_ref"), dict) else {}
        if not isinstance(rec.get("row_ref"), dict):
            rec["row_ref"] = {}
            rr = rec["row_ref"]
        rr["deadline"] = parsed["deadline"]
        recovery["deadline_confidence"] = DEADLINE_CONFIRMED
        recovery["deadline_last_verified"] = now
    else:
        recovery.setdefault("blockers", [])
        if DEADLINE_NOT_AVAILABLE not in recovery["blockers"]:
            recovery["blockers"].append(DEADLINE_NOT_AVAILABLE)

    if parsed.get("description"):
        rec["description"] = parsed["description"]
    if parsed.get("agency") and not rec.get("buyer"):
        rec["buyer"] = parsed["agency"]
    if parsed.get("solicitation_number") and not rec.get("solicitation_event_id"):
        rec["solicitation_event_id"] = parsed["solicitation_number"]
    if parsed.get("location"):
        recovery["location"] = parsed["location"]

    dl = _parse_dt(parsed.get("deadline") or rec.get("deadline"))
    if dl and dl < now_utc():
        rec["freshness"] = "EXPIRED"
        recovery["state"] = EXPIRED
        recovery["blocker"] = BLOCKER_EXPIRED
        rec["opengov_recovery"] = recovery
        return {"ok": True, "state": EXPIRED, "expired": True, "improved": improved}

    docs = parsed.get("documents") or []
    if docs and used_auth and auth_client is not None:
        for d in docs:
            u = d.get("document_url")
            if not u:
                continue
            try:
                raw = auth_client.download_bytes(u)
                if raw and len(raw) > 64:
                    d["retrieval_status"] = "DOWNLOADED"
                    d["byte_size"] = len(raw)
                    d["content_hash"] = hashlib.sha1(raw).hexdigest()[:16]
                    d["retrieved_at"] = now
                    from m3_data_root import data_path

                    h = hashlib.sha1(u.encode("utf-8")).hexdigest()[:16]
                    name = str(d.get("document_name") or f"opengov_{h}.bin")
                    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)[:120]
                    outp = data_path("opengov_auth", "documents", f"{h}_{safe}")
                    outp.parent.mkdir(parents=True, exist_ok=True)
                    outp.write_bytes(raw)
                    d["local_path"] = str(outp)
            except Exception:
                pass

    if docs:
        existing = rec.get("attachments_metadata") if isinstance(rec.get("attachments_metadata"), list) else []
        seen = {str(d.get("url") or d.get("document_url")) for d in existing if isinstance(d, dict)}
        for d in docs:
            u = d.get("document_url")
            if u and u not in seen:
                existing.append(d)
                seen.add(u)
        rec["attachments_metadata"] = existing
        recovery["documents"] = docs
        recovery["documents_recovered_count"] = len(docs)

    blockers = list(recovery.get("blockers") or [])
    if parsed.get("auth_wall") and not docs:
        if AUTH_REQUIRED not in blockers:
            blockers.append(AUTH_REQUIRED)
        if DOCUMENTS_NOT_AVAILABLE not in blockers:
            blockers.append(DOCUMENTS_NOT_AVAILABLE)

    state = DETAIL_RECOVERED if improved else DISCOVERED
    if docs:
        state = DOCUMENTS_RECOVERED

    enrich = dict(rec)
    enrich.pop("product_service_classification", None)
    enrich.pop("universe_class", None)
    cls_result = classify_universe_opportunity(enrich)
    rec["universe_class"] = cls_result["class"]
    rec["product_service_classification"] = cls_result["class"]
    rec["eligible_for_profit_research"] = cls_result["eligible_for_profit_research"]

    if cls_result["class"] in {PURE_SERVICE, CONSTRUCTION}:
        state = NOT_PRODUCT
        recovery["state"] = NOT_PRODUCT
        recovery["blockers"] = blockers
        recovery["last_recovery_success"] = now
        rec["opengov_recovery"] = recovery
        return {"ok": True, "state": NOT_PRODUCT, "improved": improved, "documents": len(docs)}

    if cls_result["class"] in ELIGIBLE_FOR_PROFIT and (docs or improved):
        state = PRODUCT_IDENTIFIED if state in {DETAIL_RECOVERED, DOCUMENTS_RECOVERED} else state
        if docs:
            state = PRODUCT_IDENTIFIED
    elif cls_result["class"] in ELIGIBLE_FOR_PROFIT:
        if PRODUCT_IDENTITY_UNCLEAR not in blockers:
            blockers.append(PRODUCT_IDENTITY_UNCLEAR)

    # Economics handoff — reuse existing engines, no invented prices
    pf = rec.get("profit_first") if isinstance(rec.get("profit_first"), dict) else {}
    card = pf.get("owner_card") if isinstance(pf.get("owner_card"), dict) else {}
    rr = rec.get("row_ref") if isinstance(rec.get("row_ref"), dict) else {}
    econ = rr.get("economics") if isinstance(rr.get("economics"), dict) else {}

    def _num(v: Any) -> float | None:
        if v is None or v == "" or str(v).upper() == "UNKNOWN":
            return None
        try:
            return float(str(v).replace(",", "").replace("$", ""))
        except (TypeError, ValueError):
            return None

    gov = _num(econ.get("expected_revenue") or card.get("expected_revenue"))
    cost = _num(econ.get("acquisition_cost") or econ.get("public_retail_total") or card.get("product_cost"))
    if gov is not None:
        state = GOV_VALUE_FOUND
    elif state == PRODUCT_IDENTIFIED and NO_GOV_VALUE_EVIDENCE not in blockers:
        blockers.append(NO_GOV_VALUE_EVIDENCE)
    if cost is not None:
        state = PUBLIC_COST_FOUND if gov is None else PUBLIC_COST_FOUND
    elif state in {PRODUCT_IDENTIFIED, GOV_VALUE_FOUND} and NO_PUBLIC_COST_EVIDENCE not in blockers:
        blockers.append(NO_PUBLIC_COST_EVIDENCE)
    if gov is not None and cost is not None:
        state = ECONOMICS_READY
        try:
            from profit_first.router import evaluate_opportunity_profit
            from line_item_economics.engine import load_analysis

            lie = load_analysis(cid)
            pev = evaluate_opportunity_profit(
                opportunity_id=cid,
                rec=rec,
                title=rec.get("title"),
                buyer=rec.get("buyer"),
                line_item_analysis=lie,
                ranking_signals={"exact_identity": bool(docs)},
            )
            e = pev.get("economics") or {}
            rec["profit_first"] = {
                "profit_status": e.get("profit_status"),
                "expected_profit": e.get("expected_profit"),
                "post_financing_profit": e.get("post_financing_profit"),
                "route": pev.get("route"),
                "owner_card": pev.get("owner_card"),
                "proof_signals": e.get("proof_signals"),
                "evaluated_at": pev.get("evaluated_at"),
                "opengov_recovery": True,
            }
        except Exception:
            log.exception("profit_first eval failed %s", cid)

    if not improved and state == DISCOVERED:
        recovery["state"] = RECOVERY_BLOCKED
        recovery["blocker"] = DETAIL_NOT_AVAILABLE
    else:
        recovery["state"] = state
    recovery["blockers"] = blockers
    recovery["last_recovery_success"] = now
    recovery["authenticated"] = used_auth
    rec["opengov_recovery"] = recovery
    if parsed.get("deadline") and state != EXPIRED:
        rec["freshness"] = "LIVE"
        rec["discovery_freshness_state"] = "LIVE_CONFIRMED"

    return {
        "ok": True,
        "state": recovery["state"],
        "improved": improved,
        "deadline_recovered": bool(parsed.get("deadline")),
        "documents": len(docs),
        "authenticated": used_auth,
        "issuing_org": bool(parsed.get("agency")),
        "solicitation_number": bool(parsed.get("solicitation_number")),
        "gov_value": gov is not None,
        "public_cost": cost is not None,
        "economics_ready": recovery["state"] == ECONOMICS_READY,
        "blockers": blockers,
        "class_after": cls_result["class"],
    }


def recovery_priority_tier(rec: dict[str, Any]) -> int:
    br = rec.get("opengov_recovery") if isinstance(rec.get("opengov_recovery"), dict) else {}
    if br.get("state") == ECONOMICS_READY:
        return 9
    state = br.get("state") or DISCOVERED
    blockers = set(br.get("blockers") or [])
    docs = rec.get("attachments_metadata") or []
    real_docs = [d for d in docs if isinstance(d, dict) and d.get("document_url")]
    score = 3
    if state == DISCOVERED or AUTH_REQUIRED in blockers:
        score = 1
    if not real_docs and str(rec.get("universe_class") or "") in ELIGIBLE_FOR_PROFIT:
        score = min(score, 1)
    dl = _parse_dt(rec.get("deadline"))
    if dl:
        hours = (dl - now_utc()).total_seconds() / 3600.0
        if 0 < hours <= 72:
            score = min(score, 1)
        elif 0 < hours <= 24 * 14:
            score = min(score, 2)
    updated = rec.get("updated_at") or rec.get("last_seen_at")
    if updated:
        udt = _parse_dt(updated)
        if udt and (now_utc() - udt).total_seconds() < 72 * 3600:
            score = min(score, 1)
    if score >= 3:
        score = 5
    return score
