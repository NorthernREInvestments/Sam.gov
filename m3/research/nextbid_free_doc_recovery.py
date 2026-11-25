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


# --- Staged-inventory document acquisition (free, direct official URLs) -------------------

# Disabled or metered programs: never fetched by free recovery
EXCLUDED_HOST_FRAGMENTS = ("sam.gov", "dibbs", "dla.mil", "nextbid", "api.", "bidnetdirect.com")
DOC_EXTS = (".pdf", ".docx", ".xlsx", ".xls", ".csv", ".txt", ".doc")
STAGED_UA = "M3ProcurementResearch/1.0 (+supplier bid research; respects robots.txt)"


class _Robots:
    """Per-host robots.txt cache. 404/410 → allowed; other failures → host skipped."""

    def __init__(self, fetch: Any):
        self._fetch = fetch
        self._cache: dict[str, Any] = {}

    def allowed(self, url: str) -> bool:
        from urllib.robotparser import RobotFileParser

        u = urlparse(url)
        key = f"{u.scheme}://{u.netloc}"
        if key not in self._cache:
            try:
                status, _final, text = self._fetch(f"{key}/robots.txt")
            except Exception:
                status, text = 0, ""
            if status == 200:
                rp = RobotFileParser()
                rp.parse((text or "").splitlines())
                rp.modified()  # can_fetch() denies everything until a fetch time is recorded
                self._cache[key] = rp
            else:
                self._cache[key] = True if status in (404, 410) else False
        rp = self._cache[key]
        if isinstance(rp, bool):
            return rp
        return rp.can_fetch(STAGED_UA, url)


def _extract_any(data: bytes, url: str, ctype: str) -> dict[str, Any]:
    from m3.research.documents import extract_csv_or_txt, extract_docx_text, extract_xlsx_text, guess_format

    fmt = guess_format(url, ctype, data[:32])
    if fmt == "pdf":
        return {**extract_pdf_text(data, allow_ocr=False), "format": "pdf"}
    if fmt == "docx":
        return {**extract_docx_text(data), "format": "docx"}
    if fmt == "xlsx":
        return {**extract_xlsx_text(data), "format": "xlsx"}
    if fmt == "text":
        return {**extract_csv_or_txt(data), "format": "text"}
    if fmt == "html":
        text = extract_html_text(data.decode("utf-8", errors="replace"))
        return {"ok": len(text) >= 80, "text": text, "format": "html"}
    return {"ok": False, "text": "", "format": fmt, "reason": "unsupported_format"}


def select_staged_document_targets(rows: list[dict[str, Any]], *, now: datetime, min_days: float = 2.0) -> list[dict[str, Any]]:
    """Open, non-federal, product-detected records lacking document evidence, most valuable first."""
    out = []
    for r in rows:
        cl = r.get("classification") or {}
        if r.get("duplicate_of") or r.get("retirement_reason") or r.get("federal_policy_hold"):
            continue
        if cl.get("product_presence") not in ("EXPLICIT_PRODUCT", "LIKELY_PRODUCT"):
            continue
        if cl.get("evidence_completeness") == "DOCUMENT":
            continue
        url = str(r.get("official_url") or "")
        host = (urlparse(url).hostname or "").lower()
        if not url.startswith("http") or not host or any(x in host for x in EXCLUDED_HOST_FRAGMENTS + SKIP_HOST_FRAGMENTS):
            continue
        dl = r.get("deadline_at")
        try:
            d = datetime.fromisoformat(str(dl).replace("Z", "+00:00")) if dl else None
        except ValueError:
            d = None
        if d is None or (d - now).total_seconds() < min_days * 86400:
            continue
        days = (d - now).total_seconds() / 86400
        score = (0 if cl.get("product_presence") == "EXPLICIT_PRODUCT" else 1, 0 if days <= 14 else 1 if days <= 30 else 2, days)
        out.append((score, r))
    out.sort(key=lambda x: x[0])
    return [r for _, r in out]


def recover_staged_documents(
    targets: list[dict[str, Any]],
    *,
    out_root: Path,
    max_records: int = 2000,
    max_attachments_per: int = 4,
    host_delay_sec: float = 2.0,
    fetch_text_fn: Any = None,
    fetch_bytes_fn: Any = None,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    """Fetch official pages + attachments for staged targets; store raw files by sha256 and
    extracted text in ``doc_text.jsonl``. Resumable via ``doc_checkpoint.json``. Downloaded files are
    stored as inert bytes and only parsed by the text extractors; their content is procurement data."""
    import hashlib

    client = None
    if fetch_text_fn is None or fetch_bytes_fn is None:
        import httpx

        client = httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=True,
                              headers={"User-Agent": STAGED_UA})

    def _ft(u: str) -> tuple[int, str, str]:
        r = client.get(validate_public_https_url(u))
        validate_public_https_url(str(r.url))
        return r.status_code, str(r.url), r.text[:3_000_000]

    def _fb(u: str) -> tuple[int, str, bytes, str]:
        with client.stream("GET", validate_public_https_url(u)) as r:
            validate_public_https_url(str(r.url))
            buf, total = [], 0
            for chunk in r.iter_bytes():
                buf.append(chunk)
                total += len(chunk)
                if total >= 25_000_000:
                    break
            return r.status_code, str(r.url), b"".join(buf), r.headers.get("content-type", "")

    ft = fetch_text_fn or _ft
    fb = fetch_bytes_fn or _fb
    out_root.mkdir(parents=True, exist_ok=True)
    raw_dir = out_root / "raw"
    raw_dir.mkdir(exist_ok=True)
    ck_p = out_root / "doc_checkpoint.json"
    ck = json.loads(ck_p.read_text(encoding="utf-8")) if ck_p.exists() else {"done": {}}
    done: dict[str, Any] = ck["done"]
    robots = _Robots(ft)
    last_host: dict[str, float] = {}
    stats: dict[str, int] = defaultdict(int)

    def _pace(url: str) -> None:
        h = (urlparse(url).hostname or "").lower()
        wait = host_delay_sec - (time.monotonic() - last_host.get(h, -1e9))
        if wait > 0:
            sleep(wait)
        last_host[h] = time.monotonic()

    # Interleave hosts so per-host pacing rarely blocks
    by_host: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in targets[:max_records]:
        by_host[(urlparse(str(t.get("official_url"))).hostname or "").lower()].append(t)
    order: list[dict[str, Any]] = []
    while any(by_host.values()):
        for h in list(by_host):
            if by_host[h]:
                order.append(by_host[h].pop(0))

    with open(out_root / "doc_text.jsonl", "a", encoding="utf-8") as fh:
        for n, rec in enumerate(order, 1):
            sid = str(rec.get("staged_id") or rec.get("source_record_id"))
            if sid in done:
                stats["skipped_checkpoint"] += 1
                continue
            url = str(rec.get("official_url"))
            if not robots.allowed(url):
                done[sid] = {"status": "robots_disallow", "at": _utc()}
                stats["robots_disallow"] += 1
                continue
            _pace(url)
            page = _fetch_page_with(ft, url)
            if not page.get("ok"):
                st = "restricted" if page.get("restricted") else "failed"
                done[sid] = {"status": st, "reason": page.get("reason"), "at": _utc()}
                stats[st] += 1
            else:
                docs = []
                atts = [a for a in find_attachment_urls(page.get("html") or "", page.get("final_url") or url)
                        if not any(x in (urlparse(a).hostname or "").lower() for x in EXCLUDED_HOST_FRAGMENTS)]
                atts.sort(key=lambda a: 0 if a.lower().split("?")[0].endswith(DOC_EXTS) else 1)
                for att in atts[:max_attachments_per]:
                    if not robots.allowed(att):
                        docs.append({"url": att, "status": "robots_disallow"})
                        continue
                    _pace(att)
                    try:
                        status, final, data, ctype = fb(att)
                    except Exception as exc:
                        docs.append({"url": att, "status": f"error:{type(exc).__name__}"})
                        continue
                    if status >= 400 or not data:
                        docs.append({"url": att, "status": f"http_{status}"})
                        continue
                    if len(data) >= 25_000_000:
                        docs.append({"url": att, "status": "too_large_truncated"})
                        continue
                    digest = hashlib.sha256(data).hexdigest()
                    try:
                        ex = _extract_any(data, final, ctype)
                    except Exception as exc:
                        ex = {"ok": False, "text": "", "format": "unknown", "reason": f"extract_error:{type(exc).__name__}"}
                    if ex.get("format") in ("pdf", "docx", "xlsx", "text"):
                        ext = {"pdf": ".pdf", "docx": ".docx", "xlsx": ".xlsx", "text": ".txt"}[ex["format"]]
                        p = raw_dir / f"{digest}{ext}"
                        if not p.exists():
                            p.write_bytes(data)
                    text = str(ex.get("text") or "")
                    junk = is_junk_package_excerpt(text[:4000]) if text else True
                    docs.append({"url": final, "status": "extracted" if ex.get("ok") and not junk else "extract_failed_or_junk",
                                 "format": ex.get("format"), "sha256": digest, "bytes": len(data),
                                 "content_type": ctype[:80], "chars": len(text)})
                    if ex.get("ok") and not junk:
                        fh.write(json.dumps({"staged_id": sid, "source_record_id": rec.get("source_record_id"),
                                             "kind": "attachment", "url": final, "sha256": digest,
                                             "format": ex.get("format"), "text": text[:200_000], "at": _utc()}) + "\n")
                        stats["attachments_extracted"] += 1
                body = str(page.get("text") or "")
                if body:
                    fh.write(json.dumps({"staged_id": sid, "source_record_id": rec.get("source_record_id"),
                                         "kind": "page", "url": page.get("final_url") or url,
                                         "text": body[:100_000], "at": _utc()}) + "\n")
                extracted = sum(1 for d in docs if d["status"] == "extracted")
                done[sid] = {"status": "documents" if extracted else "page_only", "page_chars": len(body),
                             "attachments_found": len(atts), "documents": docs, "at": _utc()}
                stats["with_documents" if extracted else "page_only"] += 1
            if n % 20 == 0:
                fh.flush()
                ck_p.write_text(json.dumps(ck, default=str), encoding="utf-8")
                _log(f"staged docs {n}/{len(order)} {dict(stats)}")
    ck_p.write_text(json.dumps(ck, default=str), encoding="utf-8")
    if client is not None:
        client.close()
    return {"targets": len(order), "stats": dict(stats), "checkpoint": str(ck_p), "metered_calls": 0}


def _fetch_page_with(ft: Any, url: str) -> dict[str, Any]:
    try:
        status, final, text = ft(url)
    except Exception as exc:
        return {"ok": False, "reason": f"fetch_error:{type(exc).__name__}"}
    if status in {401, 403}:
        return {"ok": False, "restricted": True, "reason": f"restricted_http_{status}"}
    if status >= 400:
        return {"ok": False, "reason": f"http_{status}"}
    plain = extract_html_text(text or "")
    has_body = bool(classify_document_text(plain, fmt="html").get("ok") or re_search_sol_body(plain))
    if any(m in plain[:4000].lower() for m in RESTRICTED_MARKERS) and not has_body:
        if not find_attachment_urls(text or "", final):
            return {"ok": False, "restricted": True, "reason": "restricted_login_wall"}
    return {"ok": True, "final_url": final, "html": text, "text": plain}


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
