"""Associate solicitation freeze rows with stored document packages (Build 002Q).

Root cause of prior zero package recovery:
- Most product rows already share external_key with packages; association worked.
- Stored excerpts were often portal chrome / stubs (e.g. Mesa employee portal HTML),
  so quantity extraction returned nothing — not a missing key join.
- A minority used alternate keys (Georgia gt:ga:… vs hash, SF missing packages,
  truncated query strings) and need SN / normalized-URL / opportunity-id joins.

Does not download. Recovers evidence only from existing DB rows + local blobs.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from sqlalchemy.orm import Session, joinedload

from m3.models import SolicitationPackage
from m3.research.documents import extract_pdf_text
from m3.research.reprocess import collect_package_text

_ID_QUERY_KEYS = (
    "id",
    "number",
    "folder",
    "folder_name",
    "bid",
    "bidid",
    "doc",
    "docid",
    "event",
    "esource",
    "esourcenumber",
    "solicitation",
    "opportunity",
    "year",
    "sid",
)


def normalize_official_url(url: str | None) -> str:
    """Stable URL key: host+path + meaningful id query params only."""
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        p = urlparse(raw)
    except Exception:
        return raw.lower()
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (p.path or "").rstrip("/").lower()
    q = parse_qs(p.query, keep_blank_values=False)
    keep: dict[str, str] = {}
    for k, vals in q.items():
        kl = k.lower()
        if any(tok in kl for tok in _ID_QUERY_KEYS):
            keep[kl] = (vals[0] if vals else "").lower()
    if keep:
        qs = "&".join(f"{k}={keep[k]}" for k in sorted(keep))
        return f"{host}{path}?{qs}"
    return f"{host}{path}"


def opportunity_id_from_url(url: str | None) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    low = raw.lower()
    for marker in ("esourcenumber=", "folder_name=", "bidid=", "docid=", "id="):
        idx = low.find(marker)
        if idx >= 0:
            return raw[idx + len(marker) :].split("&")[0].strip()
    return ""


def _is_junk_text(text: str) -> bool:
    from m3.research.document_quality import is_junk_package_excerpt

    return is_junk_package_excerpt(text)


def _hint_quantity_fields(pkg: SolicitationPackage) -> dict[str, Any]:
    hint = pkg.classification_hint or {}
    detail = pkg.detail_fields or {}
    qty = hint.get("quantity") or detail.get("extracted_quantity")
    if qty in (None, "", "unknown"):
        pipe = hint.get("procurement_pipeline") or {}
        qty = pipe.get("quantity")
    if qty in (None, "", "unknown"):
        return {}
    out: dict[str, Any] = {
        "quantity": qty,
        "quantity_kind": hint.get("quantity_kind")
        or detail.get("extracted_quantity_kind")
        or "fixed",
        "quantity_semantic": hint.get("quantity_semantic")
        or (hint.get("procurement_pipeline") or {}).get("quantity_semantic")
        or "ORDERED_QUANTITY",
        "quantity_evidence": list(
            hint.get("quantity_evidence")
            or detail.get("extracted_quantity_evidence")
            or (hint.get("procurement_pipeline") or {}).get("quantity_evidence")
            or []
        )[:8]
        + ["recovered_from_package_classification_hint"],
    }
    return out


def recover_text_from_package(pkg: SolicitationPackage, *, max_chars: int = 80_000) -> dict[str, Any]:
    """Prefer stored excerpts; re-read local PDF blobs when excerpts are junk/thin."""
    excerpt = collect_package_text(pkg, max_chars=max_chars)
    source = "package_excerpt"
    text = excerpt
    if _is_junk_text(excerpt):
        parts: list[str] = []
        for doc in pkg.documents or []:
            path = doc.storage_path
            if not path:
                continue
            p = Path(path)
            if not p.exists():
                continue
            try:
                data = p.read_bytes()
            except OSError:
                continue
            if not data.startswith(b"%PDF"):
                # HTML blob — skip chrome; keep only if longer unique body later
                continue
            extracted = extract_pdf_text(data, allow_ocr=False)
            if extracted.get("ok") and extracted.get("text"):
                parts.append(str(extracted["text"]))
            if sum(len(x) for x in parts) >= max_chars:
                break
        if parts:
            text = "\n\n".join(parts)[:max_chars]
            source = "reextracted_pdf_blob"
        else:
            source = "junk_or_thin_excerpt_no_pdf"
    return {
        "text": text or "",
        "text_source": source,
        "document_text_len": len((text or "").strip()),
        "junk_excerpt": _is_junk_text(excerpt),
        "package_status": pkg.package_status,
        "downloaded_count": pkg.downloaded_count or 0,
        "external_key": pkg.external_key,
        "solicitation_number": pkg.solicitation_number,
        "official_url": pkg.authoritative_url,
        "title": pkg.title,
        "source_key": pkg.source_key,
        **_hint_quantity_fields(pkg),
    }


def build_package_association_index(
    db: Session,
    *,
    include_empty_downloads: bool = True,
) -> dict[str, Any]:
    """Index all packages by external_key, SN, normalized URL, opportunity id."""
    q = db.query(SolicitationPackage).options(joinedload(SolicitationPackage.documents))
    if not include_empty_downloads:
        q = q.filter(SolicitationPackage.downloaded_count > 0)
    rows = q.order_by(
        SolicitationPackage.downloaded_count.desc(),
        SolicitationPackage.id.desc(),
    ).all()

    by_ek: dict[str, SolicitationPackage] = {}
    by_sn: dict[str, SolicitationPackage] = {}
    by_nurl: dict[str, SolicitationPackage] = {}
    by_oid: dict[str, SolicitationPackage] = {}
    by_identity: dict[str, SolicitationPackage] = {}

    def _prefer(existing: SolicitationPackage | None, pkg: SolicitationPackage) -> SolicitationPackage:
        if existing is None:
            return pkg
        # Prefer packages with more downloaded docs / COMPLETE status
        e_dl = existing.downloaded_count or 0
        p_dl = pkg.downloaded_count or 0
        if p_dl != e_dl:
            return pkg if p_dl > e_dl else existing
        e_ok = 1 if (existing.package_status or "") == "COMPLETE" else 0
        p_ok = 1 if (pkg.package_status or "") == "COMPLETE" else 0
        return pkg if p_ok > e_ok else existing

    for pkg in rows:
        ek = str(pkg.external_key or "").strip()
        if ek:
            by_ek[ek] = _prefer(by_ek.get(ek), pkg)
        sn = str(pkg.solicitation_number or "").strip().lower()
        if sn:
            by_sn[sn] = _prefer(by_sn.get(sn), pkg)
        nurl = normalize_official_url(pkg.authoritative_url)
        if nurl:
            by_nurl[nurl] = _prefer(by_nurl.get(nurl), pkg)
        oid = opportunity_id_from_url(pkg.authoritative_url)
        if oid:
            by_oid[oid.lower()] = _prefer(by_oid.get(oid.lower()), pkg)
        ik = str(pkg.identity_key or "").strip()
        if ik:
            by_identity[ik] = _prefer(by_identity.get(ik), pkg)

    return {
        "packages": rows,
        "by_ek": by_ek,
        "by_sn": by_sn,
        "by_nurl": by_nurl,
        "by_oid": by_oid,
        "by_identity": by_identity,
        "stats": {
            "packages_total": len(rows),
            "with_downloads": sum(1 for p in rows if (p.downloaded_count or 0) > 0),
            "keys_ek": len(by_ek),
            "keys_sn": len(by_sn),
            "keys_nurl": len(by_nurl),
            "keys_oid": len(by_oid),
        },
    }


def resolve_package_for_item(
    item: dict[str, Any],
    index: dict[str, Any],
) -> tuple[SolicitationPackage | None, str]:
    """Return (package, match_method)."""
    by_ek = index["by_ek"]
    by_sn = index["by_sn"]
    by_nurl = index["by_nurl"]
    by_oid = index["by_oid"]
    by_identity = index["by_identity"]

    ek = str(item.get("external_key") or "").strip()
    if ek and ek in by_ek:
        return by_ek[ek], "external_key"

    # identity_key form used by package engine
    if ek and f"ek:{ek}" in by_identity:
        return by_identity[f"ek:{ek}"], "identity_key_ek"

    sn = str(item.get("solicitation_number") or "").strip().lower()
    if sn and sn in by_sn:
        return by_sn[sn], "solicitation_number"

    url = str(item.get("official_url") or item.get("authoritative_url") or "").strip()
    nurl = normalize_official_url(url)
    if nurl and nurl in by_nurl:
        return by_nurl[nurl], "normalized_url"

    oid = opportunity_id_from_url(url)
    if oid and oid.lower() in by_oid:
        return by_oid[oid.lower()], "opportunity_id"

    src = str(item.get("source_key") or "").strip()
    if sn and src:
        ik = f"sn:{src}:{item.get('solicitation_number')}".strip()[:240]
        if ik in by_identity:
            return by_identity[ik], "identity_key_sn"

    return None, "unmatched"


def reconcile_population_to_packages(
    db: Session,
    population: list[dict[str, Any]],
    *,
    recover_blobs: bool = True,
) -> dict[str, Any]:
    """Join every freeze row to best package; recover text/qty evidence when present."""
    index = build_package_association_index(db, include_empty_downloads=True)
    print(f"package_association_index {index['stats']}", flush=True)

    match_methods: Counter[str] = Counter()
    text_sources: Counter[str] = Counter()
    evidence_by_ek: dict[str, dict[str, Any]] = {}
    # Also index evidence under all association keys for qualify lookup
    evidence_lookup: dict[str, dict[str, Any]] = {}

    linked = 0
    with_text = 0
    with_qty_hint = 0
    pdf_reextracted = 0
    junk_excerpts = 0

    for i, raw in enumerate(population):
        if (i + 1) % 5000 == 0:
            print(f"package_reconcile_progress {i+1}/{len(population)}", flush=True)
        pkg, method = resolve_package_for_item(raw, index)
        match_methods[method] += 1
        if not pkg:
            continue
        linked += 1
        if recover_blobs:
            meta = recover_text_from_package(pkg)
        else:
            text = collect_package_text(pkg, max_chars=50_000)
            meta = {
                "text": text,
                "text_source": "package_excerpt",
                "document_text_len": len(text.strip()),
                "junk_excerpt": _is_junk_text(text),
                "package_status": pkg.package_status,
                "downloaded_count": pkg.downloaded_count or 0,
                "external_key": pkg.external_key,
                "solicitation_number": pkg.solicitation_number,
                "official_url": pkg.authoritative_url,
                **_hint_quantity_fields(pkg),
            }
        text_sources[str(meta.get("text_source"))] += 1
        if meta.get("junk_excerpt"):
            junk_excerpts += 1
        if meta.get("text_source") == "reextracted_pdf_blob":
            pdf_reextracted += 1
        if (meta.get("document_text_len") or 0) >= 80 and not meta.get("junk_excerpt"):
            with_text += 1
        if meta.get("quantity") not in (None, "", "unknown"):
            with_qty_hint += 1

        # Freeze row key → evidence (prefer freeze external_key)
        freeze_ek = str(raw.get("external_key") or "").strip()
        payload = {
            **meta,
            "match_method": method,
            "package_external_key": pkg.external_key,
            "associated_freeze_external_key": freeze_ek,
        }
        if freeze_ek:
            evidence_by_ek[freeze_ek] = payload
            evidence_lookup[freeze_ek] = payload
        for key in (
            str(pkg.external_key or "").strip(),
            str(pkg.authoritative_url or "").strip(),
            str(pkg.solicitation_number or "").strip(),
            normalize_official_url(pkg.authoritative_url),
            normalize_official_url(raw.get("official_url")),
            opportunity_id_from_url(pkg.authoritative_url),
            opportunity_id_from_url(raw.get("official_url")),
            str(raw.get("official_url") or "").strip(),
            str(raw.get("solicitation_number") or "").strip(),
        ):
            if key and key not in evidence_lookup:
                evidence_lookup[key] = payload

    return {
        "population": len(population),
        "packages_total": index["stats"]["packages_total"],
        "packages_with_downloads": index["stats"]["with_downloads"],
        "linked": linked,
        "unmatched": match_methods.get("unmatched", 0),
        "match_methods": dict(match_methods),
        "with_usable_text": with_text,
        "with_quantity_hint": with_qty_hint,
        "pdf_reextracted": pdf_reextracted,
        "junk_excerpts": junk_excerpts,
        "text_sources": dict(text_sources),
        "evidence_by_freeze_ek": evidence_by_ek,
        "evidence_lookup": evidence_lookup,
        "index_stats": index["stats"],
    }
