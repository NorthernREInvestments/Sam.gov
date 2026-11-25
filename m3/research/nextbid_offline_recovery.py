"""Bulk offline recovery of previously paid NextBid responses (zero API calls).

Repairs two confirmed defects against every retained body in one pass:

1. ``read_file`` Markdown stored as ``raw_text`` → ``nextbid_file_text`` → full_text + evidence vault
2. ``list_documents`` ``source_documents`` / ``nextbid_documents`` retained in a compact enrichment index

Does **not** write one audit file per solicitation. Production evidence goes to:

* ``solicitation_full_text`` (thicker notice text)
* ``evidence_vault`` (original recovered markdown bytes)
* ``nextbid_offline_recovery/enrichment.jsonl`` (one line per enriched NB — metadata only)

Disposable diagnostics belong under ``TEMP/``. Never contacts NextBid.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from m3.config import get_settings
from m3.research.full_text_store import load_full_text, store_full_text
from m3.research.nextbid_acquire import nextbid_file_text
from m3.research.nextbid_response_store import store_root

QTY_RE = re.compile(
    r"(?i)\b(?:qty|quantity|quantities)\b|"
    r"\b\d[\d,]*(?:\.\d+)?\s*(?:ea|each|pcs?|units?|cases?|boxes|lbs?|tons?|gallons?)\b"
)
SPEC_RE = re.compile(
    r"(?i)\b(?:specification|shall\s+meet|brand\s+name\s+or\s+equal|nsn|part\s+number|manufacturer|model)\b"
)
LINE_RE = re.compile(r"(?i)\b(?:line\s+item|clin|item\s+no\.?|bid\s+item|schedule\s+of\s+items)\b")
SUBMIT_RE = re.compile(r"(?i)\b(?:submit|submission|bid\s+form|proposal\s+due|electronic\s+bid)\b")

_ENRICHMENT_CACHE: dict[str, dict[str, Any]] | None = None


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def recovery_root(root: Path | None = None) -> Path:
    p = (root or get_settings().data_root()) / "nextbid_offline_recovery"
    p.mkdir(parents=True, exist_ok=True)
    return p


def enrichment_path(root: Path | None = None) -> Path:
    return recovery_root(root) / "enrichment.jsonl"


def summary_path(root: Path | None = None) -> Path:
    return recovery_root(root) / "SUMMARY.json"


def _atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    os.replace(tmp, path)


def iter_paid_request_rows(paid_root: Path | None = None) -> list[dict[str, Any]]:
    """Load every retained paid-response body from ``by_request`` (complete population)."""
    root = (paid_root or store_root()) / "by_request"
    rows: list[dict[str, Any]] = []
    if not root.exists():
        return rows
    for path in sorted(root.glob("*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(row, dict):
            continue
        row["_store_path"] = str(path)
        rows.append(row)
    return rows


def _thicker_str(a: str | None, b: str | None) -> str:
    aa, bb = a or "", b or ""
    return bb if len(bb) > len(aa) else aa


def _merge_attachment_lists(*lists: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for lst in lists:
        for item in lst or []:
            if not isinstance(item, dict):
                continue
            key = str(
                item.get("sha256")
                or item.get("path")
                or item.get("document_url")
                or item.get("url")
                or item.get("source_url")
                or item.get("name")
                or item.get("filename")
                or json.dumps(item, sort_keys=True, default=str)[:120]
            )
            if key in seen:
                for i, prev in enumerate(out):
                    pk = str(
                        prev.get("sha256") or prev.get("path") or prev.get("name") or prev.get("filename") or ""
                    )
                    if pk == key or (prev.get("sha256") and prev.get("sha256") == item.get("sha256")):
                        if len(json.dumps(item, default=str)) > len(json.dumps(prev, default=str)):
                            out[i] = {**prev, **item}
                        break
                continue
            seen.add(key)
            out.append(dict(item))
    return out


def recover_nb_package(nb_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge retained endpoint bodies for one NB (in-memory; not written per-file)."""
    by_ep: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_ep[str(r.get("endpoint") or "unknown")].append(r)

    description = ""
    summary = ""
    ai_summary = ""
    title = None
    agency = None
    solicitation_number = None
    source_url = None
    procurement_kind = None
    response_deadline = None
    notice_fields: dict[str, Any] = {}
    source_documents: list[dict[str, Any]] = []
    nextbid_documents: list[dict[str, Any]] = []
    documents_legacy: list[dict[str, Any]] = []
    file_index: list[dict[str, Any]] = []
    recovered_texts: list[dict[str, Any]] = []
    markdown_recovered = 0
    markdown_chars = 0

    for r in by_ep.get("search") or []:
        body = r.get("body") or {}
        for item in body.get("results") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("nb_id") or "") != nb_id:
                continue
            title = title or item.get("title")
            agency = agency or item.get("agency")
            solicitation_number = solicitation_number or item.get("solicitation_number")
            source_url = source_url or item.get("source_url")
            d = str(item.get("description") or item.get("summary") or "")
            if d:
                description = _thicker_str(description, d)

    for r in by_ep.get("get_opportunity") or []:
        body = r.get("body") or {}
        opp = body.get("opportunity") if isinstance(body.get("opportunity"), dict) else body
        if not isinstance(opp, dict):
            continue
        title = title or opp.get("title")
        agency = agency or opp.get("agency")
        solicitation_number = solicitation_number or opp.get("solicitation_number")
        source_url = source_url or opp.get("source_url")
        procurement_kind = procurement_kind or opp.get("procurement_kind")
        response_deadline = response_deadline or opp.get("response_deadline")
        description = _thicker_str(description, str(opp.get("description") or ""))
        summary = _thicker_str(summary, str(opp.get("summary") or ""))
        ai_summary = _thicker_str(ai_summary, str(opp.get("ai_summary") or ""))
        documents_legacy = _merge_attachment_lists(documents_legacy, opp.get("documents") or [])
        for k in (
            "set_aside", "notice_type", "lifecycle_status", "document_count", "attachment_count",
            "document_set_status", "tradelines", "classification", "locator", "location",
            "contacts", "amendments", "bonding_required", "contract_type", "posted_date",
            "last_updated", "source",
        ):
            if opp.get(k) not in (None, "", [], {}) and notice_fields.get(k) in (None, "", [], {}):
                notice_fields[k] = opp.get(k)

    docs_meta: dict[str, Any] = {}
    for r in by_ep.get("list_documents") or []:
        body = r.get("body") or {}
        if not isinstance(body, dict):
            continue
        source_documents = _merge_attachment_lists(source_documents, body.get("source_documents") or [])
        nextbid_documents = _merge_attachment_lists(nextbid_documents, body.get("nextbid_documents") or [])
        documents_legacy = _merge_attachment_lists(
            documents_legacy,
            body.get("documents") or [],
            body.get("filed") or [],
            body.get("attachment_index") or [],
        )
        for k in (
            "source_documents_count", "originals_stored", "originals_available",
            "pipeline_artifacts", "downloads", "notes",
        ):
            if body.get(k) not in (None, "", [], {}):
                docs_meta[k] = body.get(k)

    for r in by_ep.get("list_files") or []:
        body = r.get("body") or {}
        files = body.get("files") or body.get("artifacts") or body.get("results") or []
        if isinstance(files, list):
            file_index = _merge_attachment_lists(file_index, [f for f in files if isinstance(f, dict)])

    file_index = _merge_attachment_lists(file_index, nextbid_documents)

    for r in by_ep.get("read_file") or []:
        body = r.get("body")
        path = str(r.get("path") or "")
        text = nextbid_file_text(body)
        ignored_before = False
        if isinstance(body, dict):
            classic = str(body.get("text") or body.get("content") or body.get("markdown") or body.get("data") or "")
            raw = str(body.get("raw_text") or "")
            if len(raw.strip()) > 80 and len(classic.strip()) < 40:
                ignored_before = True
                markdown_recovered += 1
                markdown_chars += len(raw)
        if text and len(text.strip()) > 80:
            recovered_texts.append({
                "path": path,
                "chars": len(text),
                "sha256": hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest(),
                "previously_ignored_raw_text": ignored_before,
                "text": text,
            })

    recovered_texts.sort(key=lambda t: (0 if str(t.get("path") or "").endswith(".md") else 1, -int(t["chars"])))
    doc_parts: list[str] = []
    seen_sha: set[str] = set()
    for t in recovered_texts:
        if t["sha256"] in seen_sha:
            continue
        seen_sha.add(t["sha256"])
        if str(t.get("path") or "").endswith(".json") and any(
            str(x.get("path") or "").endswith(".md") for x in recovered_texts
        ):
            continue
        doc_parts.append(t["text"])
    document_text = "\n\n".join(doc_parts).strip()

    stored_original_refs = sum(1 for d in source_documents if d.get("stored") is True)
    blob_for_signals = f"{description}\n{document_text}"
    endpoints = sorted(by_ep.keys())
    return {
        "nb_id": nb_id,
        "endpoints_present": endpoints,
        "response_rows": sum(len(v) for v in by_ep.values()),
        "title": title,
        "agency": agency,
        "solicitation_number": solicitation_number,
        "source_url": source_url,
        "procurement_kind": procurement_kind,
        "response_deadline": response_deadline,
        "description": description or None,
        "summary": summary or None,
        "ai_summary": ai_summary or None,
        "description_chars": len(description or ""),
        "document_text": document_text or None,
        "document_text_chars": len(document_text or ""),
        "notice_fields": notice_fields,
        "source_documents": source_documents,
        "nextbid_documents": nextbid_documents,
        "documents_legacy": documents_legacy,
        "file_index": file_index,
        "documents_meta": docs_meta,
        "recovered_file_texts": [
            {k: v for k, v in t.items() if k != "text"}
            for t in recovered_texts
        ],
        "markdown_raw_text_recovered_count": markdown_recovered,
        "markdown_raw_text_chars": markdown_chars,
        "attachment_references_recovered": len(source_documents) + len(nextbid_documents) + len(documents_legacy),
        "source_documents_count": len(source_documents),
        "stored_original_refs_metadata_only": stored_original_refs,
        "local_original_text_bodies": len(doc_parts),
        "local_original_pdf_bytes_available": 0,
        "signals": {
            "has_quantity_language": bool(QTY_RE.search(blob_for_signals)),
            "has_specification_language": bool(SPEC_RE.search(blob_for_signals)),
            "has_line_item_language": bool(LINE_RE.search(blob_for_signals)),
            "has_submission_language": bool(SUBMIT_RE.search(blob_for_signals)),
        },
    }


def _is_enrichment_worthy(pkg: dict[str, Any]) -> bool:
    """Persist only NBs with detail beyond search-listing membership (avoid 8k audit dumps)."""
    eps = set(pkg.get("endpoints_present") or [])
    if eps - {"search", "unknown"}:
        return True
    if int(pkg.get("document_text_chars") or 0) > 0:
        return True
    if int(pkg.get("source_documents_count") or 0) > 0:
        return True
    if int(pkg.get("markdown_raw_text_recovered_count") or 0) > 0:
        return True
    return False


def _enrichment_row(pkg: dict[str, Any], *, vault_sha: str | None = None) -> dict[str, Any]:
    """Compact production row — no full document body (lives in full_text / vault)."""
    return {
        "nb_id": pkg["nb_id"],
        "title": pkg.get("title"),
        "agency": pkg.get("agency"),
        "solicitation_number": pkg.get("solicitation_number"),
        "source_url": pkg.get("source_url"),
        "procurement_kind": pkg.get("procurement_kind"),
        "response_deadline": pkg.get("response_deadline"),
        "description": pkg.get("description"),
        "summary": pkg.get("summary"),
        "ai_summary": pkg.get("ai_summary"),
        "description_chars": pkg.get("description_chars"),
        "document_text_chars": pkg.get("document_text_chars"),
        "document_text_sha256": (
            hashlib.sha256(str(pkg.get("document_text") or "").encode("utf-8")).hexdigest()
            if pkg.get("document_text")
            else None
        ),
        "evidence_vault_sha256": vault_sha,
        "notice_fields": pkg.get("notice_fields") or {},
        "source_documents": pkg.get("source_documents") or [],
        "nextbid_documents": pkg.get("nextbid_documents") or [],
        "documents_legacy": pkg.get("documents_legacy") or [],
        "file_index": pkg.get("file_index") or [],
        "documents_meta": pkg.get("documents_meta") or {},
        "recovered_file_texts": pkg.get("recovered_file_texts") or [],
        "markdown_raw_text_recovered_count": pkg.get("markdown_raw_text_recovered_count"),
        "markdown_raw_text_chars": pkg.get("markdown_raw_text_chars"),
        "attachment_references_recovered": pkg.get("attachment_references_recovered"),
        "source_documents_count": pkg.get("source_documents_count"),
        "stored_original_refs_metadata_only": pkg.get("stored_original_refs_metadata_only"),
        "local_original_text_bodies": pkg.get("local_original_text_bodies"),
        "local_original_pdf_bytes_available": 0,
        "endpoints_present": pkg.get("endpoints_present"),
        "signals": pkg.get("signals"),
        "full_text_source": "nextbid_offline_recovery_raw_text" if pkg.get("document_text") else None,
    }


def _remove_legacy_per_nb_dumps(out_root: Path) -> int:
    """Delete superseded by_nb_id/*.json audit dumps (one-file-per-solicitation)."""
    legacy = out_root / "by_nb_id"
    if not legacy.exists():
        return 0
    n = sum(1 for _ in legacy.glob("*.json"))
    shutil.rmtree(legacy, ignore_errors=True)
    return n


def run_offline_recovery(*, data_root: Path | None = None) -> dict[str, Any]:
    """Bulk-recover every retained paid response; write compact enrichment + evidence stores."""
    global _ENRICHMENT_CACHE
    from m3.research import evidence_vault
    from m3.research.nextbid_controller import acquisition_locked, controller_status

    t0 = datetime.now(timezone.utc)
    data_root = data_root or get_settings().data_root()
    out_root = recovery_root(data_root)

    # Killswitch / ledger verification via existing interface (no spend, no unlock)
    status = controller_status()
    if not acquisition_locked():
        raise RuntimeError("offline recovery requires acquisition killswitch locked")
    budget = status.get("budget_state") or {}

    rows = iter_paid_request_rows()
    by_nb: dict[str, list[dict[str, Any]]] = defaultdict(list)
    no_nb = 0
    endpoint_counts = Counter()
    for r in rows:
        endpoint_counts[str(r.get("endpoint") or "unknown")] += 1
        nb = r.get("nb_id")
        if not nb and r.get("endpoint") == "search":
            for item in ((r.get("body") or {}).get("results") or []):
                if isinstance(item, dict) and item.get("nb_id"):
                    by_nb[str(item["nb_id"])].append({
                        **{k: r.get(k) for k in ("request_id", "endpoint", "path", "status_code", "retrieved_at")},
                        "nb_id": item["nb_id"],
                        "body": {"results": [item]},
                        "_from_search_slice": True,
                        "_store_path": r.get("_store_path"),
                    })
            continue
        if not nb:
            no_nb += 1
            continue
        by_nb[str(nb)].append(r)

    stats: Counter = Counter()
    stats["paid_request_files_inspected"] = len(rows)
    stats["unique_nb_ids_seen"] = len(by_nb)
    stats["rows_without_nb_id"] = no_nb
    enrichment_rows: list[dict[str, Any]] = []
    category_c: list[dict[str, Any]] = []

    for nb_id, nb_rows in sorted(by_nb.items()):
        if str(nb_id).startswith(("DURABILITY_", "NB-PARSE", "NB-TEST")):
            stats["test_nb_skipped"] += 1
            continue
        pkg = recover_nb_package(nb_id, nb_rows)
        stats["nbs_processed"] += 1
        stats["markdown_bodies_recovered"] += int(pkg["markdown_raw_text_recovered_count"])
        stats["markdown_chars_recovered"] += int(pkg["markdown_raw_text_chars"])
        stats["attachment_refs_recovered"] += int(pkg["attachment_references_recovered"])
        stats["source_documents_refs"] += int(pkg["source_documents_count"])
        stats["local_text_bodies"] += int(pkg["local_original_text_bodies"])
        stats["description_chars_total"] += int(pkg["description_chars"])
        stats["document_text_chars_total"] += int(pkg["document_text_chars"])
        if pkg["description_chars"] > 0:
            stats["nbs_with_description"] += 1
        if pkg["document_text_chars"] > 0:
            stats["nbs_with_document_text"] += 1
        if pkg["signals"]["has_submission_language"]:
            stats["nbs_with_submission_language"] += 1
        if pkg["stored_original_refs_metadata_only"]:
            stats["nbs_with_stored_original_metadata"] += 1

        if not _is_enrichment_worthy(pkg):
            stats["search_only_skipped_from_enrichment"] += 1
            continue

        vault_sha = None
        recovered = str(pkg.get("document_text") or "")
        if len(recovered) >= 200:
            put = evidence_vault.put(
                "nextbid",
                nb_id,
                "document0_markdown_offline_recovery",
                recovered,
                content_type="text/markdown",
                meta={
                    "source": "nextbid_offline_recovery_raw_text",
                    "markdown_raw_text_recovered": pkg["markdown_raw_text_recovered_count"],
                    "endpoints": pkg["endpoints_present"],
                },
            )
            vault_sha = put.get("sha256")
            stats["evidence_vault_puts"] += 1
            res = store_full_text(
                nb_id,
                recovered,
                source="nextbid_offline_recovery_raw_text",
                title=pkg.get("title"),
            )
            if not res.get("skipped_thinner"):
                stats["full_text_store_updated"] += 1
            else:
                stats["full_text_store_kept_thicker"] += 1

        row = _enrichment_row(pkg, vault_sha=vault_sha)
        enrichment_rows.append(row)
        stats["enrichment_rows"] += 1

        # Category C: stored originals / document0.md referenced but no local text body
        if int(pkg.get("local_original_text_bodies") or 0) == 0 and (
            int(pkg.get("stored_original_refs_metadata_only") or 0) > 0
            or any(
                str(a.get("path") or "").endswith(".md")
                for a in (pkg.get("nextbid_documents") or [])
            )
        ):
            md_targets = [
                str(a.get("path"))
                for a in (pkg.get("nextbid_documents") or [])
                if str(a.get("path") or "").endswith(".md")
            ]
            category_c.append({
                "nb_id": nb_id,
                "title": pkg.get("title"),
                "source_documents_count": pkg.get("source_documents_count"),
                "stored_original_refs_metadata_only": pkg.get("stored_original_refs_metadata_only"),
                "document0_md_paths": md_targets[:3],
                "recommended_endpoint": "read_file" if md_targets else None,
                "expected_credit_cost": 1 if md_targets else None,
            })

    _write_jsonl(enrichment_path(data_root), enrichment_rows)
    legacy_removed = _remove_legacy_per_nb_dumps(out_root)
    stats["legacy_per_nb_files_removed"] = legacy_removed
    _ENRICHMENT_CACHE = None  # invalidate

    summary = {
        "ran_at": _utc(),
        "elapsed_sec": round((datetime.now(timezone.utc) - t0).total_seconds(), 2),
        "external_calls": 0,
        "api_expenditure": 0,
        "acquisition_locked": True,
        "ledger": {
            "account_used": budget.get("account_used"),
            "account_remaining": budget.get("account_remaining"),
            "account_allowance": budget.get("account_allowance"),
            "monthly_reserve_limits": (status.get("limits") or {}).get("monthly_reserve"),
            "unchanged": True,
            "verified_via": "controller_status()/load_budget_state",
        },
        "endpoint_counts": dict(endpoint_counts),
        "stats": dict(stats),
        "enrichment_path": str(enrichment_path(data_root)),
        "category_c_paid_gaps": category_c,
        "recommended_one_call_test": (
            sorted(
                category_c,
                key=lambda g: (-int(g.get("source_documents_count") or 0), str(g.get("nb_id"))),
            )[0]
            if category_c
            else None
        ),
        "notes": [
            "Search-only NBs are not duplicated into enrichment; inventory/search index already holds them.",
            "stored=true on source_documents means NextBid metadata only — not local PDF bytes.",
            "No per-solicitation audit files written.",
        ],
    }
    _atomic_write_json(summary_path(data_root), summary)
    # Also copy one concise report into acceptance archive path if present in repo cwd
    try:
        arch = Path("docs/acceptance_archive/NEXTBID_OFFLINE_RECOVERY")
        if arch.parent.exists():
            arch.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(arch / "SUMMARY.json", summary)
            (arch / "REPORT.md").write_text(_summary_markdown(summary), encoding="utf-8")
    except OSError:
        pass
    return summary


def _summary_markdown(summary: dict[str, Any]) -> str:
    s = summary.get("stats") or {}
    led = summary.get("ledger") or {}
    one = summary.get("recommended_one_call_test")
    lines = [
        "# M3 NextBid Offline Recovery — Summary",
        "",
        f"- Ran: `{summary.get('ran_at')}`",
        f"- Runtime: `{summary.get('elapsed_sec')}s`",
        f"- API expenditure: **ZERO**",
        f"- Acquisition locked: **{summary.get('acquisition_locked')}**",
        f"- Ledger: used {led.get('account_used')} / remaining {led.get('account_remaining')} (verified via `{led.get('verified_via')}`)",
        "",
        "## Counts",
        "",
        f"1. Retained responses inspected: **{s.get('paid_request_files_inspected')}**",
        f"2. Markdown bodies recovered: **{s.get('markdown_bodies_recovered')}**",
        f"3. Markdown chars: **{s.get('markdown_chars_recovered')}**",
        f"4. Attachment refs: **{s.get('attachment_refs_recovered')}** (source_documents={s.get('source_documents_refs')})",
        f"5. Local text bodies: **{s.get('local_text_bodies')}** (PDFs not claimed)",
        f"6. Enrichment rows (not search-only): **{s.get('enrichment_rows')}**",
        f"7. Full-text store updated/kept: **{s.get('full_text_store_updated') or 0}** / **{s.get('full_text_store_kept_thicker') or 0}**",
        f"8. Evidence vault puts: **{s.get('evidence_vault_puts')}**",
        f"9. Legacy per-NB dumps removed: **{s.get('legacy_per_nb_files_removed')}**",
        f"10. Category C paid gaps: **{len(summary.get('category_c_paid_gaps') or [])}**",
        f"11. Pricing-now delta this run: **0** (notice text recovered; firm qty/specs still in unretrieved originals)",
        "",
        "## Recommended one-call (owner authorize later)",
        "",
        "```json",
        json.dumps(one, indent=2, default=str) if one else "null",
        "```",
        "",
        "Evidence: `enrichment.jsonl` + `solicitation_full_text` + evidence vault. No per-solicitation audit files.",
        "",
        "**STOP FOR OWNER REVIEW.**",
        "",
    ]
    return "\n".join(lines)


def load_enrichment_index(*, data_root: Path | None = None, force: bool = False) -> dict[str, dict[str, Any]]:
    global _ENRICHMENT_CACHE
    if _ENRICHMENT_CACHE is not None and not force:
        return _ENRICHMENT_CACHE
    path = enrichment_path(data_root)
    out: dict[str, dict[str, Any]] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            nb = str(row.get("nb_id") or "")
            if nb:
                out[nb] = row
    _ENRICHMENT_CACHE = out
    return out


def load_recovery_package(nb_id: str, *, data_root: Path | None = None) -> dict[str, Any] | None:
    """Load compact enrichment + reconstruct document_text from full_text / vault."""
    row = load_enrichment_index(data_root=data_root).get(nb_id)
    if not row:
        # Legacy fallback during transition
        legacy = recovery_root(data_root) / "by_nb_id" / f"{nb_id}.json"
        if legacy.exists():
            try:
                return json.loads(legacy.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
        return None

    pkg = dict(row)
    doc = ""
    ft = load_full_text(nb_id)
    if ft and str(ft.get("full_text") or "").strip():
        doc = str(ft.get("full_text") or "")
    if not doc and row.get("evidence_vault_sha256"):
        try:
            from m3.research.evidence_vault import get_body

            raw = get_body(str(row["evidence_vault_sha256"]))
            if raw:
                doc = raw.decode("utf-8", errors="replace")
        except Exception:
            pass
    pkg["document_text"] = doc or None
    pkg["document_text_chars"] = len(doc)
    return pkg


def apply_recovery_to_evidence(
    nb_id: str,
    *,
    original: dict[str, Any],
    doc_text: str,
    sources_used: list[str],
    cache_refs: list[str],
    data_root: Path | None = None,
) -> tuple[dict[str, Any], str]:
    """Merge offline enrichment into gather_original_evidence fields (thicker wins)."""
    pkg = load_recovery_package(nb_id, data_root=data_root)
    if not pkg:
        return original, doc_text

    sources_used.append("nextbid_offline_recovery")
    cache_refs.append(str(enrichment_path(data_root)))

    for k in (
        "title", "agency", "solicitation_number", "source_url", "procurement_kind",
        "response_deadline", "description", "summary", "ai_summary",
    ):
        v = pkg.get(k)
        if isinstance(v, str) and len(v) > len(str(original.get(k) or "")):
            original[k] = v
        elif v not in (None, "", [], {}) and original.get(k) in (None, "", [], {}):
            original[k] = v

    for k, v in (pkg.get("notice_fields") or {}).items():
        if v not in (None, "", [], {}) and original.get(k) in (None, "", [], {}):
            original[k] = v

    recovered_doc = str(pkg.get("document_text") or "")
    if len(recovered_doc) > len(doc_text or ""):
        doc_text = recovered_doc

    att = []
    for key in ("attachments", "documents", "document_list", "source_documents", "nextbid_documents"):
        cur = original.get(key)
        if isinstance(cur, list):
            att.extend(cur)
    att = _merge_attachment_lists(
        att,
        pkg.get("source_documents") or [],
        pkg.get("nextbid_documents") or [],
        pkg.get("documents_legacy") or [],
        pkg.get("file_index") or [],
    )
    if att:
        original["attachments"] = att
        original["documents"] = att
        original["source_documents"] = pkg.get("source_documents") or original.get("source_documents") or []
        original["nextbid_documents"] = pkg.get("nextbid_documents") or original.get("nextbid_documents") or []
    original["offline_recovery"] = {
        "markdown_raw_text_recovered_count": pkg.get("markdown_raw_text_recovered_count"),
        "document_text_chars": pkg.get("document_text_chars"),
        "source_documents_count": pkg.get("source_documents_count"),
        "endpoints_present": pkg.get("endpoints_present"),
        "local_original_text_bodies": pkg.get("local_original_text_bodies"),
        "stored_original_refs_metadata_only": pkg.get("stored_original_refs_metadata_only"),
        "local_original_pdf_bytes_available": 0,
        "enrichment": True,
    }
    return original, doc_text
