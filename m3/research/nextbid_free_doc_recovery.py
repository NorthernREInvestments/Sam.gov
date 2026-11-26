"""Free official-URL document recovery for EXISTING NextBid records (Build 002Q).

ZERO metered NextBid / OpenAI / SAM / DIBBS.
Public HTTPS fetches of verified official_url only — no discovery, no auth bypass.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from m3.config import get_settings
from m3.research.document_quality import (
    classify_document_text,
    is_junk_package_excerpt,
    title_body_overlap,
)
from m3.research.documents import (
    extract_html_text,
    extract_pdf_text,
    find_attachment_urls,
)
from m3.research.full_text_store import load_full_text, store_full_text
from m3.research.http_util import fetch_bytes, fetch_text, validate_public_https_url
from m3.research.nextbid_recovery import collect_population

SKIP_HOST_FRAGMENTS = (
    "mesaaz.gov",  # historically hung / junk portals
    "phoenix.gov",
    "login.",
    "signin.",
    "auth.",
)
RESTRICTED_MARKERS = (
    "sign in",
    "log in",
    "login",
    "access denied",
    "unauthorized",
    "vendor registration required",
    "create an account",
)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(msg: str) -> None:
    print(f"[free-doc-recovery] {msg}", flush=True)


def _checkpoint_path() -> Path:
    p = get_settings().data_root() / "nextbid_checkpoints" / "free_doc_recovery_v1.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load_checkpoint() -> dict[str, Any]:
    path = _checkpoint_path()
    if not path.exists():
        return {"version": 1, "done": {}, "updated_at": None}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "done": {}, "updated_at": None}


def save_checkpoint(ck: dict[str, Any]) -> None:
    ck["updated_at"] = _utc()
    path = _checkpoint_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(ck, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _host_ok(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    return not any(x in host for x in SKIP_HOST_FRAGMENTS)


def _priority_score(it: dict[str, Any]) -> int:
    score = 0
    req = str(it.get("requirement_class") or "").lower()
    status = str(it.get("solicitation_status") or it.get("lifecycle_status") or "").lower()
    title = str(it.get("title") or "")
    if status in {"open", "amended", "active"}:
        score += 50
    if req in {"product_supply", "product_with_incidental_services"}:
        score += 40
    elif req in {"unknown_mixed", "unknown", "pending_classification", ""}:
        score += 20
    if it.get("quantity"):
        score += 25
    if re_search_product(title):
        score += 15
    # Prefer thinner text (need recovery)
    doclen = int(it.get("document_text_len") or len(str(it.get("document_text") or it.get("description") or "")))
    if doclen < 500:
        score += 10
    if doclen >= 4000:
        score -= 20
    return score


def re_search_product(title: str) -> bool:
    import re

    return bool(
        re.search(
            r"(?i)\b(purchase|furnish|supply|equipment|supplies|materials?|goods|"
            r"vehicle|pump|generator|trailer|furniture|hardware)\b",
            title or "",
        )
    )


def re_search_sol_body(text: str) -> bool:
    import re

    return bool(
        re.search(
            r"(?i)("
            r"solicitation|invitation\s+for\s+bids?|request\s+for\s+(?:proposal|quote|bid)|"
            r"\bIFB\b|\bRFP\b|\bRFB\b|\bRFQ\b|bid\s+opening|scope\s+of\s+(?:work|supply)|"
            r"line\s+item|unit\s+price|submit\s+(?:bids?|proposals?)"
            r")",
            text or "",
        )
    )


def _select_targets(items: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    ranked = []
    for it in items:
        req = str(it.get("requirement_class") or "").lower()
        # Skip confirmed non-fits
        if req in {
            "service",
            "construction",
            "software_saas",
            "not_applicable_non_solicitation",
        }:
            continue
        if str(it.get("solicitation_status") or "").lower() in {
            "closed",
            "cancelled",
            "historical_award",
            "closed_by_deadline",
            "forecast",
        }:
            continue
        url = str(it.get("official_url") or it.get("source_url") or "").strip()
        if not url.startswith("http"):
            continue
        if not _host_ok(url):
            continue
        ranked.append(( _priority_score(it), it))
    ranked.sort(key=lambda x: -x[0])
    return [it for _, it in ranked[:limit]]


def _fetch_page(url: str, *, timeout: float = 12.0) -> dict[str, Any]:
    try:
        validate_public_https_url(url)
    except Exception as exc:
        return {"ok": False, "reason": f"url_invalid:{exc}"}
    try:
        status, final, text = fetch_text(url, timeout=timeout)
    except Exception as exc:
        return {"ok": False, "reason": f"fetch_error:{type(exc).__name__}"}
    if status in {401, 403}:
        return {
            "ok": False,
            "restricted": True,
            "reason": f"restricted_http_{status}",
            "status": status,
            "final_url": final,
        }
    if status >= 400:
        return {"ok": False, "reason": f"http_{status}", "status": status, "final_url": final}
    plain = extract_html_text(text or "")
    low = (plain or "")[:4000].lower()
    quality = classify_document_text(plain, fmt="html")
    has_body = bool(
        quality.get("ok")
        or re_search_sol_body(plain)
    )
    # Portal chrome often mentions "sign in" / vendor registration even on public pages.
    # Only treat as restricted when login wall dominates AND no solicitation body.
    if any(m in low for m in RESTRICTED_MARKERS) and not has_body:
        return {
            "ok": False,
            "restricted": True,
            "reason": f"restricted_login_wall_http_{status}",
            "status": status,
            "final_url": final,
        }
    if quality.get("is_junk") or is_junk_package_excerpt(plain[:5000]):
        return {
            "ok": False,
            "reason": "junk_portal_html",
            "status": status,
            "final_url": final,
            "chars": len(plain),
        }
    return {
        "ok": True,
        "status": status,
        "final_url": final,
        "html": text,
        "text": plain,
        "chars": len(plain),
        "quality": quality,
    }


def _fetch_attachment(url: str, *, timeout: float = 15.0) -> dict[str, Any]:
    try:
        validate_public_https_url(url)
        status, final, data, ctype = fetch_bytes(url, timeout=timeout)
    except Exception as exc:
        return {"ok": False, "reason": type(exc).__name__}
    if status >= 400 or not data:
        return {"ok": False, "reason": f"http_{status}", "status": status}
    low = url.lower()
    if ".pdf" in low or (ctype or "").lower().startswith("application/pdf") or data[:4] == b"%PDF":
        ex = extract_pdf_text(data, allow_ocr=False)
        if ex.get("ok") and ex.get("text"):
            return {"ok": True, "text": ex["text"], "format": "pdf", "url": final, "chars": len(ex["text"])}
        return {"ok": False, "reason": "pdf_extract_failed"}
    # Treat as text/html fallback
    try:
        plain = extract_html_text(data.decode("utf-8", errors="ignore"))
    except Exception:
        return {"ok": False, "reason": "decode_failed"}
    if len(plain) < 80 or is_junk_package_excerpt(plain[:4000]):
        return {"ok": False, "reason": "attachment_junk_or_short"}
    return {"ok": True, "text": plain, "format": "other", "url": final, "chars": len(plain)}


def recover_free_documents(
    db: Session,
    *,
    limit: int = 180,
    max_attachments_per: int = 2,
    host_delay_sec: float = 0.35,
) -> dict[str, Any]:
    """Recover public official docs for existing priority solicitations."""
    t0 = time.time()
    pop = collect_population()
    items = list(pop["by_id"].values())
    # Prefer requirement_class from last pipeline artifact when present
    pop_path = get_settings().data_root() / "acceptance" / "nextbid_pipeline_repair_population.jsonl"
    by_nb_class: dict[str, dict[str, Any]] = {}
    if pop_path.exists():
        for line in pop_path.open(encoding="utf-8"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("nb_id"):
                by_nb_class[str(r["nb_id"])] = r
    for it in items:
        nb = str(it.get("nb_id") or "")
        prior = by_nb_class.get(nb) or {}
        if prior.get("requirement_class") and not it.get("requirement_class"):
            it["requirement_class"] = prior["requirement_class"]
        if prior.get("quantity") and not it.get("quantity"):
            it["quantity"] = prior["quantity"]
        if prior.get("solicitation_status"):
            it["solicitation_status"] = prior["solicitation_status"]
        if prior.get("document_text_len"):
            it["document_text_len"] = prior["document_text_len"]

    targets = _select_targets(items, limit=limit)
    ck = load_checkpoint()
    done = dict(ck.get("done") or {})

    stats: dict[str, int] = defaultdict(int)
    recovered_ids: list[str] = []
    restricted_ids: list[str] = []
    last_host_at: dict[str, float] = {}

    _log(f"targets={len(targets)} checkpoint_done={len(done)}")

    for i, it in enumerate(targets, 1):
        nb = str(it.get("nb_id") or "")
        url = str(it.get("official_url") or it.get("source_url") or "").strip()
        title = str(it.get("title") or "")
        if not nb or not url:
            continue
        prev = done.get(nb) or {}
        if prev.get("status") in {"recovered", "restricted", "unchanged_skip"} and not prev.get("retry"):
            stats["skipped_checkpoint"] += 1
            continue
        # Skip if we already have genuine long matching text
        existing = load_full_text(nb)
        exist_text = str((existing or {}).get("full_text") or "")
        if (
            len(exist_text) >= 2500
            and not is_junk_package_excerpt(exist_text[:5000])
            and title_body_overlap(title, exist_text[:8000]).get("ok")
            and not (existing or {}).get("is_listing_preview_only")
        ):
            done[nb] = {"status": "unchanged_skip", "chars": len(exist_text), "at": _utc()}
            stats["unchanged_skip"] += 1
            continue

        host = (urlparse(url).hostname or "").lower()
        wait = host_delay_sec - (time.time() - last_host_at.get(host, 0))
        if wait > 0:
            time.sleep(min(wait, 2.0))
        last_host_at[host] = time.time()

        if i % 10 == 1 or i == len(targets):
            _log(f"progress {i}/{len(targets)} recovered={stats['recovered']} restricted={stats['restricted']}")

        page = _fetch_page(url, timeout=12.0)
        if page.get("restricted"):
            stats["restricted"] += 1
            restricted_ids.append(nb)
            done[nb] = {"status": "restricted", "reason": page.get("reason"), "at": _utc()}
            continue
        if not page.get("ok"):
            stats["failed"] += 1
            done[nb] = {"status": "failed", "reason": page.get("reason"), "at": _utc()}
            continue

        parts = [str(page.get("text") or "")]
        att_ok = 0
        for att in (find_attachment_urls(page.get("html") or "", page.get("final_url") or url) or [])[
            :max_attachments_per
        ]:
            if not _host_ok(att):
                continue
            ah = (urlparse(att).hostname or "").lower()
            wait = host_delay_sec - (time.time() - last_host_at.get(ah, 0))
            if wait > 0:
                time.sleep(min(wait, 2.0))
            last_host_at[ah] = time.time()
            ares = _fetch_attachment(att, timeout=15.0)
            if ares.get("ok") and ares.get("text"):
                parts.append(str(ares["text"]))
                att_ok += 1
                stats["attachments_recovered"] += 1

        combined = "\n\n".join(p for p in parts if p and len(p) >= 40)
        if len(combined) < 120 or is_junk_package_excerpt(combined[:5000]):
            stats["rejected_quality"] += 1
            done[nb] = {"status": "rejected_quality", "chars": len(combined), "at": _utc()}
            continue
        overlap = title_body_overlap(title, combined[:12000])
        if not overlap.get("ok") and len(combined) > 800:
            # Keep only if solicitation body markers present
            q = classify_document_text(combined)
            if not q.get("ok"):
                stats["rejected_mismatch"] += 1
                done[nb] = {"status": "rejected_mismatch", "overlap": overlap, "at": _utc()}
                continue

        store_full_text(
            nb,
            combined,
            source="free_official_url_recovery",
            title=title,
        )
        stats["recovered"] += 1
        recovered_ids.append(nb)
        done[nb] = {
            "status": "recovered",
            "chars": len(combined),
            "attachments": att_ok,
            "url": url,
            "at": _utc(),
        }
        if i % 25 == 0:
            ck["done"] = done
            save_checkpoint(ck)

    ck["done"] = done
    save_checkpoint(ck)
    elapsed = round(time.time() - t0, 2)
    _log(f"done recovered={stats['recovered']} restricted={stats['restricted']} elapsed={elapsed}s")
    return {
        "targets": len(targets),
        "stats": dict(stats),
        "recovered_ids_sample": recovered_ids[:40],
        "restricted_ids_sample": restricted_ids[:40],
        "recovered_count": int(stats["recovered"]),
        "restricted_count": int(stats["restricted"]),
        "elapsed_sec": elapsed,
        "checkpoint": str(_checkpoint_path()),
        "metered_nextbid": 0,
        "openai_usd": 0,
    }
