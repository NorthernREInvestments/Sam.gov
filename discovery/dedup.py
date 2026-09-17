"""Cross-source deterministic deduplication."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from discovery.schema import CanonicalOpportunity


def _norm(s: str | None) -> str:
    if not s:
        return ""
    t = re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()
    return re.sub(r"\s+", " ", t)


def title_fingerprint(title: str | None) -> str:
    n = _norm(title)
    return hashlib.sha256(n.encode("utf-8")).hexdigest()[:32] if n else ""


def description_fingerprint(desc: str | None) -> str:
    n = _norm((desc or "")[:2000])
    return hashlib.sha256(n.encode("utf-8")).hexdigest()[:32] if n else ""


def strong_canonical_key(opp: CanonicalOpportunity | dict[str, Any]) -> str | None:
    """
    Strong identity only — never merge on title similarity alone.
    Uses jurisdiction + agency + solicitation number, or source+external_id.
    """
    if isinstance(opp, CanonicalOpportunity):
        d = opp.to_dict()
    else:
        d = opp

    sol = _norm(d.get("solicitation_number"))
    jur = _norm(d.get("jurisdiction") or d.get("state_code"))
    agency = _norm(d.get("agency"))
    if sol and (jur or agency):
        return f"strong:{jur}|{agency}|{sol}"

    ext = str(d.get("external_id") or "").strip()
    src = str(d.get("source_id") or "").strip()
    if ext and src:
        return f"src:{src}|{ext}"

    url = str(d.get("detail_url") or d.get("source_url") or "").strip().lower()
    if url:
        # strip trailing slash / query noise lightly
        url = url.split("?")[0].rstrip("/")
        return f"url:{hashlib.sha256(url.encode()).hexdigest()[:40]}"

    return None


def prefer_official_source(
    existing_tier: int,
    existing_source_id: str | None,
    candidate_tier: int,
    candidate_source_id: str | None,
) -> bool:
    """Prefer lower trust tier number (TIER 1 official) as authoritative."""
    if candidate_tier < existing_tier:
        return True
    if candidate_tier == existing_tier:
        # Stable preference — keep existing unless candidate explicitly marked official
        return False
    return False


def fuzzy_match_evidence(
    a: dict[str, Any],
    b: dict[str, Any],
) -> dict[str, Any]:
    """
    Secondary evidence only — NEVER sufficient alone to merge.
    """
    evidence = []
    same_title = title_fingerprint(a.get("title")) == title_fingerprint(b.get("title")) and bool(
        title_fingerprint(a.get("title"))
    )
    if same_title:
        evidence.append("normalized_title")
    if _norm(a.get("agency")) and _norm(a.get("agency")) == _norm(b.get("agency")):
        evidence.append("agency")
    if a.get("response_deadline") and a.get("response_deadline") == b.get("response_deadline"):
        evidence.append("deadline")
    return {
        "evidence": evidence,
        "sufficient_to_merge": False,  # hard rule
        "note": "Fuzzy evidence never merges without strong identifier",
    }
