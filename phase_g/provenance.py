"""Phase G provenance classes — LIVE_SOURCE is the only live-proof class."""

from __future__ import annotations

PROVENANCE_LIVE_SOURCE = "LIVE_SOURCE"
PROVENANCE_FROZEN_REAL_SOURCE = "FROZEN_REAL_SOURCE"
PROVENANCE_REALISTIC_FIXTURE = "REALISTIC_FIXTURE"
PROVENANCE_SYNTHETIC_EDGE = "SYNTHETIC_EDGE"

PROVENANCE_CLASSES = frozenset(
    {
        PROVENANCE_LIVE_SOURCE,
        PROVENANCE_FROZEN_REAL_SOURCE,
        PROVENANCE_REALISTIC_FIXTURE,
        PROVENANCE_SYNTHETIC_EDGE,
    }
)

# Map Phase F harness labels → Phase G classes (never call frozen "live")
_HARNESS_TO_PHASE_G = {
    "REAL_SOURCE_FIXTURE": PROVENANCE_FROZEN_REAL_SOURCE,
    "REALISTIC_FROZEN_FIXTURE": PROVENANCE_REALISTIC_FIXTURE,
    "SYNTHETIC_EDGE_CASE": PROVENANCE_SYNTHETIC_EDGE,
    "SYNTHETIC_VALIDATION_FIXTURE": PROVENANCE_SYNTHETIC_EDGE,
}


def normalize_provenance(raw: str | None) -> str | None:
    if not raw:
        return None
    s = str(raw).strip().upper()
    if s in PROVENANCE_CLASSES:
        return s
    return _HARNESS_TO_PHASE_G.get(s) or _HARNESS_TO_PHASE_G.get(str(raw).strip())


def stamp_live_source(row: dict, *, retrieved_at: str, source_system: str) -> dict:
    """Stamp Phase G LIVE_SOURCE provenance onto a deal row (in place + return)."""
    out = row
    out["phase_g_provenance"] = PROVENANCE_LIVE_SOURCE
    out["source_provenance"] = PROVENANCE_LIVE_SOURCE
    out["live_retrieval_timestamp"] = retrieved_at
    out["live_source_system"] = source_system
    # Never allow fixture labels to masquerade as live
    if str(out.get("source_type") or "").upper().endswith("FIXTURE"):
        out["source_type_note"] = "fixture_label_superseded_by_LIVE_SOURCE_retrieval"
    return out


def counts_as_live_proof(provenance: str | None) -> bool:
    return normalize_provenance(provenance) == PROVENANCE_LIVE_SOURCE
