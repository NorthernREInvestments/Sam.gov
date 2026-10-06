"""Derive unique opportunity IDs blocked/unlocked by a registration portal.

Never reuse a global capped count. Each unlock computes its own unique ID set.
"""

from __future__ import annotations

import re
from typing import Any


def _norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "").lower()).strip()


def _state_token_from_portal(portal_id: str, buyer_name: str | None = None) -> str | None:
    """Extract state/jurisdiction token from portal id / name when present."""
    blob = f"{portal_id} {buyer_name or ''}".lower()
    # network_bidnet_missouri → missouri
    m = re.search(r"bidnet[_-]([a-z]{4,})", blob)
    if m:
        return m.group(1)
    m = re.search(r":state:([a-z]{2,})", blob)
    if m:
        return m.group(1)
    # "State of Montana" / plain names
    for st in (
        "missouri",
        "montana",
        "nebraska",
        "texas",
        "ohio",
        "maine",
        "maryland",
        "massachusetts",
        "mississippi",
        "colorado",
        "arizona",
        "alabama",
        "alaska",
        "arkansas",
        "california",
        "florida",
        "georgia",
        "illinois",
        "indiana",
        "iowa",
        "kansas",
        "kentucky",
        "louisiana",
        "michigan",
        "minnesota",
        "nevada",
        "newmexico",
        "newyork",
        "northcarolina",
        "northdakota",
        "oklahoma",
        "oregon",
        "pennsylvania",
        "southcarolina",
        "southdakota",
        "tennessee",
        "utah",
        "virginia",
        "washington",
        "westvirginia",
        "wisconsin",
        "wyoming",
    ):
        if st in blob.replace(" ", "").replace("_", "").replace("-", ""):
            return st
    return None


def _source_ids_for_rec(rec: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for e in rec.get("source_provenance") or []:
        if isinstance(e, dict):
            for k in ("source_id", "portal", "feed", "source"):
                v = e.get(k)
                if v:
                    out.add(str(v))
    rr = rec.get("row_ref")
    if isinstance(rr, dict) and rr.get("source_id"):
        out.add(str(rr["source_id"]))
    if rec.get("platform"):
        out.add(str(rec["platform"]))
    return out


def _rec_text_blob(rec: dict[str, Any]) -> str:
    rr = rec.get("row_ref") if isinstance(rec.get("row_ref"), dict) else {}
    return _norm(
        " ".join(
            str(x or "")
            for x in (
                rec.get("buyer"),
                rec.get("jurisdiction"),
                rec.get("title"),
                rec.get("platform"),
                rr.get("agency"),
                rr.get("source_id"),
            )
        )
    )


def opportunity_ids_for_portal(
    *,
    portal_id: str,
    portal_row: dict[str, Any],
    store: dict[str, dict[str, Any]],
    available_only: bool = True,
    dead_states: set[str] | None = None,
) -> list[str]:
    """Return sorted unique canonical opportunity IDs linked to this unlock/portal."""
    dead = dead_states or {"FAST_REJECT", "REJECTED", "EXPIRED", "CANCELED", "CANCELLED", "HARD_REJECT"}
    portal_id = str(portal_id or "").strip()
    buyer_name = str(portal_row.get("buyer_name") or portal_row.get("portal") or "")
    state_tok = _state_token_from_portal(portal_id, buyer_name)
    buyer_n = _norm(buyer_name)

    # Prefer explicitly stored unique IDs when present
    stored = portal_row.get("opportunity_ids") or portal_row.get("linked_opportunity_ids") or []
    if isinstance(stored, list) and stored:
        ids = []
        seen: set[str] = set()
        for raw in stored:
            cid = str(raw or "").strip()
            if not cid or cid in seen:
                continue
            if available_only:
                rec = store.get(cid)
                if rec and str(rec.get("current_funnel_state") or "") in dead:
                    continue
                if rec and str(rec.get("freshness") or "").upper() in {"EXPIRED", "CANCELED", "CANCELLED"}:
                    continue
            seen.add(cid)
            ids.append(cid)
        if ids:
            return sorted(ids)

    matched: set[str] = set()
    for cid, rec in store.items():
        if not isinstance(rec, dict):
            continue
        if available_only:
            st = str(rec.get("current_funnel_state") or "")
            if st in dead:
                continue
            if str(rec.get("freshness") or "").upper() in {"EXPIRED", "CANCELED", "CANCELLED"}:
                continue
            if str(rec.get("source_status") or "").upper() in {"CANCELED", "CANCELLED", "CLOSED_EXPIRED"}:
                continue

        sources = _source_ids_for_rec(rec)
        if portal_id and portal_id in sources:
            matched.add(cid)
            continue
        # Fuzzy: portal id substring in source ids
        if portal_id and any(portal_id in s or s in portal_id for s in sources if s and s != "accessible_latest"):
            matched.add(cid)
            continue
        blob = _rec_text_blob(rec)
        if state_tok and state_tok in blob.replace(" ", "").replace("_", "").replace("-", ""):
            # Prefer registration-needed / free-reg opportunities for unlock cards
            access = str(rec.get("access_status") or "").upper()
            src_status = str(rec.get("source_status") or "").upper()
            reg = str(rec.get("registration_status") or "").upper()
            if (
                "FREE_REGISTRATION" in src_status
                or "REGISTRATION" in src_status
                or access in {"NO", "REGISTRATION_REQUIRED", "BLOCKED"}
                or "REGISTER" in reg
                or state_tok in _norm(rec.get("buyer") or "")
                or state_tok in _norm(rec.get("jurisdiction") or "")
            ):
                matched.add(cid)
                continue
        if buyer_n and len(buyer_n) >= 4 and buyer_n in _norm(rec.get("buyer") or ""):
            matched.add(cid)

    return sorted(matched)


def summarize_unlock_links(
    *,
    portal_id: str,
    portal_row: dict[str, Any],
    store: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    ids = opportunity_ids_for_portal(portal_id=portal_id, portal_row=portal_row, store=store)
    buyers: set[str] = set()
    for cid in ids:
        rec = store.get(cid) or {}
        b = rec.get("buyer")
        if b:
            buyers.add(str(b))
    if portal_row.get("buyer_name"):
        buyers.add(str(portal_row["buyer_name"]))
    return {
        "portal_id": portal_id,
        "opportunity_ids": ids,
        "opportunity_count": len(ids),
        "buyer_ids": sorted(buyers),
        "buyer_count": len(buyers),
    }
