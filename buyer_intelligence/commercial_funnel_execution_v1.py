"""Permanent commercial funnel execution — 50 live common-product end-to-end test.

Stages: select → package/abstract extract → evidence join → public price (bounded) →
RFQ prep (AUTO_SEND=false) → margin/finance assessment.

Uses existing BidNet session, SPF historical store, commercial evidence index.
No OpenAI. No SAM. No fabricated quotes/financing.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from application_clock import now_utc
from bidnet_discovery.deadline_persist import extract_bidnet_closing_from_html
from bidnet_recovery.parse_abstract import parse_bidnet_abstract
from buyer_intelligence.commercial_evidence_index_v1 import (
    build_commercial_evidence_index,
    lookup_evidence_for_opportunity,
)
from discovery.deadline import normalize_deadline
from m3_data_root import data_path
from phase_l.l23_full_population_funnel import load_store

log = __import__("logging").getLogger("govtracker.commercial_funnel_execution_v1")

BUILD = "20261015-m3-commercial-funnel-execution-v1d"
OUT = "_og_commercial_research_pipeline_out"
FCF_CLASS = f"{OUT}/m3_fcf_v1_classifications.json"
MANIFEST = f"{OUT}/m3_cfe_v1_50_manifest.json"
CHECKPOINT = f"{OUT}/m3_cfe_v1_checkpoint.json"
PROGRESS = f"{OUT}/m3_cfe_v1_progress.json"
RESULTS = f"{OUT}/m3_cfe_v1_results.json"
REPORT = f"{OUT}/m3_cfe_v1_report.json"
RFQ_QUEUE = f"{OUT}/m3_cfe_v1_rfq_queue.json"
LOCK = f"{OUT}/m3_cfe_v1_run.lock"

AUTO_SEND = False
HTTP_TIMEOUT_S = 15.0
ITEM_TIMEOUT_S = 45.0
SAVE_EVERY = 5
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_COMMON = re.compile(
    r"\b(tool|tools|screw|fastener|mro|lubricant|filter|toner|paper|janitorial|"
    r"ppe|glove|hardware|electrical|lighting|lamp|led|office|furniture|automotive|"
    r"windshield|cable|battery|hose|valve|bearing|abrasive|safety)\b",
    re.I,
)
_COMPLEX = re.compile(
    r"\b(construction|renovation|install(ation)?|labor|service\s+only|consulting|"
    r"software\s+license|staffing|custodial\s+service)\b",
    re.I,
)


def _utc() -> str:
    return now_utc().isoformat()


def _save(rel: str, obj: Any) -> Path:
    path = Path(data_path(rel))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)
    return path


def _load(rel: str) -> Any:
    path = Path(data_path(rel))
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _progress(**kwargs: Any) -> None:
    cur = _load(PROGRESS) or {"kind": "CommercialFunnelExecutionProgress", "build": BUILD}
    cur.update(kwargs)
    cur["heartbeat_at"] = _utc()
    _save(PROGRESS, cur)


def _acquire_lock() -> Path | None:
    lock = Path(data_path(LOCK))
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists():
        try:
            if time.time() - lock.stat().st_mtime < 7200:
                return None
        except Exception:
            pass
    lock.write_text(str(os.getpid()), encoding="utf-8")
    return lock


def _release_lock(lock: Path | None) -> None:
    if lock:
        try:
            lock.unlink(missing_ok=True)
        except Exception:
            pass


def _cookies_client() -> httpx.Client:
    client = httpx.Client(
        timeout=HTTP_TIMEOUT_S,
        follow_redirects=True,
        headers={"User-Agent": UA, "Accept": "text/html"},
    )
    try:
        from bidnet_auth.session_store import load_storage_state

        state = load_storage_state() or {}
        for c in state.get("cookies") or []:
            if not isinstance(c, dict):
                continue
            if c.get("name") and c.get("value") is not None:
                try:
                    client.cookies.set(
                        str(c["name"]),
                        str(c["value"]),
                        domain=str(c.get("domain") or "www.bidnetdirect.com"),
                        path=str(c.get("path") or "/"),
                    )
                except Exception:
                    pass
    except Exception:
        pass
    return client


def select_commercial_test_50(*, n: int = 50) -> dict[str, Any]:
    """Select strongest live eligible common-product opportunities from FCF cohort."""
    cls = _load(FCF_CLASS) or {}
    rows = cls.get("rows") or []
    store = load_store()
    scored: list[tuple[float, dict[str, Any]]] = []
    for r in rows:
        if r.get("funnel_class") != "LIVE_ELIGIBLE":
            continue
        if r.get("deadline_status") != "OPEN_CONFIRMED":
            continue
        if r.get("product_class") not in {"PRODUCT", "MIXED", "UNKNOWN_PRODUCTNESS"}:
            continue
        title = str(r.get("product") or "")
        if _COMPLEX.search(title):
            continue
        if not (_COMMON.search(title) or r.get("common_product")):
            continue
        url = str(r.get("authoritative_url") or "")
        if "bidnet" not in url.lower():
            continue
        days = r.get("days_left")
        try:
            dleft = float(days) if days is not None else 0.0
        except Exception:
            dleft = 0.0
        if dleft < 2:
            continue
        score = float(r.get("research_priority_score") or 0)
        if r.get("common_product"):
            score += 20
        if 5 <= dleft <= 45:
            score += 15
        # Prefer shorter titles (less market-basket complexity) but keep baskets
        if len(title) < 80:
            score += 8
        scored.append((score, r))

    scored.sort(key=lambda x: (-x[0], x[1].get("opportunity_id") or ""))
    selected = []
    seen_stems: set[str] = set()
    for sc, r in scored:
        if len(selected) >= n:
            break
        stem = re.sub(r"[^a-z0-9]+", " ", str(r.get("product") or "").lower())
        stem = " ".join(stem.split()[:4])
        if stem in seen_stems and len(selected) < n - 5:
            continue
        seen_stems.add(stem)
        oid = r["opportunity_id"]
        rec = store.get(oid) or {}
        selected.append(
            {
                "opportunity_id": oid,
                "title": r.get("product"),
                "buyer": r.get("buyer"),
                "deadline": r.get("deadline"),
                "days_left": r.get("days_left"),
                "authoritative_url": r.get("authoritative_url") or rec.get("authoritative_url"),
                "selection_score": sc,
                "funnel_class": r.get("funnel_class"),
            }
        )

    man = {
        "kind": "CommercialFunnelExecution50Manifest",
        "build": BUILD,
        "run_id": f"cfe50-{uuid.uuid4().hex[:8]}",
        "created_at": _utc(),
        "AUTO_SEND": AUTO_SEND,
        "requested_n": n,
        "selected_n": len(selected),
        "rows": selected,
        "NOTE": "Fewer than 50 if insufficient viable live common-product evidence.",
    }
    _save(MANIFEST, man)
    return man


def _extract_lines_from_parse(parsed: dict[str, Any], title: str) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    # Prefer structured overview / description bullets
    overview = str(parsed.get("overview") or parsed.get("description") or "")
    for raw_line in overview.split("\n"):
        s = raw_line.strip()
        if len(s) < 8 or len(s) > 240:
            continue
        m = re.match(r"^(\d+(?:\.\d+)?)\s*[xX×]\s+(.+)$", s)
        if m:
            lines.append(
                {
                    "description": m.group(2).strip()[:200],
                    "quantity": m.group(1),
                    "uom": None,
                    "source": "abstract_overview_qty",
                }
            )
            continue
        if re.search(r"\b(ea|each|box|case|gal|lb|kit|set)\b", s, re.I) and len(lines) < 20:
            lines.append({"description": s[:200], "quantity": None, "uom": None, "source": "abstract_overview"})
    if not lines and title:
        lines.append(
            {
                "description": title[:200],
                "quantity": None,
                "uom": None,
                "source": "title_fallback",
                "NOTE": "Title-only — package lines not published on abstract",
            }
        )
    return lines[:40]


def _dec(v: Any) -> Decimal | None:
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError):
        return None


def detect_awardable_scope(
    *,
    title: str = "",
    lines: list[dict[str, Any]] | None = None,
    html_or_notes: str = "",
    package_status: str = "",
) -> dict[str, Any]:
    """Infer award unit: individual line, lot, or all-or-none contract.

    Conservative: without explicit language, multi-line packages are treated as
    complete-package candidates (not automatic line-level resale profit).
    """
    blob = " ".join(
        [
            str(title or ""),
            str(html_or_notes or ""),
            " ".join(_line_desc(li) for li in (lines or [])[:30] if isinstance(li, dict)),
        ]
    ).lower()
    n_lines = sum(
        1
        for li in (lines or [])
        if isinstance(li, dict)
        and li.get("source") != "title_fallback"
        and li.get("LINE_SOURCE_TYPE") != "TITLE_FALLBACK"
    )
    individual = bool(
        re.search(
            r"\b(line[- ]item award|award(ed)? by line|multiple awards? (will be|may be)|"
            r"partial awards?|award(s)? (may|will) be made on (a )?line)\b",
            blob,
        )
    )
    lot = bool(
        re.search(
            r"\b(award(ed)? by lot|lot[- ]level|bid by lot|separate lots?|"
            r"award(s)? (may|will) be made (by|on) lot)\b",
            blob,
        )
    )
    all_or_none = bool(
        re.search(
            r"\b(all[- ]or[- ]none|complete bid required|must bid all|"
            r"single award|one award|entire contract|aggregate award)\b",
            blob,
        )
    )
    if individual and not all_or_none:
        unit = "INDIVIDUAL_LINE"
        complete = n_lines >= 1
    elif lot and not all_or_none:
        unit = "LOT"
        complete = n_lines >= 1
    elif all_or_none or n_lines >= 20:
        unit = "COMPLETE_PACKAGE"
        complete = n_lines >= 1 and bool(package_status)
    elif n_lines > 1:
        unit = "COMPLETE_PACKAGE_ASSUMED"
        complete = n_lines >= 1
    elif n_lines == 1:
        unit = "INDIVIDUAL_LINE"
        complete = True
    else:
        unit = "UNKNOWN"
        complete = False
    return {
        "award_unit": unit,
        "complete_awardable_lot": bool(complete and unit in {"INDIVIDUAL_LINE", "LOT"}),
        "complete_awardable_package": bool(
            complete and unit in {"COMPLETE_PACKAGE", "COMPLETE_PACKAGE_ASSUMED"}
        ),
        "line_count_considered": n_lines,
        "signals": {
            "individual_line": individual,
            "lot": lot,
            "all_or_none": all_or_none,
        },
        "note": (
            "Multi-line solicitations without explicit split-award language are treated "
            "as complete-package economics — line margins are not assumed realizable."
        ),
    }


def assess_economics(row: dict[str, Any]) -> dict[str, Any]:
    """Honest economics — no assumed discounts or financing approval."""
    hist = _dec(row.get("historical_unit_price"))
    pub = _dec(row.get("public_unit_price"))
    qty = _dec(row.get("primary_quantity")) or Decimal("1")
    quote = _dec(row.get("actual_quote_unit_price"))
    scope = row.get("awardable_scope") if isinstance(row.get("awardable_scope"), dict) else {}
    award_unit = str(scope.get("award_unit") or "UNKNOWN")

    acq = quote or pub
    gov = hist
    out: dict[str, Any] = {
        "acquisition_basis": (
            "ACTUAL_QUOTE"
            if quote is not None
            else ("PUBLIC_RETAIL" if pub is not None else None)
        ),
        "government_basis": "HISTORICAL_UNIT" if hist is not None else None,
        "acquisition_unit": str(acq) if acq is not None else None,
        "government_unit": str(gov) if gov is not None else None,
        "quantity_used": str(qty),
        "award_unit": award_unit,
        "economics_basis": (
            "AWARDABLE_LINE"
            if award_unit == "INDIVIDUAL_LINE"
            else (
                "AWARDABLE_LOT"
                if award_unit == "LOT"
                else ("COMPLETE_PACKAGE" if "PACKAGE" in award_unit else "UNSPECIFIED")
            )
        ),
        "estimated_gross_spread": None,
        "estimated_total_profit": None,
        "econ_state": "MARGIN_UNVERIFIED",
        "financeable": False,
        "financing_status": "UNKNOWN",
        "finance_note": "$0 owner cash / no PG / no assumed terms — needs documented supplier or PO funding",
    }
    # For complete-package awards, unit-line spread is only a preliminary indicator.
    preliminary_only = award_unit in {"COMPLETE_PACKAGE", "COMPLETE_PACKAGE_ASSUMED", "UNKNOWN"}
    if acq is not None and gov is not None and gov > acq:
        spread = (gov - acq) * qty
        out["estimated_gross_spread"] = str(spread)
        out["estimated_total_profit"] = str(spread)  # freight/finance unknown
        if preliminary_only and award_unit != "INDIVIDUAL_LINE":
            out["econ_state"] = "PRELIMINARY_MARGIN_PACKAGE_SCOPE"
            out["finance_note"] = (
                out["finance_note"]
                + "; spread uses sample line — full package acquisition not yet verified"
            )
        elif quote is not None:
            out["econ_state"] = "PROFITABLE_BUT_FINANCING_UNVERIFIED"
        else:
            out["econ_state"] = "PROFITABLE_BUT_QUOTE_MISSING"
    elif acq is not None and gov is not None and gov <= acq:
        out["econ_state"] = "NEGATIVE_OR_INSUFFICIENT_MARGIN"
        out["estimated_gross_spread"] = str((gov - acq) * qty)
    elif quote is None and (pub is not None or hist is not None):
        out["econ_state"] = (
            "PROFITABLE_BUT_QUOTE_MISSING"
            if (pub and hist and hist > pub and not preliminary_only)
            else "MARGIN_UNVERIFIED"
        )
    return out


def _execution_environment() -> dict[str, Any]:
    """Where CFE is running — never includes secret values."""
    railway = bool(
        (os.environ.get("RAILWAY_ENVIRONMENT") or "").strip()
        or (os.environ.get("RAILWAY_SERVICE_NAME") or "").strip()
        or (os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or "").strip()
    )
    return {
        "railway": railway,
        "railway_environment": (os.environ.get("RAILWAY_ENVIRONMENT") or "")[:40] or None,
        "railway_service": (os.environ.get("RAILWAY_SERVICE_NAME") or "")[:60] or None,
        "m3_data_root_set": bool((os.environ.get("M3_DATA_ROOT") or "").strip()),
    }


def _open_bidnet_auth_client() -> tuple[dict[str, Any], Any | None]:
    """Reuse production BidNetAuthenticatedClient (same as scheduled discovery).

    Returns (status_dict_without_secrets, client_or_None).
    Caller must close the client.
    """
    from bidnet_auth.client import BidNetAuthenticatedClient
    from bidnet_auth.config import auth_credential_source, load_bidnet_auth_config

    cfg = load_bidnet_auth_config()
    cred_src = auth_credential_source()
    env = _execution_environment()
    status: dict[str, Any] = {
        "auth_enabled": bool(cfg.auth_enabled),
        "credentials_present": bool(cfg.credentials_present),
        "credential_source": cred_src.get("credential_source"),
        "env_username_present": cred_src.get("env_username_present"),
        "env_password_present": cred_src.get("env_password_present"),
        "portal_store_credentials_present": cred_src.get("portal_store_credentials_present"),
        "authenticated": False,
        "status": None,
        "external_blocker": None,
        "message": None,
        "execution_environment": env,
        "auth_client": "BidNetAuthenticatedClient",
    }
    if not cfg.auth_enabled:
        status["status"] = "DISABLED"
        status["message"] = "BIDNET_AUTH_ENABLED=false"
        return status, None

    if not cfg.credentials_present:
        # Distinguish local Cursor runs (no Railway env) from true production misconfig
        if not env["railway"] and not cred_src.get("portal_store_credentials_present"):
            status["status"] = "LOCAL_ENV_WITHOUT_PRODUCTION_CREDENTIALS"
            status["external_blocker"] = "LOCAL_ENV_WITHOUT_PRODUCTION_CREDENTIALS"
            status["message"] = (
                "CFE is running outside Railway without BIDNET_USERNAME/BIDNET_PASSWORD. "
                "Production already has those vars — run CFE via railway run/ssh, not bare local python."
            )
            return status, None

    client = BidNetAuthenticatedClient(cfg)
    try:
        result = client.ensure_authenticated()
        status["status"] = result.status
        status["authenticated"] = bool(result.authenticated)
        status["message"] = str(result.message or "")[:200]
        status["session_reused"] = bool(result.reused_session)
        if not result.authenticated:
            msg = str(result.message or "")
            try:
                from bidnet_auth.auth_lifecycle_v1 import classify_auth_result_message

                status["failure_class"] = classify_auth_result_message(
                    status=str(result.status), message=msg
                )
            except Exception:
                status["failure_class"] = None
            if "EXTERNAL_ACCOUNT_CREDENTIALS_REQUIRED" in msg:
                status["external_blocker"] = "EXTERNAL_ACCOUNT_CREDENTIALS_REQUIRED"
            elif result.status == "AUTH_CHALLENGE":
                status["external_blocker"] = "EXTERNAL_CAPTCHA_HUMAN_ACTION_REQUIRED"
            elif status.get("failure_class"):
                status["external_blocker"] = status["failure_class"]
            else:
                status["external_blocker"] = str(result.status)
            client.close()
            return status, None
        return status, client
    except Exception as exc:
        try:
            client.close()
        except Exception:
            pass
        try:
            from bidnet_auth.auth_lifecycle_v1 import (
                classify_browser_exception,
                force_cleanup_orphaned_chromium,
                format_browser_unavailable,
            )

            force_cleanup_orphaned_chromium(max_kill=4)
            klass = classify_browser_exception(exc)
            status["status"] = klass
            status["external_blocker"] = klass
            status["failure_class"] = klass
            status["message"] = format_browser_unavailable(exc)[:200]
        except Exception:
            status["status"] = "AUTH_PROBE_FAILED"
            status["external_blocker"] = "AUTH_PROBE_FAILED"
            status["message"] = type(exc).__name__
        return status, None


def _line_qty(li: dict[str, Any]) -> Any:
    if not isinstance(li, dict):
        return None
    qty = li.get("quantity")
    if qty not in (None, ""):
        return qty
    q = li.get("QTY")
    if isinstance(q, dict):
        return q.get("VALUE")
    return q


def _line_desc(li: dict[str, Any]) -> str:
    if not isinstance(li, dict):
        return ""
    d = li.get("description")
    if d in (None, "") and li.get("DESCRIPTION") not in (None, ""):
        d = li.get("DESCRIPTION")
    if isinstance(d, dict):
        d = d.get("VALUE") or d.get("text") or d.get("raw") or ""
    return str(d or "").strip()


def _line_mpn(li: dict[str, Any]) -> str:
    if not isinstance(li, dict):
        return ""
    for key in ("MPN", "mpn", "part_number", "PART_NUMBER", "catalog_number", "CATALOG_NUMBER", "model", "MODEL"):
        v = li.get(key)
        if isinstance(v, dict):
            v = v.get("VALUE") or v.get("text") or ""
        s = str(v or "").strip()
        if len(s) >= 3:
            return s
    return ""


def _line_mfr(li: dict[str, Any]) -> str:
    if not isinstance(li, dict):
        return ""
    for key in ("MANUFACTURER", "manufacturer", "BRAND", "brand", "MFR", "mfr"):
        v = li.get(key)
        if isinstance(v, dict):
            v = v.get("VALUE") or v.get("text") or ""
        s = str(v or "").strip()
        if s:
            return s
    return ""


def _research_public_prices_for_lines(
    *,
    opportunity_id: str,
    lines: list[dict[str, Any]],
    budget_s: float = 40.0,
    max_lines: int = 3,
) -> dict[str, Any]:
    """Bounded public acquisition-price research for extracted product lines.

    Uses existing commercial_gap_closure / public_price_search. Never invents prices.
    """
    t0 = time.time()
    researched: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    distributors: list[dict[str, Any]] = []
    try:
        from buyer_intelligence.commercial_gap_closure_v1 import research_mpn_public_price
        from public_price_search.models import PUBLIC_PRICE_FOUND, PUBLIC_PRICE_PARTIAL
        from public_price_search.resolver import resolve_public_price
    except Exception as exc:
        return {
            "ok": False,
            "error": type(exc).__name__,
            "prices": [],
            "failures": [{"failure_class": "IMPORT_ERROR", "detail": str(exc)[:120]}],
            "distributors": [],
            "verified_public_unit_price": None,
        }

    # Prefer MPN lines, then long descriptions
    ranked = sorted(
        [li for li in lines if isinstance(li, dict)],
        key=lambda li: (
            0 if _line_mpn(li) else 1,
            0 if len(_line_desc(li)) >= 24 else 1,
            -len(_line_desc(li)),
        ),
    )
    for li in ranked[: max(1, int(max_lines))]:
        if time.time() - t0 >= budget_s:
            failures.append({"failure_class": "BUDGET_EXCEEDED"})
            break
        mpn = _line_mpn(li)
        desc = _line_desc(li)
        mfr = _line_mfr(li)
        qty = _line_qty(li)
        remain = max(5.0, budget_s - (time.time() - t0))
        hit: dict[str, Any] | None = None
        if mpn:
            try:
                pr = research_mpn_public_price(
                    mpn=mpn,
                    manufacturer=mfr or None,
                    description=desc or None,
                    opportunity_id=opportunity_id,
                    qty=qty,
                    deadline_s=min(35.0, remain),
                )
                if pr.get("unit_price"):
                    hit = {
                        "mpn": mpn,
                        "description": desc[:160] or None,
                        "manufacturer": mfr or None,
                        "unit_price": pr.get("unit_price"),
                        "uom": pr.get("uom"),
                        "pack_qty": pr.get("pack_qty"),
                        "url": pr.get("url"),
                        "supplier": pr.get("supplier"),
                        "class": pr.get("class"),
                        "route": pr.get("route"),
                        "newly_acquired": pr.get("newly_acquired"),
                        "qty": qty,
                    }
                else:
                    failures.append(
                        {
                            "mpn": mpn,
                            "status": pr.get("status"),
                            "failure_class": pr.get("failure_class"),
                        }
                    )
            except Exception as exc:
                failures.append({"mpn": mpn, "failure_class": type(exc).__name__})
        elif len(desc) >= 24:
            try:
                identity = {
                    "part_number": None,
                    "manufacturer": mfr or None,
                    "raw_description": desc[:200],
                    "model": None,
                }
                with httpx.Client(timeout=12.0, follow_redirects=True) as client:
                    result = resolve_public_price(
                        identity,
                        opportunity_id=opportunity_id,
                        client=client,
                        max_queries=2,
                        max_pages=3,
                        use_budget=True,
                    )
                status = result.get("status")
                ev = result.get("evidence") or {}
                if status in {PUBLIC_PRICE_FOUND, PUBLIC_PRICE_PARTIAL} and ev.get("unit_price"):
                    hit = {
                        "mpn": None,
                        "description": desc[:160],
                        "manufacturer": mfr or None,
                        "unit_price": str(ev.get("unit_price")),
                        "uom": ev.get("uom"),
                        "pack_qty": ev.get("pack_qty"),
                        "url": ev.get("url") or ev.get("source_url"),
                        "supplier": ev.get("seller") or ev.get("seller_class"),
                        "class": "DESCRIPTION_PUBLIC_PRICE",
                        "route": "resolve_public_price_description",
                        "newly_acquired": True,
                        "qty": qty,
                    }
                else:
                    failures.append(
                        {
                            "description": desc[:80],
                            "status": status,
                            "failure_class": "NO_PUBLIC_PRICE",
                        }
                    )
            except Exception as exc:
                failures.append({"description": desc[:80], "failure_class": type(exc).__name__})
        else:
            failures.append({"failure_class": "PRODUCT_IDENTITY_INSUFFICIENT"})

        if hit:
            researched.append(hit)
            if hit.get("supplier"):
                distributors.append(
                    {
                        "name": str(hit["supplier"])[:120],
                        "type": "distributor",
                        "basis": "verified_public_price",
                        "url": (hit.get("url") or "")[:220] or None,
                        "unit_price": hit.get("unit_price"),
                    }
                )

    verified = researched[0]["unit_price"] if researched else None
    return {
        "ok": bool(researched),
        "prices": researched,
        "failures": failures[:12],
        "distributors": distributors,
        "verified_public_unit_price": verified,
        "elapsed_s": round(time.time() - t0, 2),
        "price_evidence_count": len(researched),
    }


def _prior_commercial_resume(item: dict[str, Any]) -> dict[str, Any]:
    """Durable extract evidence from a prior checkpoint — skip re-auth/docs when valid."""
    prior = item.get("_prior_checkpoint") if isinstance(item.get("_prior_checkpoint"), dict) else {}
    lines = item.get("line_items") or prior.get("line_items") or []
    docs = item.get("recovered_documents") or prior.get("recovered_documents") or []
    if not isinstance(lines, list):
        lines = []
    if not isinstance(docs, list):
        docs = []
    complete = bool(item.get("complete_spec") or prior.get("complete_spec"))
    docs_n = int(
        item.get("documents_downloaded")
        or prior.get("documents_downloaded")
        or sum(
            1
            for d in docs
            if isinstance(d, dict) and (d.get("local_path_set") or d.get("local_path"))
        )
        or 0
    )
    next_stage = str(
        item.get("next_stage") or prior.get("next_stage") or item.get("stage_reached") or ""
    ).upper()
    resume_commercial = bool(
        item.get("commercial_resume")
        or prior.get("commercial_resume")
        or next_stage in {"SOURCE", "PRICE", "ECONOMICS", "FINANCE", "BID_PREP"}
    )
    usable = bool(
        resume_commercial
        and complete
        and docs_n > 0
        and lines
        and any(_line_qty(li) not in (None, "") for li in lines if isinstance(li, dict))
        and any(_line_desc(li) for li in lines if isinstance(li, dict))
    )
    return {
        "usable": usable,
        "lines": list(lines) if usable else [],
        "docs": list(docs) if usable else [],
        "docs_n": docs_n if usable else 0,
        "complete": complete if usable else False,
        "next_stage": next_stage or None,
        "prior": prior,
    }


def process_one(
    item: dict[str, Any],
    *,
    client: httpx.Client,
    evidence_index: dict[str, Any],
    auth_status: dict[str, Any] | None = None,
    l23_rec: dict[str, Any] | None = None,
    auth_client: Any | None = None,
) -> dict[str, Any]:
    t0 = time.time()
    oid = item["opportunity_id"]
    l23_rec = dict(l23_rec or {})
    stage_timings: dict[str, float] = {}
    stage_status: dict[str, str] = {}
    url = str(item.get("authoritative_url") or l23_rec.get("authoritative_url") or "").strip()
    if not url.startswith("http"):
        # Resolve from common L23 / row_ref fields before declaring fetch failure
        for key in ("source_url", "url", "detail_url", "portal_url"):
            cand = str(l23_rec.get(key) or "").strip()
            if cand.startswith("http"):
                url = cand
                break
        if not url.startswith("http"):
            rr = l23_rec.get("row_ref") if isinstance(l23_rec.get("row_ref"), dict) else {}
            for key in ("authoritative_url", "url", "href", "detail_url"):
                cand = str(rr.get(key) or "").strip()
                if cand.startswith("http"):
                    url = cand
                    break
    title = str(item.get("title") or l23_rec.get("title") or l23_rec.get("product") or "")
    # Queue rows often omit title — derive from open-bids slug for search unlock.
    if len(title.strip()) < 6 and url:
        import re as _re

        m_slug = _re.search(r"/open-bids/([^/]+)/\d+", url, _re.I)
        if m_slug:
            title = _re.sub(r"[-_]+", " ", m_slug.group(1)).strip()
    auth_status = auth_status or {}
    out: dict[str, Any] = {
        "opportunity_id": oid,
        "title": title,
        "buyer": item.get("buyer") or l23_rec.get("buyer"),
        "deadline": item.get("deadline") or l23_rec.get("deadline"),
        "authoritative_url": url,
        "AUTO_SEND": AUTO_SEND,
        "started_at": _utc(),
        "bidnet_auth_status": auth_status.get("status"),
        "url_resolved_from_l23": bool(url) and url != str(item.get("authoritative_url") or "").strip(),
    }
    # Evidence join first — only skip live price research for KNOWN verified public prices.
    # product_cost / unverified overlays must not short-circuit commercial research.
    ev = lookup_evidence_for_opportunity(oid, title=title, index=evidence_index)
    out["evidence_sources"] = ev.get("sources") or []
    out["historical_unit_price"] = ev.get("historical_unit") or ev.get("historical_government_price")
    cached_public = ev.get("public_acquisition_cost")
    if cached_public in (None, "") and isinstance(ev.get("acquisition_prices"), (int, float, str)):
        cached_public = ev.get("acquisition_prices")
    pricing_known = str(ev.get("pricing_status") or "").upper() == "KNOWN"
    if pricing_known and cached_public not in (None, ""):
        out["public_unit_price"] = cached_public
        out["public_price_verified"] = True
        out["public_price_route"] = "evidence_index_known"
    else:
        out["public_unit_price"] = None
        out["public_price_verified"] = False
        if cached_public not in (None, ""):
            out["cached_unverified_public_hint"] = cached_public
    out["prior_package_depth"] = ev.get("package_depth")
    out["prior_real_lines"] = ev.get("real_lines")

    # Production document recovery — BidNetAuthenticatedClient + process_bidnet_opportunity
    # (private detail, attachments, paid-entitlement → official/SAM alternate). Not abstract-only.
    html = ""
    status = 0
    final = url
    parsed: dict[str, Any] = {}
    fetch_via = "none"
    package_status = "METADATA_ONLY"
    lines: list[dict[str, Any]] = []
    free_chase: dict[str, Any] | None = None
    recovered_docs: list[dict[str, Any]] = []
    is_bidnet_url = "bidnet" in url.lower()
    out["is_bidnet_url"] = is_bidnet_url
    resume = _prior_commercial_resume(item)
    skip_docs_recovery = bool(resume.get("usable"))
    if skip_docs_recovery:
        t_docs = time.time()
        lines = list(resume.get("lines") or [])
        recovered_docs = list(resume.get("docs") or [])
        package_status = "DOCUMENTS_RECOVERED"
        fetch_via = "prior_checkpoint_commercial_resume"
        out["documents_downloaded"] = int(resume.get("docs_n") or 0)
        out["document_count"] = int(resume.get("docs_n") or len(recovered_docs) or 0)
        out["commercial_resume_from_checkpoint"] = True
        out["skipped_docs_recovery"] = True
        stage_status["ACCESS_DOCS"] = "COMPLETED_PRIOR"
        stage_status["EXTRACT"] = "COMPLETED_PRIOR"
        stage_timings["docs_resume_s"] = round(time.time() - t_docs, 4)
        out["last_completed_stage"] = "EXTRACT"
        out["next_stage"] = resume.get("next_stage") or "PRICE"
    # Re-auth once if session was invalidated (e.g. concurrent login probe).
    if (
        not skip_docs_recovery
        and auth_client is not None
        and is_bidnet_url
        and url.startswith("http")
        and not getattr(auth_client, "is_authenticated", False)
        and hasattr(auth_client, "ensure_authenticated")
    ):
        try:
            refreshed = auth_client.ensure_authenticated()
            out["bidnet_auth_status"] = getattr(refreshed, "status", None) or out.get(
                "bidnet_auth_status"
            )
            out["auth_refreshed"] = bool(getattr(refreshed, "authenticated", False))
            if isinstance(auth_status, dict):
                auth_status["status"] = out["bidnet_auth_status"]
                auth_status["authenticated"] = bool(getattr(refreshed, "authenticated", False))
        except Exception as exc:
            out["auth_refresh_error"] = type(exc).__name__
    if (
        not skip_docs_recovery
        and auth_client is not None
        and getattr(auth_client, "is_authenticated", False)
        and url.startswith("http")
        and is_bidnet_url
    ):
        try:
            # Permanent production path: hard 45s wall clock; no unbounded Playwright
            from buyer_intelligence.bidnet_bounded_document_recovery_v1 import (
                COMPLETE_PACKAGE,
                PAID_ENTITLEMENT,
                recover_one_bounded,
            )

            # CRITICAL: reuse the live authenticated client (isolate_process=False).
            # PG *registration* is group-level (separate queue) — opportunity budget
            # is for detail/docs/lines only. Registered groups get a longer docs budget.
            from bidnet_auth.purchasing_group_registration import extract_pg_id_from_url
            from bidnet_auth.purchasing_group_tracker import is_registered

            t_docs = time.time()
            pg_id_from_url = extract_pg_id_from_url(url)
            if pg_id_from_url:
                out["purchasing_group_id"] = pg_id_from_url
            pg_registered = bool(pg_id_from_url and is_registered(str(pg_id_from_url)))
            # Reserve wall-clock for PRICE stage; align with FSPF item timeout.
            wall = float(os.environ.get("M3_FSPF_ITEM_TIMEOUT_S") or "100")
            price_reserve = float(os.environ.get("M3_FSPF_PRICE_RESERVE_S") or "35")
            docs_cap = 90.0 if pg_registered else 50.0
            item_budget = max(25.0, min(docs_cap, wall - price_reserve))
            stage_status["ACCESS_DOCS"] = "STARTED"
            bounded = recover_one_bounded(
                {
                    "opportunity_id": oid,
                    "title": title,
                    "buyer": item.get("buyer"),
                    "authoritative_url": url,
                    "deadline": item.get("deadline"),
                },
                auth_client=auth_client,
                l23=dict(l23_rec or {}),
                timeout_s=item_budget,
                isolate_process=False,
            )
            fetch_via = "bounded_document_recovery_inline"
            out["bounded_blocker"] = bounded.get("primary_blocker")
            out["bounded_timed_out"] = bool(bounded.get("timed_out"))
            out["blocker_evidence"] = bounded.get("blocker_evidence")
            out["detail_status"] = bounded.get("detail_status")
            out["package_state"] = bounded.get("package_state")
            out["access_note"] = bounded.get("access_note")
            out["early_search_to_detail"] = bounded.get("early_search_to_detail")
            out["search_to_detail_retry"] = bounded.get("search_to_detail_retry")
            out["search_to_detail_docs"] = bounded.get("search_to_detail_docs")
            out["search_to_detail"] = bounded.get("search_to_detail")
            out["bidnet_solicitation_id"] = bounded.get("bidnet_solicitation_id")
            out["official_portal_resolved"] = bool(bounded.get("free_portal_recovered"))
            out["solicitation_resolved"] = bool(bounded.get("solicitation_from_intercept"))
            out["paid_entitlement_requirement"] = bounded.get("paid_entitlement_requirement")
            out["solicitation_from_intercept"] = bounded.get("solicitation_from_intercept")
            out["official_hit"] = bounded.get("official_hit")
            # If bounded path saw a PG URL without registration status, enqueue
            # group-level work (never burn opportunity budget on form fill here).
            if (
                pg_id_from_url
                and not bounded.get("purchasing_group_registration")
                and not (bounded.get("documents") or [])
            ):
                try:
                    from bidnet_auth.purchasing_group_queue import enqueue_group_registration
                    from bidnet_auth.purchasing_group_tracker import is_registered

                    if is_registered(str(pg_id_from_url)):
                        bounded["purchasing_group_registration"] = {
                            "status": "REGISTERED",
                            "already_registered": True,
                            "purchasing_group_id": pg_id_from_url,
                        }
                    else:
                        enqueue_group_registration(
                            str(pg_id_from_url),
                            opportunity_id=oid,
                            source_url=url,
                        )
                        bounded["purchasing_group_registration"] = {
                            "status": "QUEUED",
                            "skipped": True,
                            "reason": "deferred_to_group_level_queue",
                            "purchasing_group_id": pg_id_from_url,
                        }
                        bounded["package_blocker_hint"] = "AWAITING_PURCHASING_GROUP_REGISTRATION"
                        out["pg_registration_via"] = "cfe_group_queue_enqueue"
                except Exception as exc:
                    out["purchasing_group_registration_error"] = type(exc).__name__
            if bounded.get("purchasing_group_gate") or bounded.get("purchasing_group_registration"):
                out["purchasing_group_gate"] = bounded.get("purchasing_group_gate")
                out["purchasing_group_registration"] = bounded.get("purchasing_group_registration")
                pg_reg = bounded.get("purchasing_group_registration") or {}
                pg_st = str(pg_reg.get("status") or "").upper()
                out["purchasing_group_id"] = (
                    pg_reg.get("purchasing_group_id")
                    or (bounded.get("purchasing_group_gate") or {}).get("purchasing_group_id")
                    or pg_id_from_url
                )
                if bounded.get("package_blocker_hint") == "AWAITING_PURCHASING_GROUP_REGISTRATION" or pg_st in {
                    "QUEUED",
                    "REGISTERING",
                }:
                    out["package_blocker"] = "AWAITING_PURCHASING_GROUP_REGISTRATION"
                elif pg_st in {"OWNER_APPROVAL_REQUIRED", "PREPARED"}:
                    out["package_blocker"] = "AWAITING_OWNER_ACTION"
                    out["owner_action_required"] = True
                elif pg_st == "PAID_REQUIRED":
                    out["package_blocker"] = "PAID_ENTITLEMENT:PURCHASING_GROUP_FEE"
                elif pg_st == "REGISTERED" or pg_reg.get("already_registered"):
                    # Registered: docs recovery should have run; if empty, precise retryable
                    if not (bounded.get("documents") or []):
                        if str(bounded.get("primary_blocker") or "") == "SOLICITATION_RESOLUTION_FAILED":
                            out["package_blocker"] = "SOLICITATION_RESOLUTION_FAILED"
                            out["software_defect_key"] = (
                                out.get("software_defect_key")
                                or "bidnet_solicitation_resolution"
                            )
                        else:
                            out["package_blocker"] = (
                                out.get("package_blocker") or "DETAIL_FETCH_EMPTY"
                            )
                        out["pg_registered_awaiting_docs"] = True
                elif str(bounded.get("primary_blocker") or "").upper().find("PURCHASING") >= 0:
                    out["package_blocker"] = "PURCHASING_GROUP_REGISTRATION_REQUIRED"
            elif pg_id_from_url and str(bounded.get("primary_blocker") or "").upper().find("PURCHASING") >= 0:
                out["package_blocker"] = "PURCHASING_GROUP_REGISTRATION_REQUIRED"
                out["purchasing_group_id"] = pg_id_from_url
            recovered_docs = [
                {
                    "document_name": d.get("name") or d.get("document_name") or d.get("filename"),
                    "document_url": d.get("url") or d.get("document_url"),
                    "retrieval_status": d.get("retrieval_status"),
                    "local_path": d.get("local_path") or d.get("canonical_storage_reference"),
                    "local_path_set": bool(
                        d.get("local_path_set")
                        or d.get("local_path")
                        or d.get("canonical_storage_reference")
                    ),
                    "source": d.get("source"),
                }
                for d in (bounded.get("documents") or [])
                if isinstance(d, dict)
            ]
            out["document_count"] = len(recovered_docs)
            out["documents_downloaded"] = sum(
                1 for d in recovered_docs if d.get("local_path_set") or d.get("local_path")
            )
            status = 200
            final = url
            lines = list(bounded.get("line_items") or [])
            if bounded.get("primary_blocker") == COMPLETE_PACKAGE:
                package_status = "DOCUMENTS_RECOVERED"
                out.pop("package_blocker", None)
            elif recovered_docs:
                package_status = "DOCUMENTS_RECOVERED"
                out.pop("package_blocker", None)
            elif bounded.get("primary_blocker") == PAID_ENTITLEMENT:
                out["package_blocker"] = (
                    f"PAID_ENTITLEMENT:{bounded.get('paid_entitlement_requirement') or 'verified'}"
                )
            elif str(bounded.get("primary_blocker") or "") == "SOLICITATION_RESOLUTION_FAILED":
                # Only assign after authenticated search actually ran. A browser
                # failure must never be counted as proof the solicitation is missing.
                auth_st = str(out.get("bidnet_auth_status") or auth_status.get("status") or "")
                if auth_st in {
                    "AUTH_FAILED",
                    "BROWSER_STARTUP_FAILED",
                    "BROWSER_PROCESS_FAILED",
                    "BROWSER_CONTEXT_INVALID",
                    "SESSION_RESTORE_FAILED",
                } or str(out.get("auth_message") or "").startswith("browser_unavailable"):
                    out["package_blocker"] = str(
                        auth_status.get("failure_class")
                        or auth_status.get("external_blocker")
                        or "BROWSER_CONTEXT_INVALID"
                    )
                    out["software_defect_key"] = "browser_auth_transient"
                else:
                    out["package_blocker"] = "SOLICITATION_RESOLUTION_FAILED"
                    out["software_defect_key"] = out.get("software_defect_key") or (
                        "bidnet_solicitation_resolution"
                    )
            stage_timings["docs_recovery_s"] = round(time.time() - t_docs, 3)
            # Soft budget exceed after durable docs+lines is NOT a terminal timeout.
            if bounded.get("docs_budget_exceeded_after_success") or (
                bounded.get("docs_budget_exceeded") and recovered_docs and lines
            ):
                out["docs_budget_exceeded"] = True
                out["bounded_timed_out"] = False
                stage_status["ACCESS_DOCS"] = "COMPLETED"
                stage_status["EXTRACT"] = "COMPLETED" if lines else "PARTIAL"
                out["last_completed_stage"] = "EXTRACT" if lines else "ACCESS_DOCS"
            elif bounded.get("timed_out") and recovered_docs and lines:
                # Defensive: success evidence beats timeout label from older recoverers.
                out["docs_budget_exceeded"] = True
                out["bounded_timed_out"] = False
                out.pop("package_blocker", None)
                if out.get("bounded_blocker") in {
                    "PER_OPPORTUNITY_TIMEOUT",
                    "PER_OPPORTUNITY_TIMEOUT_45S",
                }:
                    out["bounded_blocker"] = COMPLETE_PACKAGE
                stage_status["ACCESS_DOCS"] = "COMPLETED"
                stage_status["EXTRACT"] = "COMPLETED"
                out["last_completed_stage"] = "EXTRACT"
            elif bounded.get("timed_out"):
                out["package_blocker"] = out.get("package_blocker") or "PER_OPPORTUNITY_TIMEOUT"
                out["bounded_timed_out"] = True
                stage_status["ACCESS_DOCS"] = "TIMED_OUT"
            elif recovered_docs:
                stage_status["ACCESS_DOCS"] = "COMPLETED"
                stage_status["EXTRACT"] = "COMPLETED" if lines else "PARTIAL"
                out["last_completed_stage"] = "EXTRACT" if lines else "ACCESS_DOCS"
        except Exception as exc:
            out["process_bidnet_error"] = f"{type(exc).__name__}:{exc}"[:200]
            fetch_via = "process_bidnet_opportunity_failed"
            stage_status["ACCESS_DOCS"] = "FAILED"

    # After a hard docs timeout with no lines, do not burn the price reserve on chase.
    # Also skip chase when durable docs+lines already exist (resume or successful extract).
    skip_fallback_chase = (bool(out.get("bounded_timed_out")) and not lines) or bool(
        recovered_docs and lines
    ) or skip_docs_recovery

    if (not skip_fallback_chase) and fetch_via in {"none", "process_bidnet_opportunity_failed"}:
        try:
            if (
                url.startswith("http")
                and auth_client is not None
                and getattr(auth_client, "is_authenticated", False)
            ):
                html = auth_client.fetch_html(url, timeout_ms=45_000) or ""
                status = 200 if html else 0
                final = url
                fetch_via = "BidNetAuthenticatedClient.fetch_html"
            elif is_bidnet_url:
                # Never treat unauthenticated public httpx as BidNet package recovery.
                out["package_blocker"] = str(
                    auth_status.get("external_blocker")
                    or auth_status.get("status")
                    or "BIDNET_AUTH_REQUIRED"
                )
                out["terminal_blocker"] = out["package_blocker"]
                fetch_via = "bidnet_auth_required"
            elif url.startswith("http"):
                r = client.get(url)
                status = int(r.status_code)
                html = r.text or ""
                final = str(r.url)
                fetch_via = "httpx_public_or_cookie"
        except Exception as exc:
            out["fetch_error"] = f"{type(exc).__name__}:{exc}"[:200]
            status = 0
            if is_bidnet_url:
                out["package_blocker"] = out.get("package_blocker") or "BIDNET_AUTH_REQUIRED"
                fetch_via = fetch_via or "bidnet_auth_fetch_failed"
            elif not html and url.startswith("http"):
                try:
                    r = client.get(url)
                    status = int(r.status_code)
                    html = r.text or ""
                    final = str(r.url)
                    fetch_via = "httpx_fallback_after_auth_error"
                except Exception as exc2:
                    out["fetch_error"] = f"{out.get('fetch_error')};fallback:{type(exc2).__name__}"[:240]

    out["http_status"] = status
    out["final_url"] = final
    out["fetch_via"] = fetch_via
    out["recovered_documents"] = [
        {
            "name": d.get("document_name") or d.get("filename") or d.get("name"),
            "retrieval_status": d.get("retrieval_status"),
            "local_path_set": bool(d.get("local_path") or d.get("canonical_storage_reference")),
            "source": d.get("source") or d.get("free_chase_via"),
        }
        for d in recovered_docs[:20]
        if isinstance(d, dict)
    ]
    # Abstract parse is a fallback only when bounded recovery produced no lines.
    # Never overwrite a successful docs/lines recovery with ABSTRACT_FETCH_FAILED_OR_WAF.
    if (not lines) and html and len(html) > 2000 and "gokuProps" not in html:
        parsed = parse_bidnet_abstract(html, detail_url=final)
        out["abstract_parse_ok"] = bool(parsed.get("parse_ok"))
        out["abstract_solicitation_number"] = parsed.get("solicitation_number")
        out["abstract_deadline_raw"] = parsed.get("deadline_raw") or parsed.get("close_date_raw")
        out["locked_fields"] = parsed.get("locked_fields") or []
        out["document_count"] = max(int(out.get("document_count") or 0), len(parsed.get("documents") or []))
        lines = _extract_lines_from_parse(parsed, title)
        # Closing confirmation (already have deadline — provenance only)
        ex = extract_bidnet_closing_from_html(html, detail_url=final, retrieved_at=_utc())
        if ex.get("deadline_raw"):
            out["closing_confirmed_raw"] = ex.get("deadline_raw")
        if lines and not (len(lines) == 1 and lines[0].get("source") == "title_fallback"):
            package_status = "PARTIAL_ABSTRACT_LINES"
        if parsed.get("description") or parsed.get("overview"):
            if package_status == "METADATA_ONLY":
                package_status = "ABSTRACT_DESCRIPTION_ONLY"
        docs = parsed.get("documents") or []
        if docs and not (parsed.get("auth_wall") and parsed.get("locked_fields")):
            package_status = "AUTHENTICATED_PACKAGE_DOCS"
            out.pop("package_blocker", None)
        elif parsed.get("auth_wall") or parsed.get("locked_fields"):
            out["auth_wall"] = True
            if auth_status.get("external_blocker") == "LOCAL_ENV_WITHOUT_PRODUCTION_CREDENTIALS":
                out["package_blocker"] = "LOCAL_ENV_WITHOUT_PRODUCTION_CREDENTIALS"
            elif auth_status.get("external_blocker") or not auth_status.get("authenticated"):
                out["package_blocker"] = str(
                    auth_status.get("external_blocker") or "EXTERNAL_ACCOUNT_CREDENTIALS_REQUIRED"
                )
            else:
                # Authenticated session still sees locked fields → entitlement / purchasing-group
                pg = out.get("purchasing_group_gate") or out.get("bounded_blocker") or ""
                if "PURCHASING_GROUP" in str(pg).upper() or "purchasing" in str(
                    (out.get("blocker_evidence") or {})
                ).lower():
                    out["package_blocker"] = "PURCHASING_GROUP_REGISTRATION_REQUIRED"
                else:
                    out["package_blocker"] = "SOURCE_REQUIRES_PAID_ENTITLEMENT_OR_LOGIN"
    elif not lines and not recovered_docs:
        # Precise empty-result classification — not every miss is a WAF
        pg_hint = "purchasinggroupid=" in url.lower()
        if not url or not url.startswith("http"):
            out["package_blocker"] = out.get("package_blocker") or "MISSING_AUTHORITATIVE_URL"
        elif pg_hint and (
            out.get("purchasing_group_gate")
            or out.get("bounded_blocker")
            or fetch_via
            in {"bounded_document_recovery_45s", "bounded_document_recovery_inline"}
        ):
            # Public statewide URLs with purchasingGroupId are PG gates, not WAF
            out["package_blocker"] = out.get("package_blocker") or "PURCHASING_GROUP_REGISTRATION_REQUIRED"
            if not out.get("purchasing_group_id"):
                try:
                    from bidnet_auth.purchasing_group_registration import extract_pg_id_from_url

                    out["purchasing_group_id"] = extract_pg_id_from_url(url)
                except Exception:
                    pass
        elif status in (401, 403):
            out["package_blocker"] = out.get("package_blocker") or "SOURCE_REQUIRES_PAID_ENTITLEMENT"
        elif "gokuProps" in (html or "") or (html and len(html) <= 2000):
            out["package_blocker"] = out.get("package_blocker") or "WAF_OR_CHALLENGE_PAGE"
        elif fetch_via in {"none", "process_bidnet_opportunity_failed"} and not html:
            if auth_status.get("external_blocker"):
                out["package_blocker"] = out.get("package_blocker") or str(auth_status["external_blocker"])
            else:
                out["package_blocker"] = out.get("package_blocker") or "DETAIL_FETCH_EMPTY"
        elif not html:
            out["package_blocker"] = out.get("package_blocker") or (
                "PURCHASING_GROUP_REGISTRATION_REQUIRED" if pg_hint else "DETAIL_FETCH_EMPTY"
            )
        else:
            out["package_blocker"] = out.get("package_blocker") or "ABSTRACT_PARSE_UNAVAILABLE"

    # Free public package chase (existing BidNet→buyer-portal path; no membership bypass)
    # Skip after docs timeout OR when durable docs+lines already exist so pricing
    # budget is not consumed by chase.
    if skip_fallback_chase:
        out["free_chase_status"] = (
            "SKIPPED_HAVE_DOCS_AND_LINES"
            if (recovered_docs and lines) or skip_docs_recovery
            else "SKIPPED_AFTER_DOCS_TIMEOUT"
        )
    else:
        try:
            from bidnet_recovery.free_package_chase import chase_free_package

            rec = dict(l23_rec or {})
            rec.update(
                {
                    "opportunity_id": oid,
                    "title": title,
                    "buyer": item.get("buyer"),
                    "authoritative_url": url,
                }
            )
            free_chase = chase_free_package(rec, parsed=parsed or None)
            out["free_chase_status"] = free_chase.get("status")
            out["free_chase_route"] = free_chase.get("recovery_route")
            out["free_chase_docs"] = len(free_chase.get("documents") or [])
            out["free_chase_source_url"] = free_chase.get("source_url") or free_chase.get(
                "official_source_url"
            )
            docs = free_chase.get("documents") or []
            if docs:
                package_status = "FREE_PACKAGE_FOUND"
                out.pop("package_blocker", None)
                chase_lines = free_chase.get("line_items") or free_chase.get("lines") or []
                if chase_lines:
                    lines = list(chase_lines)
        except Exception as exc:
            out["free_chase_error"] = f"{type(exc).__name__}:{exc}"[:160]

    out["package_status"] = package_status
    out["line_items"] = lines
    out["line_count"] = len(lines)
    out["complete_spec"] = bool(
        lines
        and not (
            len(lines) == 1
            and (
                lines[0].get("source") == "title_fallback"
                or lines[0].get("LINE_SOURCE_TYPE") == "TITLE_FALLBACK"
            )
        )
        and any(_line_qty(li) not in (None, "") for li in lines if isinstance(li, dict))
        and any(_line_desc(li) for li in lines if isinstance(li, dict))
    )
    if lines and _line_qty(lines[0]) not in (None, ""):
        out["primary_quantity"] = _line_qty(lines[0])

    # Re-match historical by primary line description if still missing
    if out.get("historical_unit_price") in (None, "") and lines:
        ev2 = lookup_evidence_for_opportunity(
            oid, title=_line_desc(lines[0]) or title, index=evidence_index
        )
        if ev2.get("historical_unit"):
            out["historical_unit_price"] = ev2.get("historical_unit")
            out["historical_match"] = ev2.get("historical_match")

    # Distributor candidates (deterministic heuristics — not quotes)
    distributors = []
    blob = (title + " " + " ".join(_line_desc(li) for li in lines if isinstance(li, dict))).lower()
    if re.search(r"\b(tool|drill|saw|wrench|fastener|screw)\b", blob):
        distributors += [
            {"name": "Grainger", "type": "distributor", "basis": "category_heuristic"},
            {"name": "MSC Industrial", "type": "distributor", "basis": "category_heuristic"},
            {"name": "Fastenal", "type": "distributor", "basis": "category_heuristic"},
        ]
    if re.search(r"\b(mro|filter|lubricant|bearing|hose|valve)\b", blob):
        distributors += [
            {"name": "Motion Industries", "type": "distributor", "basis": "category_heuristic"},
            {"name": "Applied Industrial", "type": "distributor", "basis": "category_heuristic"},
        ]
    if re.search(r"\b(toner|paper|office|janitorial|glove|ppe)\b", blob):
        distributors += [
            {"name": "Staples Business Advantage", "type": "distributor", "basis": "category_heuristic"},
            {"name": "Uline", "type": "distributor", "basis": "category_heuristic"},
        ]
    if re.search(r"\b(led|lamp|lighting|electrical)\b", blob):
        distributors += [
            {"name": "Platt Electric", "type": "distributor", "basis": "category_heuristic"},
            {"name": "Graybar", "type": "distributor", "basis": "category_heuristic"},
        ]
    # dedupe
    seen = set()
    uniq = []
    for d in distributors:
        if d["name"] not in seen:
            seen.add(d["name"])
            uniq.append(d)

    # Awardable scope before commercial research (affects economics basis)
    awardable = detect_awardable_scope(
        title=title,
        lines=lines,
        html_or_notes=str(out.get("access_note") or "")
        + " "
        + str((out.get("blocker_evidence") or "")),
        package_status=package_status,
    )
    out["awardable_scope"] = awardable
    out["award_unit"] = awardable.get("award_unit")
    out["complete_awardable_lot"] = bool(awardable.get("complete_awardable_lot"))
    out["complete_awardable_package"] = bool(awardable.get("complete_awardable_package"))

    # Live public acquisition-price research when specs exist and cache is empty.
    # Bounded; never invents prices. Feeds distributors + RFQ + economics.
    # Stage-aware: if remaining wall is below a meaningful pricing floor, persist
    # extract and defer commercial resume instead of starting a doomed search.
    price_research: dict[str, Any] = {
        "ok": False,
        "prices": [],
        "failures": [],
        "skipped": True,
    }
    commercial_budget_deferred = False
    source_outcome = "NOT_ATTEMPTED"
    min_price_budget = float(os.environ.get("M3_FSPF_MIN_PRICE_BUDGET_S") or "18")
    wall = float(os.environ.get("M3_FSPF_ITEM_TIMEOUT_S") or "100")
    remain_before_commercial = wall - (time.time() - t0)
    stage_timings["remain_before_commercial_s"] = round(remain_before_commercial, 3)
    spec_for_research = bool(
        lines
        and not (
            len(lines) == 1
            and (
                lines[0].get("source") == "title_fallback"
                or lines[0].get("LINE_SOURCE_TYPE") == "TITLE_FALLBACK"
            )
        )
        and any(_line_qty(li) not in (None, "") for li in lines if isinstance(li, dict))
        and any(_line_desc(li) for li in lines if isinstance(li, dict))
    )
    if spec_for_research and (
        out.get("public_unit_price") in (None, "") or not out.get("public_price_verified")
    ):
        if remain_before_commercial < min_price_budget:
            commercial_budget_deferred = True
            source_outcome = "SKIPPED_INSUFFICIENT_BUDGET"
            stage_status["SOURCE"] = "DEFERRED_INSUFFICIENT_BUDGET"
            stage_status["PRICE"] = "DEFERRED_INSUFFICIENT_BUDGET"
            out["commercial_budget_deferred"] = True
            out["last_completed_stage"] = out.get("last_completed_stage") or "EXTRACT"
            out["next_stage"] = "PRICE"
            price_research = {
                "ok": False,
                "prices": [],
                "failures": [{"failure_class": "INSUFFICIENT_REMAINING_BUDGET"}],
                "skipped": True,
                "deferred": True,
                "remain_s": round(remain_before_commercial, 3),
                "min_budget_s": min_price_budget,
                "elapsed_s": 0.0,
                "price_evidence_count": 0,
            }
        else:
            t_price = time.time()
            stage_status["SOURCE"] = "STARTED"
            stage_status["PRICE"] = "STARTED"
            remain = max(min_price_budget, remain_before_commercial)
            price_research = _research_public_prices_for_lines(
                opportunity_id=oid,
                lines=lines,
                budget_s=min(45.0, remain),
                max_lines=int(os.environ.get("M3_FSPF_PRICE_MAX_LINES") or "8"),
            )
            stage_timings["price_research_s"] = round(time.time() - t_price, 3)
            stage_timings["supplier_research_s"] = stage_timings["price_research_s"]
            if price_research.get("verified_public_unit_price") not in (None, ""):
                out["public_unit_price"] = price_research["verified_public_unit_price"]
                out["public_price_evidence"] = (price_research.get("prices") or [])[:5]
                out["public_price_route"] = "live_research_mpn_or_description"
                stage_status["PRICE"] = "COMPLETED"
                source_outcome = "SUPPLIER_AND_PRICE_FOUND"
            elif price_research.get("failures"):
                out["public_price_failures"] = (price_research.get("failures") or [])[:8]
                fail_classes = {
                    str(f.get("failure_class") or f.get("status") or "")
                    for f in (price_research.get("failures") or [])
                    if isinstance(f, dict)
                }
                if fail_classes & {
                    "IMPORT_ERROR",
                    "BUDGET_EXCEEDED",
                    "TimeoutException",
                    "ReadTimeout",
                    "ConnectTimeout",
                }:
                    source_outcome = (
                        "RESEARCH_INTERRUPTED"
                        if "BUDGET_EXCEEDED" in fail_classes
                        else "PROVIDER_ERROR"
                    )
                    stage_status["PRICE"] = (
                        "TIMED_OUT" if "BUDGET_EXCEEDED" in fail_classes else "PROVIDER_ERROR"
                    )
                else:
                    source_outcome = "NO_COMPLIANT_PUBLIC_PRICE"
                    stage_status["PRICE"] = "COMPLETED_NO_PRICE"
            else:
                source_outcome = "NO_COMPLIANT_PUBLIC_PRICE"
                stage_status["PRICE"] = "COMPLETED_NO_PRICE"
            for d in price_research.get("distributors") or []:
                if isinstance(d, dict) and d.get("name") and d["name"] not in seen:
                    seen.add(d["name"])
                    uniq.append(d)
    elif out.get("public_unit_price") not in (None, "") and out.get("public_price_verified"):
        source_outcome = "CACHED_VERIFIED_PRICE"
        stage_status["PRICE"] = "COMPLETED_PRIOR"
    out["price_research"] = {
        "ok": bool(price_research.get("ok")),
        "price_evidence_count": int(price_research.get("price_evidence_count") or 0),
        "elapsed_s": price_research.get("elapsed_s"),
        "skipped": bool(price_research.get("skipped")),
        "deferred": bool(price_research.get("deferred")),
        "failure_classes": list(
            {
                str(f.get("failure_class") or f.get("status") or "UNKNOWN")
                for f in (price_research.get("failures") or [])
                if isinstance(f, dict)
            }
        )[:8],
    }

    out["distributors"] = uniq
    # Distinguish completed empty search vs provider failure vs interrupted research.
    if uniq:
        out["supplier_status"] = "SUPPLIER_IDENTIFIED"
        if source_outcome == "NOT_ATTEMPTED":
            source_outcome = "HEURISTIC_CANDIDATES_ONLY"
        stage_status["SOURCE"] = stage_status.get("SOURCE") or "COMPLETED"
    elif source_outcome == "PROVIDER_ERROR":
        out["supplier_status"] = "SOURCE_PROVIDER_ERROR"
        stage_status["SOURCE"] = "PROVIDER_ERROR"
    elif source_outcome in {"RESEARCH_INTERRUPTED", "SKIPPED_INSUFFICIENT_BUDGET"}:
        out["supplier_status"] = "SOURCE_INCOMPLETE"
        stage_status["SOURCE"] = source_outcome
    elif source_outcome in {"NO_COMPLIANT_PUBLIC_PRICE", "HEURISTIC_CANDIDATES_ONLY"}:
        out["supplier_status"] = "SOURCE_UNAVAILABLE"
        stage_status["SOURCE"] = "COMPLETED_NO_SUPPLIER"
        source_outcome = "NO_COMPLIANT_SUPPLIER_FOUND"
    elif commercial_budget_deferred:
        out["supplier_status"] = "SOURCE_INCOMPLETE"
    else:
        out["supplier_status"] = "SOURCE_UNAVAILABLE"
        source_outcome = source_outcome if source_outcome != "NOT_ATTEMPTED" else "NO_CANDIDATES"
    out["source_outcome"] = source_outcome

    # RFQ prep — never send; incomplete specs are NOT ready-to-send
    spec_complete = spec_for_research
    delivery_loc = (
        str(item.get("delivery_location") or l23_rec.get("delivery_location") or "").strip()
        or None
    )
    rfq_lines = []
    for li in lines[:80]:
        if not isinstance(li, dict):
            continue
        if li.get("source") == "title_fallback" or li.get("LINE_SOURCE_TYPE") == "TITLE_FALLBACK":
            continue
        rfq_lines.append(
            {
                "description": _line_desc(li)[:240],
                "quantity": _line_qty(li),
                "uom": li.get("uom") or li.get("UOM") or li.get("unit"),
                "mpn": _line_mpn(li) or None,
                "manufacturer": _line_mfr(li) or None,
                "packaging": li.get("packaging") or li.get("pack_size") or li.get("case_pack"),
            }
        )
    rfq = {
        "opportunity_id": oid,
        "AUTO_SEND": False,
        "status": (
            "AWAITING_OWNER_APPROVED_OUTREACH"
            if spec_complete
            else "BLOCKED_INCOMPLETE_SPECIFICATIONS"
        ),
        "solicitation_url": url,
        "buyer": item.get("buyer") or l23_rec.get("buyer"),
        "deadline": item.get("deadline") or l23_rec.get("deadline"),
        "award_unit": awardable.get("award_unit"),
        "delivery_location": delivery_loc,
        "delivery_timing": item.get("delivery_timing") or l23_rec.get("delivery_timing"),
        "line_items": rfq_lines or lines,
        "suggested_recipients": [d["name"] for d in uniq] if spec_complete else [],
        "requested_from_supplier": [
            "unit_price",
            "freight",
            "stock_or_lead_time",
            "payment_or_deposit_terms",
            "packaging_confirmation",
        ],
        "message_outline": (
            f"RFQ for {title[:120]} (solicitation {oid}). Award unit: "
            f"{awardable.get('award_unit')}. Please quote exact products with unit price, "
            f"case/pack basis, MOQ, lead time/stock, freight to "
            f"{delivery_loc or 'buyer delivery location'}, and payment/deposit terms. "
            f"Do not submit until owner approves send (AUTO_SEND=false)."
            if spec_complete
            else "Do not send — product lines/quantities not recovered from solicitation documents."
        ),
        "ready_to_send": False,
        "spec_complete": spec_complete,
        "public_price_evidence_count": int(price_research.get("price_evidence_count") or 0),
    }
    out["rfq"] = rfq
    out["actual_quote_unit_price"] = None  # none received
    out["quote_status"] = rfq["status"]
    out["complete_spec"] = spec_complete

    econ = assess_economics(out)
    out["economics"] = econ
    out["econ_state"] = econ["econ_state"]
    # MARGIN_UNVERIFIED without acquisition evidence is provisional — not a completed assessment.
    out["economics_complete"] = bool(
        econ.get("acquisition_unit") not in (None, "")
        and econ.get("econ_state") not in {None, "", "MARGIN_UNVERIFIED"}
    )
    if not out["economics_complete"]:
        stage_status["ECONOMICS"] = "PROVISIONAL_INCOMPLETE"
    else:
        stage_status["ECONOMICS"] = "COMPLETED"
    out["financeable"] = False
    out["financing_status"] = "UNKNOWN"
    out["bid_ready"] = False

    # Next action — prefer actionable owner steps over generic lock labels
    if commercial_budget_deferred and out.get("complete_spec"):
        out["next_action"] = (
            "Resume commercial research (SOURCE/PRICE) using persisted product specifications"
        )
        out["terminal_blocker"] = "COMMERCIAL_STAGE_BUDGET_DEFERRED"
        out["next_stage"] = "PRICE"
        out["last_completed_stage"] = out.get("last_completed_stage") or "EXTRACT"
    elif out.get("package_status") == "FREE_PACKAGE_FOUND" and not out.get("complete_spec"):
        out["next_action"] = "Extract line quantities from recovered free package documents"
        out["terminal_blocker"] = "INCOMPLETE_LINE_ITEMS"
    elif out.get("package_blocker") == "LOCAL_ENV_WITHOUT_PRODUCTION_CREDENTIALS":
        out["next_action"] = (
            "Re-run CFE on Railway (railway run/ssh) where BIDNET_USERNAME/BIDNET_PASSWORD "
            "are already configured — do not re-enter credentials locally"
        )
        out["terminal_blocker"] = "LOCAL_ENV_WITHOUT_PRODUCTION_CREDENTIALS"
    elif out.get("package_blocker") == "EXTERNAL_ACCOUNT_CREDENTIALS_REQUIRED":
        out["next_action"] = (
            "Production BidNet credentials missing/unreadable in this process — verify "
            "BIDNET_USERNAME/BIDNET_PASSWORD env presence (not values) and session refresh"
        )
        out["terminal_blocker"] = "EXTERNAL_ACCOUNT_CREDENTIALS_REQUIRED"
    elif out.get("package_blocker"):
        out["next_action"] = f"Resolve package access: {out['package_blocker']}"
        out["terminal_blocker"] = out["package_blocker"]
    elif not out.get("complete_spec"):
        # Zero-doc resolution misses must not be mislabeled as incomplete specs.
        docs_n = int(out.get("documents_downloaded") or 0)
        early_st = str(
            ((out.get("early_search_to_detail") or {}) if isinstance(out.get("early_search_to_detail"), dict) else {}).get(
                "status"
            )
            or out.get("early_search_status")
            or ""
        ).upper()
        s2d_st = str(
            (
                (out.get("search_to_detail") or {})
                if isinstance(out.get("search_to_detail"), dict)
                else {}
            ).get("status")
            or out.get("search_to_detail_status")
            or ""
        ).upper()
        if docs_n == 0 and (
            str(out.get("bounded_blocker") or "") == "SOLICITATION_RESOLUTION_FAILED"
            or early_st
            in {
                "NO_INTERCEPTION_RESULT",
                "NO_SEARCH_QUERIES",
                "INTERCEPTION_NAV_FAILED",
            }
            or s2d_st
            in {
                "NO_INTERCEPTION_RESULT",
                "NO_SEARCH_QUERIES",
                "INTERCEPTION_NAV_FAILED",
            }
        ):
            out["next_action"] = (
                "Resolve private solicitation via authenticated search (statewide/open-bids "
                "IDs are not private solicitation IDs)"
            )
            out["terminal_blocker"] = "SOLICITATION_RESOLUTION_FAILED"
            out["software_defect_key"] = out.get("software_defect_key") or (
                "bidnet_solicitation_resolution"
            )
        else:
            out["next_action"] = (
                "Recover full solicitation package / line quantities from buyer portal"
            )
            out["terminal_blocker"] = "INCOMPLETE_LINE_ITEMS"
    elif source_outcome == "PROVIDER_ERROR":
        out["next_action"] = "Retry supplier/price research after provider error"
        out["terminal_blocker"] = "SOURCE_PROVIDER_ERROR"
        out["next_stage"] = "SOURCE"
    elif source_outcome == "RESEARCH_INTERRUPTED":
        out["next_action"] = "Resume interrupted supplier/price research"
        out["terminal_blocker"] = "SOURCE_RESEARCH_INTERRUPTED"
        out["next_stage"] = "PRICE"
    elif out.get("historical_unit_price") in (None, "") and out.get("public_unit_price") in (None, ""):
        out["next_action"] = "Obtain public distributor price or owner-approved RFQ send"
        # Completed empty search vs missing evidence overall
        if source_outcome == "NO_COMPLIANT_SUPPLIER_FOUND":
            out["terminal_blocker"] = "PRICE_UNAVAILABLE_AFTER_SEARCH"
        else:
            out["terminal_blocker"] = "MISSING_ACQUISITION_AND_GOV_PRICE"
        out["next_stage"] = "PRICE"
        out["last_completed_stage"] = out.get("last_completed_stage") or "EXTRACT"
    elif out["quote_status"] == "AWAITING_OWNER_APPROVED_OUTREACH":
        out["next_action"] = "Owner approve RFQ send to suggested distributors (AUTO_SEND=false)"
        out["terminal_blocker"] = "AWAITING_OWNER_APPROVED_OUTREACH"
    else:
        out["next_action"] = "Continue financing assessment after quote"
        out["terminal_blocker"] = "FINANCING_UNVERIFIED"

    out["stage_timings"] = stage_timings
    out["stage_status"] = stage_status
    out["elapsed_s"] = round(time.time() - t0, 2)
    out["finished_at"] = _utc()
    return out


def run_commercial_funnel_50(*, n: int = 50, resume: bool = True) -> dict[str, Any]:
    lock = _acquire_lock()
    if not lock:
        return {"ok": False, "error": "run_lock_held"}
    t0 = time.time()
    try:
        _progress(phase="INDEX", status="RUNNING")
        evidence_index = build_commercial_evidence_index()
        auth_status, auth_client = _open_bidnet_auth_client()
        _progress(phase="AUTH", bidnet_auth={k: v for k, v in auth_status.items() if k != "client"})
        man = _load(MANIFEST) if resume and _load(MANIFEST) else None
        if not man or not man.get("rows"):
            man = select_commercial_test_50(n=n)
        selected = man.get("rows") or []
        store = load_store()

        ckpt = _load(CHECKPOINT) or {
            "kind": "CommercialFunnelExecutionCheckpoint",
            "build": BUILD,
            "run_id": man.get("run_id"),
            "completed_ids": [],
            "results": [],
        }
        if not resume:
            ckpt = {
                "kind": "CommercialFunnelExecutionCheckpoint",
                "build": BUILD,
                "run_id": man.get("run_id"),
                "completed_ids": [],
                "results": [],
            }
        done = set(ckpt.get("completed_ids") or [])
        results = list(ckpt.get("results") or [])

        _progress(
            phase="EXECUTE",
            selected=len(selected),
            completed=len(done),
            evidence_oids=evidence_index.get("by_opportunity_count"),
            evidence_hist=evidence_index.get("opportunities_with_historical_price"),
            evidence_public=evidence_index.get("opportunities_with_public_price"),
            bidnet_authenticated=auth_status.get("authenticated"),
            bidnet_auth_status=auth_status.get("status"),
            credential_source=auth_status.get("credential_source"),
            execution_railway=bool((auth_status.get("execution_environment") or {}).get("railway")),
        )

        client = _cookies_client()
        try:
            for i, item in enumerate(selected):
                oid = item["opportunity_id"]
                if oid in done:
                    continue
                row = process_one(
                    item,
                    client=client,
                    evidence_index=evidence_index,
                    auth_status=auth_status,
                    l23_rec=store.get(oid) or {},
                    auth_client=auth_client,
                )
                results.append(row)
                done.add(oid)
                ckpt["completed_ids"] = sorted(done)
                ckpt["results"] = results
                ckpt["at"] = _utc()
                if len(done) % SAVE_EVERY == 0:
                    _save(CHECKPOINT, ckpt)
                    _save(RESULTS, {"build": BUILD, "count": len(results), "rows": results})
                    _progress(
                        phase="EXECUTE",
                        completed=len(done),
                        selected=len(selected),
                        last_oid=oid,
                        last_blocker=row.get("terminal_blocker"),
                    )
        finally:
            client.close()
            if auth_client is not None:
                try:
                    auth_client.close()
                except Exception:
                    pass

        _save(CHECKPOINT, ckpt)
        _save(RESULTS, {"build": BUILD, "count": len(results), "rows": results})

        # RFQ queue
        rfqs = [r["rfq"] for r in results if isinstance(r.get("rfq"), dict)]
        _save(
            RFQ_QUEUE,
            {
                "kind": "CommercialFunnelRFQQueue",
                "build": BUILD,
                "AUTO_SEND": False,
                "count": len(rfqs),
                "rows": rfqs,
                "NOTE": "Owner must approve before any external send.",
            },
        )

        # Aggregate
        blockers = Counter(r.get("terminal_blocker") or "UNKNOWN" for r in results)
        pkg = Counter(r.get("package_status") for r in results)
        econ = Counter(r.get("econ_state") for r in results)
        report = {
            "kind": "CommercialFunnelExecutionReport",
            "build": BUILD,
            "run_id": man.get("run_id"),
            "built_at": _utc(),
            "elapsed_s": round(time.time() - t0, 2),
            "AUTO_SEND": False,
            "openai_spend_usd": 0,
            "sam_api_calls": 0,
            "metrics": {
                "opportunities_tested": len(results),
                "full_packages_recovered": sum(
                    1
                    for r in results
                    if r.get("package_status") in {"FULL_PACKAGE", "AUTHENTICATED_PACKAGE_DOCS", "FREE_PACKAGE_FOUND"}
                ),
                "authenticated_package_docs": pkg.get("AUTHENTICATED_PACKAGE_DOCS", 0),
                "partial_abstract_lines": pkg.get("PARTIAL_ABSTRACT_LINES", 0),
                "abstract_description_only": pkg.get("ABSTRACT_DESCRIPTION_ONLY", 0),
                "metadata_only": pkg.get("METADATA_ONLY", 0),
                "fetch_via_auth_client": sum(
                    1 for r in results if str(r.get("fetch_via") or "").startswith("BidNetAuthenticatedClient")
                ),
                "complete_product_specifications": sum(1 for r in results if r.get("complete_spec")),
                "complete_bidding_lots": sum(1 for r in results if r.get("complete_spec") and r.get("line_count", 0) > 1),
                "verified_historical_government_prices": sum(
                    1 for r in results if r.get("historical_unit_price") not in (None, "")
                ),
                "verified_retail_distributor_prices": sum(
                    1 for r in results if r.get("public_unit_price") not in (None, "")
                ),
                "qualified_distributors_identified": sum(1 for r in results if r.get("distributors")),
                "complete_rfqs_prepared": len(rfqs),
                "real_supplier_quotes_received": 0,
                "positive_documented_margins": sum(
                    1
                    for r in results
                    if (r.get("economics") or {}).get("estimated_gross_spread")
                    and Decimal(str((r.get("economics") or {}).get("estimated_gross_spread"))) > 0
                ),
                "financeable_opportunities": 0,
                "fully_bid_ready_opportunities": 0,
            },
            "package_status_counts": dict(pkg),
            "econ_state_counts": dict(econ),
            "blockers": dict(blockers),
            "evidence_index_stats": {
                "by_opportunity_count": evidence_index.get("by_opportunity_count"),
                "opportunities_with_historical_price": evidence_index.get("opportunities_with_historical_price"),
                "opportunities_with_public_price": evidence_index.get("opportunities_with_public_price"),
                "by_product_stem_count": evidence_index.get("by_product_stem_count"),
            },
            "bidnet_auth": auth_status,
            "free_chase_status_counts": dict(
                Counter(r.get("free_chase_status") or "NOT_RUN" for r in results)
            ),
            "top_actionable": [
                {
                    "opportunity_id": r.get("opportunity_id"),
                    "title": r.get("title"),
                    "buyer": r.get("buyer"),
                    "deadline": r.get("deadline"),
                    "historical_unit_price": r.get("historical_unit_price"),
                    "public_unit_price": r.get("public_unit_price"),
                    "estimated_total_profit": (r.get("economics") or {}).get("estimated_total_profit"),
                    "distributors": [d.get("name") for d in (r.get("distributors") or [])],
                    "quote_status": r.get("quote_status"),
                    "financing_status": r.get("financing_status"),
                    "solicitation_url": r.get("authoritative_url"),
                    "next_action": r.get("next_action"),
                    "terminal_blocker": r.get("terminal_blocker"),
                    "package_status": r.get("package_status"),
                    "line_count": r.get("line_count"),
                }
                for r in sorted(
                    results,
                    key=lambda x: (
                        0 if x.get("historical_unit_price") else 1,
                        0 if x.get("complete_spec") else 1,
                        -(x.get("line_count") or 0),
                    ),
                )[:15]
            ],
            "architecture_notes": {
                "reused": [
                    "parse_bidnet_abstract",
                    "commercial_evidence_index_v1 (SPF/profit-first/HT/cex joins)",
                    "BidNet storage_state cookies",
                    "FCF LIVE_ELIGIBLE classifications",
                ],
                "permanent_fixes": [
                    "commercial_evidence_index_v1 joins historical SPF by product stem",
                    "available_deals_visibility will consume evidence index",
                    "RFQ queue persisted with AUTO_SEND=false",
                ],
                "retention": "Results/checkpoint/RFQ queue retained; raw HTML not stored",
            },
        }
        _save(REPORT, report)
        _progress(phase="DONE", status="COMPLETE", completed=len(results))
        return {"ok": True, "final_status": "COMPLETE", **report}
    finally:
        _release_lock(lock)
