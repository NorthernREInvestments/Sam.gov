"""Pre-spend document fetch plan — credits are scarce; inventory first.

Rules:
- Look at get_opportunity counts + stored list_documents BEFORE any new metered call.
- Prefer free source_url; pay get_document_download only for real sha256 attachments.
- Never spend read_file credits on NextBid document0 .json/.md sidecars when a real
  attachment already exists (or opportunity already says the package is that one file).
- Emit an explicit plan: what we already have, what is free, what would cost credits.
"""
from __future__ import annotations

from typing import Any


def _name(e: dict[str, Any]) -> str:
    return str(e.get("name") or e.get("filename") or e.get("path") or "document")


def _sha(e: dict[str, Any]) -> str:
    return str(e.get("sha256") or e.get("hash") or "").strip()


def _url(e: dict[str, Any]) -> str:
    return str(e.get("source_url") or e.get("url") or "").strip()


def _path(e: dict[str, Any]) -> str:
    return str(e.get("path") or e.get("_read_file_path") or "").strip()


def is_document0_sidecar(e: dict[str, Any]) -> bool:
    """NextBid pipeline artifact — not a buyer-facing solicitation PDF."""
    kind = str(e.get("kind") or "").lower()
    path = _path(e).lower()
    name = _name(e).lower()
    list_key = str(e.get("list_key") or e.get("_list_key") or "").lower()
    if kind == "document0":
        return True
    if "document0" in path or "document0" in name:
        return True
    if list_key == "nextbid_documents" and (path.endswith(".json") or path.endswith(".md")):
        return True
    return False


def is_real_attachment(e: dict[str, Any]) -> bool:
    if is_document0_sidecar(e):
        return False
    sha = _sha(e)
    if sha:
        return True
    name = _name(e).lower()
    if name.endswith((".pdf", ".docx", ".doc", ".xlsx", ".xls", ".zip", ".csv", ".txt")):
        return True
    return False


def opportunity_expected_doc_count(opportunity: dict[str, Any] | None) -> int | None:
    if not isinstance(opportunity, dict):
        return None
    for key in ("document_count", "attachment_count"):
        v = opportunity.get(key)
        if v is None or v == "":
            continue
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n >= 0:
            return n
    docs = opportunity.get("documents")
    if isinstance(docs, list):
        return len(docs)
    return None


def classify_inventory_entries(entries: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    real: list[dict[str, Any]] = []
    free_url: list[dict[str, Any]] = []
    sidecars: list[dict[str, Any]] = []
    other: list[dict[str, Any]] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        if is_document0_sidecar(e):
            sidecars.append(e)
            continue
        url = _url(e)
        if url.startswith("https://") and "login" not in url.lower():
            free_url.append(e)
        if is_real_attachment(e):
            real.append(e)
        else:
            other.append(e)
    return {
        "real_attachments": real,
        "free_url_candidates": free_url,
        "document0_sidecars": sidecars,
        "other": other,
    }


def build_document_fetch_plan(
    *,
    nb_id: str,
    entries: list[dict[str, Any]],
    opportunity: dict[str, Any] | None = None,
    already_saved_names: set[str] | None = None,
    max_paid_credits: int | None = None,
) -> dict[str, Any]:
    """Return what to attempt and what NOT to spend credits on."""
    already = already_saved_names or set()
    classified = classify_inventory_entries(entries)
    expected = opportunity_expected_doc_count(opportunity)

    real = classified["real_attachments"]
    # Prefer unique by sha/name
    seen: set[str] = set()
    unique_real: list[dict[str, Any]] = []
    for e in real:
        key = _sha(e) or _name(e)
        if key in seen:
            continue
        seen.add(key)
        unique_real.append(e)

    need_download: list[dict[str, Any]] = []
    skip_already: list[dict[str, Any]] = []
    for e in unique_real:
        nm = _name(e)
        if nm in already or any(nm in s or s.endswith(nm) for s in already):
            skip_already.append(e)
        else:
            need_download.append(e)

    # Cap paid downloads by opportunity count when known (gold credits).
    paid_budget = max_paid_credits
    if expected is not None and paid_budget is None:
        paid_budget = max(0, expected - len(skip_already))
    elif expected is not None and paid_budget is not None:
        paid_budget = min(paid_budget, max(0, expected - len(skip_already)))

    paid_targets = need_download
    if paid_budget is not None:
        paid_targets = need_download[: int(paid_budget)]

    # document0: NEVER spend credits — hard rule (empty/useless burns).
    skip_sidecars = list(classified["document0_sidecars"])
    allow_sidecar_read = False

    return {
        "nb_id": nb_id,
        "expected_doc_count": expected,
        "inventory_entry_count": len(entries),
        "real_attachment_count": len(unique_real),
        "document0_sidecar_count": len(skip_sidecars),
        "already_saved": sorted(already),
        "skip_already_have": [_name(e) for e in skip_already],
        "free_url_first": [_name(e) for e in classified["free_url_candidates"]],
        "paid_download_targets": [
            {
                "name": _name(e),
                "sha256": _sha(e)[:16] if _sha(e) else None,
                "has_free_url": bool(_url(e).startswith("https://")),
            }
            for e in paid_targets
        ],
        "do_not_spend_credits_on": [
            {
                "name": _name(e),
                "path": _path(e),
                "reason": "document0_sidecar_not_buyer_pdf",
            }
            for e in skip_sidecars
        ]
        if not allow_sidecar_read
        else [],
        "allow_document0_read_file": allow_sidecar_read,
        "max_paid_credits_planned": paid_budget,
        "targets_for_download": paid_targets,
        "classified": {
            "real": [_name(e) for e in unique_real],
            "sidecars": [_path(e) or _name(e) for e in skip_sidecars],
        },
    }


def downloadable_targets_from_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Entries safe to attempt (free URL then paid sha). Excludes document0 by default."""
    return list(plan.get("targets_for_download") or [])
