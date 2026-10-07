"""Recover detail / deadline / documents for one BidNet canonical opportunity."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

from application_clock import now_utc
from bidnet_recovery.parse_abstract import parse_bidnet_abstract
from bidnet_recovery.states import (
    AUTH_CHALLENGE,
    AUTH_REQUIRED,
    BLOCKER_EXPIRED,
    DEADLINE_CONFIRMED,
    DEADLINE_NOT_AVAILABLE,
    DEADLINE_UNKNOWN,
    DEFAULT_MAX_DETAIL_ATTEMPTS,
    DEFAULT_MAX_DOCUMENT_ATTEMPTS,
    DETAIL_NOT_AVAILABLE,
    DETAIL_RECOVERED,
    DISCOVERED,
    DOCUMENTS_NOT_AVAILABLE,
    DOCUMENTS_RECOVERED,
    ECONOMICS_READY,
    EXPIRED,
    FREE_PACKAGE_FOUND,
    GOV_VALUE_FOUND,
    NOT_PRODUCT,
    NO_GOV_VALUE_EVIDENCE,
    NO_PUBLIC_COST_EVIDENCE,
    PACKAGE_MATCH_AMBIGUOUS,
    PACKAGE_RECOVERY_RETRYABLE,
    PACKAGE_UNAVAILABLE_FREE,
    PARTIAL_FREE_PACKAGE_FOUND,
    PRODUCT_IDENTIFIED,
    PRODUCT_IDENTITY_UNCLEAR,
    PUBLIC_COST_FOUND,
    RECOVERY_BLOCKED,
    SOURCE_ERROR,
    VALID_FREE_PACKAGE_FOUND,
)

_PACKAGE_OK = {FREE_PACKAGE_FOUND, VALID_FREE_PACKAGE_FOUND, PARTIAL_FREE_PACKAGE_FOUND}
from universe_pass.classify import (
    CONSTRUCTION,
    ELIGIBLE_FOR_PROFIT,
    PURE_SERVICE,
    classify_universe_opportunity,
)

log = logging.getLogger("govtracker.bidnet_recovery")


def _merge_free_docs(rec: dict[str, Any], docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge free-chase documents into attachments_metadata; return newly added."""
    existing = rec.get("attachments_metadata") if isinstance(rec.get("attachments_metadata"), list) else []
    seen = {str(d.get("url") or d.get("document_url")) for d in existing if isinstance(d, dict)}
    added: list[dict[str, Any]] = []
    for d in docs:
        if not isinstance(d, dict):
            continue
        u = d.get("document_url") or d.get("url")
        if not u or str(u) in seen:
            continue
        row = dict(d)
        row.setdefault("url", u)
        existing.append(row)
        seen.add(str(u))
        added.append(row)
    rec["attachments_metadata"] = existing
    return added


def _chase_free_package_or_dead(
    rec: dict[str, Any],
    recovery: dict[str, Any],
    *,
    parsed: dict[str, Any] | None = None,
    blockers: list[str] | None = None,
    store: dict[str, Any] | None = None,
    refresh_overview: bool | None = None,
) -> tuple[list[dict[str, Any]], list[str], str | None]:
    """Chase free public package. No BidNet membership.

    Returns (docs, blockers, chase_status).
    Only PACKAGE_UNAVAILABLE_FREE → economics_dead.
    RETRYABLE / AMBIGUOUS never permanently kill the row.
    """
    from bidnet_recovery.free_package_chase import (
        _overview_text,
        _sol_number,
        candidate_free_urls,
        chase_free_package,
    )

    blockers = list(blockers if blockers is not None else recovery.get("blockers") or [])
    # Drop prior terminal/retry chase markers before re-evaluate
    blockers = [
        b
        for b in blockers
        if b
        not in {
            PACKAGE_UNAVAILABLE_FREE,
            PACKAGE_RECOVERY_RETRYABLE,
            PACKAGE_MATCH_AMBIGUOUS,
            FREE_PACKAGE_FOUND,
            VALID_FREE_PACKAGE_FOUND,
            PARTIAL_FREE_PACKAGE_FOUND,
        }
    ]
    if refresh_overview is None:
        # Skip overview HTTP when no public sol / free URL / existing overview
        refresh_overview = bool(
            _sol_number(rec, parsed)
            or candidate_free_urls(rec, parsed)
            or _overview_text(rec, parsed)
        )
    chase = chase_free_package(
        rec, parsed=parsed, store=store, refresh_overview=bool(refresh_overview)
    )
    recovery["free_package_chase"] = {
        "status": chase.get("status"),
        "document_count": chase.get("document_count"),
        "documents_found": chase.get("documents_found"),
        "document_types": chase.get("document_types"),
        "attempts": chase.get("attempts"),
        "attempt_count": chase.get("attempt_count"),
        "last_attempt_at": chase.get("last_attempt_at"),
        "candidate_urls": chase.get("candidate_urls"),
        "recovery_route": chase.get("recovery_route"),
        "source_url": chase.get("source_url"),
        "matched_source": chase.get("matched_source"),
        "matched_opportunity_id": chase.get("matched_opportunity_id"),
        "confidence": chase.get("confidence"),
        "fingerprint": chase.get("fingerprint"),
        "retry_reasons": chase.get("retry_reasons"),
        "note": chase.get("note"),
        "completed_at": chase.get("completed_at"),
    }
    status = str(chase.get("status") or PACKAGE_RECOVERY_RETRYABLE)
    docs = list(chase.get("documents") or [])
    recovery["membership_used"] = False

    if status in _PACKAGE_OK and docs:
        added = _merge_free_docs(rec, docs)
        recovery["documents"] = docs
        recovery["documents_recovered_count"] = len(docs)
        recovery["free_package_found"] = True
        recovery["economics_dead"] = False
        rec.pop("economics_dead", None)
        rec.pop("economics_dead_reason", None)
        blockers = [
            b
            for b in blockers
            if b not in {AUTH_REQUIRED, DOCUMENTS_NOT_AVAILABLE}
        ]
        recovery["document_source"] = "free_public_chase"
        log.info(
            "free package chase recovered %s docs for %s",
            len(added),
            rec.get("canonical_id") or rec.get("id"),
        )
        return docs, blockers, status

    recovery["free_package_found"] = False
    if status == PACKAGE_UNAVAILABLE_FREE:
        if PACKAGE_UNAVAILABLE_FREE not in blockers:
            blockers.append(PACKAGE_UNAVAILABLE_FREE)
        if DOCUMENTS_NOT_AVAILABLE not in blockers:
            blockers.append(DOCUMENTS_NOT_AVAILABLE)
        recovery["economics_dead"] = True
        recovery["economics_dead_reason"] = PACKAGE_UNAVAILABLE_FREE
        rec["eligible_for_profit_research"] = False
        rec["economics_dead"] = True
        rec["economics_dead_reason"] = PACKAGE_UNAVAILABLE_FREE
        return [], blockers, PACKAGE_UNAVAILABLE_FREE

    # Retryable or ambiguous — never economics_dead
    recovery["economics_dead"] = False
    rec.pop("economics_dead", None)
    rec.pop("economics_dead_reason", None)
    if status == PACKAGE_MATCH_AMBIGUOUS:
        if PACKAGE_MATCH_AMBIGUOUS not in blockers:
            blockers.append(PACKAGE_MATCH_AMBIGUOUS)
        return [], blockers, PACKAGE_MATCH_AMBIGUOUS
    if PACKAGE_RECOVERY_RETRYABLE not in blockers:
        blockers.append(PACKAGE_RECOVERY_RETRYABLE)
    if DOCUMENTS_NOT_AVAILABLE not in blockers:
        blockers.append(DOCUMENTS_NOT_AVAILABLE)
    return [], blockers, PACKAGE_RECOVERY_RETRYABLE


def is_bidnet_rec(rec: dict[str, Any]) -> bool:
    plat = str(rec.get("platform") or "").lower()
    if "bidnet" in plat:
        return True
    rr = rec.get("row_ref") if isinstance(rec.get("row_ref"), dict) else {}
    if "bidnet" in str(rr.get("source_id") or "").lower():
        return True
    url = str(rec.get("authoritative_url") or rr.get("detail_url") or rr.get("source_url") or "")
    return "bidnetdirect.com" in url.lower()


def detail_url_for(rec: dict[str, Any]) -> str | None:
    rr = rec.get("row_ref") if isinstance(rec.get("row_ref"), dict) else {}
    for key in ("authoritative_url",):
        u = rec.get(key)
        if u and str(u).startswith("http"):
            return str(u)
    for key in ("detail_url", "source_url"):
        u = rr.get(key) or rec.get(key)
        if u and str(u).startswith("http"):
            return str(u)
    # attachments_metadata may hold abstract URL
    for att in rec.get("attachments_metadata") or []:
        if isinstance(att, dict) and str(att.get("url") or "").startswith("http"):
            return str(att["url"])
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


def _client():
    from discovery.http_client import PublicProcurementHttpClient, RequestBudget

    budget = RequestBudget(
        max_total_requests=5000,
        max_requests_per_source=50,
        max_pages_per_source=5,
        max_records_per_source=100,
        max_runtime_seconds=3600.0,
        max_retries=1,
        min_interval_seconds=0.4,
        timeout_seconds=28.0,
    )
    return PublicProcurementHttpClient(
        budget=budget,
        authorize_live=True,
        user_agent="M3BidNetRecovery/1.0 (public research)",
    )


def _materially_improved(before: dict[str, Any], parsed: dict[str, Any]) -> bool:
    """DETAIL_RECOVERED requires material improvement over discovery metadata."""
    gains = 0
    if parsed.get("deadline") and not before.get("deadline"):
        gains += 2
    if parsed.get("description") and not before.get("description"):
        gains += 1
    if parsed.get("agency") and not before.get("buyer"):
        gains += 1
    if parsed.get("solicitation_number") and not before.get("solicitation_event_id"):
        gains += 1
    if parsed.get("location") and not before.get("jurisdiction"):
        gains += 1
    if parsed.get("documents"):
        gains += 1
    if parsed.get("overview") and len(str(parsed.get("overview") or "")) > 40:
        gains += 1
    return gains >= 1 and bool(parsed.get("material_improvement"))


def _existing_economics(rec: dict[str, Any]) -> dict[str, Any]:
    rr = rec.get("row_ref") if isinstance(rec.get("row_ref"), dict) else {}
    econ = rr.get("economics") if isinstance(rr.get("economics"), dict) else {}
    pf = rec.get("profit_first") if isinstance(rec.get("profit_first"), dict) else {}
    card = pf.get("owner_card") if isinstance(pf.get("owner_card"), dict) else {}

    def _num(v: Any) -> float | None:
        if v is None or v == "" or str(v).upper() == "UNKNOWN":
            return None
        try:
            return float(str(v).replace(",", "").replace("$", ""))
        except (TypeError, ValueError):
            return None

    gov = _num(econ.get("expected_revenue") or econ.get("historical_award_total") or card.get("expected_revenue"))
    cost = _num(econ.get("acquisition_cost") or econ.get("public_retail_total") or card.get("product_cost"))
    return {
        "gov_value": gov,
        "acquisition_cost": cost,
        "profit_status": pf.get("profit_status"),
        "proof_signals": pf.get("proof_signals") or card.get("proof_signals") or [],
    }


def recover_one(
    cid: str,
    rec: dict[str, Any],
    *,
    client: Any | None = None,
    auth_client: Any | None = None,
    max_detail_attempts: int = DEFAULT_MAX_DETAIL_ATTEMPTS,
    max_document_attempts: int = DEFAULT_MAX_DOCUMENT_ATTEMPTS,
    fetch_live: bool = True,
    html_fixture: str | None = None,
) -> dict[str, Any]:
    """Enrich one BidNet record. Mutates rec in place. Returns recovery telemetry.

    When ``auth_client`` is authenticated, detail HTML is fetched via Playwright session.
    """
    now = now_utc().isoformat()
    recovery = dict(rec.get("bidnet_recovery") or {})
    recovery.setdefault("state", DISCOVERED)
    recovery.setdefault("attempt_count", 0)
    recovery.setdefault("blockers", [])
    recovery["last_recovery_attempt"] = now

    before_cls = str(rec.get("universe_class") or rec.get("product_service_classification") or "")
    before_snapshot = {
        "deadline": rec.get("deadline"),
        "description": rec.get("description"),
        "buyer": rec.get("buyer"),
        "solicitation_event_id": rec.get("solicitation_event_id"),
        "jurisdiction": rec.get("jurisdiction"),
    }

    url = detail_url_for(rec)
    if not url and not html_fixture:
        recovery["state"] = RECOVERY_BLOCKED
        recovery["blockers"] = [DETAIL_NOT_AVAILABLE]
        recovery["blocker"] = DETAIL_NOT_AVAILABLE
        rec["bidnet_recovery"] = recovery
        return {"ok": False, "state": RECOVERY_BLOCKED, "blocker": DETAIL_NOT_AVAILABLE}

    attempts = int(recovery.get("attempt_count") or 0) + 1
    recovery["attempt_count"] = attempts
    if attempts > max_detail_attempts and recovery.get("state") == DISCOVERED:
        recovery["state"] = RECOVERY_BLOCKED
        recovery["blocker"] = DETAIL_NOT_AVAILABLE
        recovery["blockers"] = list(set((recovery.get("blockers") or []) + [DETAIL_NOT_AVAILABLE]))
        rec["bidnet_recovery"] = recovery
        return {"ok": False, "state": RECOVERY_BLOCKED, "blocker": DETAIL_NOT_AVAILABLE}

    # Fetch / parse
    parsed: dict[str, Any]
    http_status = None
    used_auth = False
    if html_fixture is not None:
        parsed = parse_bidnet_abstract(html_fixture, detail_url=url)
        http_status = 200
    elif fetch_live:
        try:
            body = ""
            if auth_client is not None and getattr(auth_client, "is_authenticated", False):
                used_auth = True
                body = auth_client.fetch_html(url)
                http_status = 200
                recovery["fetch_mode"] = "authenticated"
                # Diagnostic markers only (never store full HTML / secrets)
                low = (body or "").lower()
                recovery["fetch_diag"] = {
                    "html_len": len(body or ""),
                    "has_mets_field": "mets-field" in low,
                    "has_member_only": "member-only" in low or "registered members only" in low,
                    "has_closing_date": "closing date" in low,
                    "has_issuing_org": "issuing organization" in low,
                    "has_login": "authentication/login" in low or 'type="password"' in low,
                    "title_snippet": (
                        (__import__("re").search(r"<title>([^<]{0,120})</title>", body or "", __import__("re").I) or [None, ""])[1][:80]
                    ),
                }
                try:
                    recovery["fetch_diag"]["final_url"] = str(getattr(getattr(auth_client, "_page", None), "url", "") or "")[:220]
                except Exception:
                    pass
            else:
                cli = client or _client()
                resp = cli.get(url, source_id=f"bidnet_recovery_{cid[:12]}")
                http_status = resp.status_code
                body = resp.text or ""
                recovery["fetch_mode"] = "anonymous"
                if resp.status_code in {401, 403}:
                    # BidNet wall — chase free public package only (no membership)
                    docs_free, blockers_free, chase_status = _chase_free_package_or_dead(
                        rec, recovery, blockers=[AUTH_REQUIRED]
                    )
                    if chase_status in _PACKAGE_OK and docs_free:
                        recovery["state"] = DOCUMENTS_RECOVERED
                        recovery["blockers"] = blockers_free
                        recovery["blocker"] = None
                        recovery["http_status"] = http_status
                        rec["bidnet_recovery"] = recovery
                        return {
                            "ok": True,
                            "state": DOCUMENTS_RECOVERED,
                            "documents": len(docs_free),
                            "free_package": True,
                            "http_status": http_status,
                            "blockers": blockers_free,
                        }
                    recovery["state"] = RECOVERY_BLOCKED
                    recovery["blocker"] = chase_status or PACKAGE_RECOVERY_RETRYABLE
                    recovery["blockers"] = blockers_free
                    recovery["http_status"] = http_status
                    rec["bidnet_recovery"] = recovery
                    return {
                        "ok": False,
                        "state": RECOVERY_BLOCKED,
                        "blocker": chase_status,
                        "economics_dead": chase_status == PACKAGE_UNAVAILABLE_FREE,
                        "http_status": http_status,
                        "blockers": blockers_free,
                    }
            if body.startswith("AUTH_CHALLENGE") or "AUTH_CHALLENGE:" in str(body)[:40]:
                raise RuntimeError(str(body)[:80])
            parsed = parse_bidnet_abstract(body, detail_url=url)
            # Cache body hash only (not full HTML)
            recovery["detail_content_hash"] = hashlib.sha1(body.encode("utf-8", errors="ignore")).hexdigest()[:16]
        except Exception as exc:
            err_name = type(exc).__name__
            err_msg = str(exc)
            if "AUTH_CHALLENGE" in err_msg:
                log.warning("BidNet AUTH_CHALLENGE during recover %s", cid)
                recovery["state"] = RECOVERY_BLOCKED
                recovery["blocker"] = AUTH_CHALLENGE
                recovery["blockers"] = [AUTH_CHALLENGE]
                recovery["error"] = "AUTH_CHALLENGE"
                rec["bidnet_recovery"] = recovery
                return {
                    "ok": False,
                    "state": RECOVERY_BLOCKED,
                    "blocker": AUTH_CHALLENGE,
                    "auth_challenge": True,
                }
            log.warning("BidNet detail fetch failed %s: %s", cid, err_name)
            recovery["state"] = RECOVERY_BLOCKED
            recovery["blocker"] = SOURCE_ERROR
            recovery["blockers"] = [SOURCE_ERROR]
            recovery["error"] = err_name
            rec["bidnet_recovery"] = recovery
            return {"ok": False, "state": RECOVERY_BLOCKED, "blocker": SOURCE_ERROR}
    else:
        parsed = {"parse_ok": False, "material_improvement": False}

    if not parsed.get("parse_ok"):
        recovery["state"] = RECOVERY_BLOCKED
        recovery["blocker"] = DETAIL_NOT_AVAILABLE
        recovery["blockers"] = [DETAIL_NOT_AVAILABLE]
        recovery["parse_ok"] = False
        recovery["authenticated"] = used_auth
        rec["bidnet_recovery"] = recovery
        return {
            "ok": False,
            "state": RECOVERY_BLOCKED,
            "blocker": DETAIL_NOT_AVAILABLE,
            "authenticated": used_auth,
            "auth_wall": bool(parsed.get("auth_wall")),
            "fetch_diag": recovery.get("fetch_diag"),
        }

    # Apply detail fields
    improved = _materially_improved(before_snapshot, parsed)
    if parsed.get("deadline"):
        rec["deadline"] = parsed["deadline"]
        rr = rec.setdefault("row_ref", {}) if isinstance(rec.get("row_ref"), dict) else {}
        if not isinstance(rec.get("row_ref"), dict):
            rec["row_ref"] = {}
            rr = rec["row_ref"]
        rr["deadline"] = parsed["deadline"]
        recovery["deadline_source"] = "bidnet_public_abstract"
        recovery["deadline_confidence"] = (
            DEADLINE_CONFIRMED
            if parsed.get("timezone_confidence") in {"KNOWN", "EXPLICIT", "HIGH"}
            or parsed.get("close_date_raw")
            else DEADLINE_CONFIRMED  # raw close date from BidNet field is confirmed
        )
        if parsed.get("close_date_raw"):
            recovery["deadline_confidence"] = DEADLINE_CONFIRMED
        recovery["deadline_last_verified"] = now
        recovery["deadline_raw"] = parsed.get("close_date_raw")
    else:
        recovery.setdefault("deadline_confidence", DEADLINE_UNKNOWN)
        recovery.setdefault("blockers", [])
        if DEADLINE_NOT_AVAILABLE not in recovery["blockers"]:
            recovery["blockers"].append(DEADLINE_NOT_AVAILABLE)

    if parsed.get("description") and (
        not rec.get("description") or len(str(parsed["description"])) > len(str(rec.get("description") or ""))
    ):
        rec["description"] = parsed["description"]
        if isinstance(rec.get("row_ref"), dict):
            rec["row_ref"]["description"] = parsed["description"]

    if parsed.get("agency") and not rec.get("buyer"):
        rec["buyer"] = parsed["agency"]
    if parsed.get("location"):
        rec.setdefault("jurisdiction", "LOCAL")
        recovery["location"] = parsed["location"]
    if parsed.get("solicitation_number") and not rec.get("solicitation_event_id"):
        rec["solicitation_event_id"] = parsed["solicitation_number"]
    if parsed.get("issue_date"):
        recovery["issue_date"] = parsed["issue_date"]
    if parsed.get("agency_source_url"):
        recovery["agency_source_url"] = parsed["agency_source_url"]
        # Provenance merge
        prov = list(rec.get("source_provenance") or [])
        prov.append(
            {
                "feed": "bidnet_agency_source",
                "url": parsed["agency_source_url"],
                "source_id": "bidnet_followthrough",
            }
        )
        rec["source_provenance"] = prov

    # Expiry from recovered deadline
    dl = _parse_dt(parsed.get("deadline") or rec.get("deadline"))
    if dl and dl < now_utc():
        rec["freshness"] = "EXPIRED"
        rec["current_funnel_state"] = "EXPIRED"
        recovery["state"] = EXPIRED
        recovery["blocker"] = BLOCKER_EXPIRED
        recovery["last_recovery_success"] = now
        rec["bidnet_recovery"] = recovery
        return {"ok": True, "state": EXPIRED, "expired": True, "improved": improved}

    docs = parsed.get("documents") or []
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

    # Auth wall for deeper fields/docs (authenticated fetch may unlock)
    blockers = list(recovery.get("blockers") or [])
    if used_auth:
        recovery["authenticated"] = True
        # Drop AUTH_REQUIRED if locked fields are gone
        if not parsed.get("auth_wall"):
            blockers = [b for b in blockers if b != AUTH_REQUIRED]
        # Attempt authenticated document byte fetch for discovered URLs
        if docs and auth_client is not None:
            for d in docs:
                u = d.get("document_url")
                if not u:
                    continue
                try:
                    raw = auth_client.download_bytes(u)
                    if raw and len(raw) > 64:
                        try:
                            from bidnet_engine.package_materialization import looks_like_html_bytes

                            if looks_like_html_bytes(raw):
                                d["retrieval_status"] = "INVALID_HTML_PAGE"
                                d["byte_size"] = len(raw)
                                continue
                        except Exception:
                            pass
                        d["retrieval_status"] = "DOWNLOADED"
                        d["byte_size"] = len(raw)
                        # Persist under data root attachments cache when possible
                        try:
                            from m3_data_root import data_path
                            import hashlib as _hl

                            h = _hl.sha1(u.encode("utf-8")).hexdigest()[:16]
                            name = str(d.get("document_name") or f"bidnet_{h}.bin")
                            safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)[:120]
                            outp = data_path("bidnet_auth", "documents", f"{h}_{safe}")
                            outp.parent.mkdir(parents=True, exist_ok=True)
                            outp.write_bytes(raw)
                            d["local_path"] = str(outp)
                        except Exception:
                            pass
                except Exception:
                    pass
    if parsed.get("auth_wall") and not docs:
        if AUTH_REQUIRED not in blockers:
            blockers.append(AUTH_REQUIRED)
        # Free-only chase: agency / OpenGov / public detail — never BidNet membership
        docs_free, blockers, chase_status = _chase_free_package_or_dead(
            rec, recovery, parsed=parsed, blockers=blockers
        )
        if chase_status in _PACKAGE_OK and docs_free:
            docs = docs_free
            improved = True
        elif DOCUMENTS_NOT_AVAILABLE not in blockers:
            blockers.append(DOCUMENTS_NOT_AVAILABLE)

    state = DISCOVERED
    if improved:
        state = DETAIL_RECOVERED
    if docs:
        state = DOCUMENTS_RECOVERED

    # Reclassify with enriched text — do not preserve contradicted product labels
    cls_before = before_cls
    enrich_rec = dict(rec)
    # Drop prior class so stronger recovered evidence can override
    enrich_rec.pop("product_service_classification", None)
    enrich_rec.pop("universe_class", None)
    cls_result = classify_universe_opportunity(enrich_rec)
    rec["universe_class"] = cls_result["class"]
    rec["product_service_classification"] = cls_result["class"]
    rec["universe_classification"] = cls_result
    rec["eligible_for_profit_research"] = cls_result["eligible_for_profit_research"]
    reclassified = cls_before != cls_result["class"] and bool(cls_before)

    if cls_result["class"] in {PURE_SERVICE, CONSTRUCTION} or not cls_result["eligible_for_profit_research"]:
        if cls_result["class"] in {PURE_SERVICE, CONSTRUCTION}:
            state = NOT_PRODUCT
            recovery["state"] = NOT_PRODUCT
            recovery["blockers"] = blockers
            recovery["last_recovery_success"] = now
            recovery["reclassified"] = reclassified
            recovery["class_before"] = cls_before
            recovery["class_after"] = cls_result["class"]
            rec["bidnet_recovery"] = recovery
            return {
                "ok": True,
                "state": NOT_PRODUCT,
                "reclassified": reclassified,
                "class_before": cls_before,
                "class_after": cls_result["class"],
                "improved": improved,
                "deadline_recovered": bool(parsed.get("deadline")),
                "documents": len(docs),
            }

    # Product identity — title/overview clues; full CLIN needs documents
    identity_bits = []
    blob = f"{rec.get('title') or ''} {rec.get('description') or ''}"
    if re_search_part(blob):
        identity_bits.append("part_or_model_clue")
    if docs:
        identity_bits.append("documents_present")
    if rec.get("description") and len(str(rec.get("description"))) > 60:
        identity_bits.append("description_present")

    product_identified = bool(identity_bits) and cls_result["class"] in ELIGIBLE_FOR_PROFIT
    if product_identified and state in {DETAIL_RECOVERED, DOCUMENTS_RECOVERED}:
        # Prefer DOCUMENTS_RECOVERED order; product id can follow detail
        if state == DETAIL_RECOVERED or state == DOCUMENTS_RECOVERED:
            state = PRODUCT_IDENTIFIED if state == DETAIL_RECOVERED or docs else state
            if state == DOCUMENTS_RECOVERED:
                state = PRODUCT_IDENTIFIED
        recovery["product_identity"] = {"clues": identity_bits, "method": "text_heuristic"}
    elif cls_result["class"] in ELIGIBLE_FOR_PROFIT:
        if PRODUCT_IDENTITY_UNCLEAR not in blockers:
            blockers.append(PRODUCT_IDENTITY_UNCLEAR)

    # Existing economics evidence (do not invent; only attach if present)
    ev = _existing_economics(rec)
    gov_found = ev["gov_value"] is not None
    cost_found = ev["acquisition_cost"] is not None
    if gov_found and state in {PRODUCT_IDENTIFIED, DOCUMENTS_RECOVERED, DETAIL_RECOVERED}:
        state = GOV_VALUE_FOUND
    elif not gov_found and product_identified:
        if NO_GOV_VALUE_EVIDENCE not in blockers:
            blockers.append(NO_GOV_VALUE_EVIDENCE)

    if cost_found:
        if state == GOV_VALUE_FOUND or gov_found:
            state = PUBLIC_COST_FOUND if not gov_found else PUBLIC_COST_FOUND
        elif product_identified:
            state = PUBLIC_COST_FOUND
    elif product_identified:
        if NO_PUBLIC_COST_EVIDENCE not in blockers:
            blockers.append(NO_PUBLIC_COST_EVIDENCE)

    if gov_found and cost_found:
        state = ECONOMICS_READY
        # Route through profit-first without inventing numbers
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
                ranking_signals={"exact_identity": bool(identity_bits)},
            )
            econ = pev.get("economics") or {}
            rec["profit_first"] = {
                "profit_status": econ.get("profit_status"),
                "expected_profit": econ.get("expected_profit"),
                "post_financing_profit": econ.get("post_financing_profit"),
                "route": pev.get("route"),
                "owner_card": pev.get("owner_card"),
                "proof_signals": econ.get("proof_signals"),
                "research_priority": (pev.get("research") or {}).get("priority"),
                "evaluated_at": pev.get("evaluated_at"),
                "bidnet_recovery": True,
            }
            recovery["profit_status"] = econ.get("profit_status")
        except Exception:
            log.exception("profit_first eval failed %s", cid)

    # If auth blocks documents and free chase failed → economics-dead
    if (
        state in {DETAIL_RECOVERED, PRODUCT_IDENTIFIED, DISCOVERED}
        and not docs
        and PACKAGE_UNAVAILABLE_FREE in blockers
    ):
        recovery["document_blocker"] = PACKAGE_UNAVAILABLE_FREE
        recovery["economics_dead"] = True
        if not improved and not parsed.get("deadline"):
            state = RECOVERY_BLOCKED
            recovery["blocker"] = PACKAGE_UNAVAILABLE_FREE
    elif (
        state in {DETAIL_RECOVERED, PRODUCT_IDENTIFIED}
        and AUTH_REQUIRED in blockers
        and not docs
        and attempts >= max_document_attempts
        and not (gov_found and cost_found)
    ):
        # Free chase not yet run or still only AUTH_REQUIRED context
        recovery["document_blocker"] = AUTH_REQUIRED

    if not improved and state == DISCOVERED:
        recovery["state"] = RECOVERY_BLOCKED
        recovery["blocker"] = DETAIL_NOT_AVAILABLE
        blockers.append(DETAIL_NOT_AVAILABLE)
    else:
        recovery["state"] = state

    recovery["blockers"] = blockers
    recovery["blocker"] = blockers[0] if blockers and state != ECONOMICS_READY else recovery.get("blocker")
    recovery["last_recovery_success"] = now
    recovery["reclassified"] = reclassified
    recovery["class_before"] = cls_before
    recovery["class_after"] = cls_result["class"]
    recovery["http_status"] = http_status
    recovery["detail_url"] = url
    rec["bidnet_recovery"] = recovery
    rec["discovery_freshness_state"] = (
        "LIVE_CONFIRMED" if parsed.get("deadline") and state != EXPIRED else rec.get("discovery_freshness_state")
    )
    if parsed.get("deadline") and state != EXPIRED:
        rec["freshness"] = "LIVE"
        rec["freshness_confidence"] = "CONFIRMED"

    return {
        "ok": True,
        "state": recovery["state"],
        "improved": improved,
        "deadline_recovered": bool(parsed.get("deadline")),
        "documents": len(docs),
        "reclassified": reclassified,
        "class_before": cls_before,
        "class_after": cls_result["class"],
        "gov_value": gov_found,
        "public_cost": cost_found,
        "economics_ready": recovery["state"] == ECONOMICS_READY,
        "blockers": blockers,
        "auth_wall": bool(parsed.get("auth_wall")),
        "authenticated": used_auth,
        "issuing_org": bool(parsed.get("agency")),
        "solicitation_number": bool(parsed.get("solicitation_number")),
        "agency_source_url": bool(parsed.get("agency_source_url")),
    }


def re_search_part(blob: str) -> bool:
    import re

    return bool(
        re.search(
            r"\b(NSN|P/?N|MPN|model|part\s*#|SKU|catalog|OEM|brand[\-\s]?name|"
            r"poly\s+tubing|valve|coupling|equipment|supply|supplies|materials?)\b",
            blob or "",
            re.I,
        )
    )


def recovery_priority_tier(rec: dict[str, Any]) -> int:
    """1 = highest priority (new/updated/imminent/missing-docs); 5 = backlog; 9 = skip."""
    title = str(rec.get("title") or "")
    url = detail_url_for(rec) or ""
    sol = rec.get("solicitation_event_id")
    cls = str(rec.get("universe_class") or "")
    br = rec.get("bidnet_recovery") if isinstance(rec.get("bidnet_recovery"), dict) else {}
    if br.get("state") == ECONOMICS_READY:
        return 9
    # Free chase exhausted — no membership path; skip forever
    if br.get("economics_dead") or PACKAGE_UNAVAILABLE_FREE in set(br.get("blockers") or []):
        return 9
    if rec.get("economics_dead"):
        return 9

    # Tier 1: newly discovered / never recovered / auth-blocked product
    state = br.get("state") or DISCOVERED
    blockers = set(br.get("blockers") or [])
    docs_meta = rec.get("attachments_metadata") or []
    real_docs = [
        d
        for d in docs_meta
        if isinstance(d, dict)
        and d.get("document_type") in {"attachment", "linked"}
        and "abstract" not in str(d.get("document_url") or d.get("url") or "").lower()
    ]
    if state == DISCOVERED or not br:
        score = 1
    elif AUTH_REQUIRED in blockers or (cls in ELIGIBLE_FOR_PROFIT and not real_docs):
        score = 1
    else:
        score = 3

    # Tier 1–2: imminent deadline
    dl = _parse_dt(rec.get("deadline") or br.get("deadline_raw"))
    if dl:
        hours = (dl - now_utc()).total_seconds() / 3600.0
        if 0 < hours <= 72:
            score = min(score, 1)
        elif 0 < hours <= 24 * 14:
            score = min(score, 2)

    # Recently updated / amended signals
    updated = rec.get("updated_at") or rec.get("last_seen_at") or (rec.get("row_ref") or {}).get("updated_at")
    if updated:
        udt = _parse_dt(updated)
        if udt and (now_utc() - udt).total_seconds() < 72 * 3600:
            score = min(score, 1)

    if "amend" in title.lower() or "addendum" in title.lower():
        score = min(score, 1)

    if url and sol:
        score = min(score, 2)
    if cls in ELIGIBLE_FOR_PROFIT and url:
        score = min(score, 2)
    if re_search_part(title) and url:
        score = min(score, 1)
    if "schedule" in title.lower() or "bid sheet" in title.lower() or "line item" in title.lower():
        score = min(score, 1)

    # Older backlog
    if score >= 3 and state not in {DISCOVERED}:
        score = 5
    return score
