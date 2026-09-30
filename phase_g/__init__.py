"""Phase G package — supervised live deal validation (read-only)."""

from phase_g.provenance import (
    PROVENANCE_FROZEN_REAL_SOURCE,
    PROVENANCE_LIVE_SOURCE,
    PROVENANCE_REALISTIC_FIXTURE,
    PROVENANCE_SYNTHETIC_EDGE,
    counts_as_live_proof,
    normalize_provenance,
    stamp_live_source,
)

__all__ = [
    "PROVENANCE_LIVE_SOURCE",
    "PROVENANCE_FROZEN_REAL_SOURCE",
    "PROVENANCE_REALISTIC_FIXTURE",
    "PROVENANCE_SYNTHETIC_EDGE",
    "counts_as_live_proof",
    "normalize_provenance",
    "stamp_live_source",
]
