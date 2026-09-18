"""INTERNAL IMPLEMENTATION MAP — Federal + DLA one-shot build (Phase 0).

Existing reusable:
- sam_client.normalize_opportunity / SAM_SEARCH_URL (API v2)
- discovery.dla_fallback (narrow SPE/org SAM fallback when DIBBS blocked)
- live_dibbs / live_piee_public fetchers + FEDERAL_NON_SAM_LIVE seeds
- m3_pipeline_handoff (durable but slow at 13k+)
- national_procurement_coverage_map / BidNet national scale
- classify_discovery_opportunity cheap screen

Broken Federal paths:
- assert_no_broad_sam_discovery blocks SAM from live discovery pool
- SAM used only as scarce last-resort / DLA fallback (limit 50, offset 0)
- set-aside filter in fetch_naics_from_sam drops non-SBA notices
- no Federal notice-type / org hierarchy / count reconciliation

Missing Federal paths:
- First-class authoritative SAM active-opportunities enumeration source
- Exhaustive pagination + durable Federal checkpoint
- Notice-type intelligence (bid-ready vs sources sought etc.)
- Agency breakdown reporting

Broken DLA paths:
- DIBBS direct: BOT/403
- PIEE: often auth
- DLA SAM fallback: capped, SPE-prefix heavy, not reconciled

Missing DLA:
- DLA_PROCUREMENT_SOURCE_MAP, DLA_SOURCE_RECONCILIATION, DLA_COVERAGE_MATRIX
- Bounded DLA empirical sample
- Structured NSN/P/N/qty extraction pipeline
- FEDERAL_DISCOVERY_GAP_QUEUE

Artificial limits:
- SAM_API_CALL_LIMIT default 10/day
- dla_fallback max_results=50, offset=0 only
- Checkpoint every 25 with full survivor rewrite → handoff bottleneck

Dedupe risks:
- noticeId vs solicitationNumber vs SPE identity across SAM/DIBBS
- Amendments must update not inflate

Handoff bottlenecks:
- save_handoff_checkpoint rewrites full survivors JSON to AppSetting every 25 rows
- store.save() every checkpoint
- reconcile calls store.all() (O(n) deepcopy)

This build owns: Federal SAM ingest, DLA map/reconcile/sample, product extract,
handoff throughput, coverage semantics, UI metrics, targeted tests, one bootstrap.
"""

from __future__ import annotations

# Coverage semantics (Phase 2)
FEDERAL_SAM_PUBLIC_COVERAGE_COMPLETE_TO_CHECKPOINT = "FEDERAL_SAM_PUBLIC_COVERAGE_COMPLETE_TO_CHECKPOINT"
FEDERAL_SAM_PUBLIC_COVERAGE_PARTIAL = "FEDERAL_SAM_PUBLIC_COVERAGE_PARTIAL"
FEDERAL_SOURCE_ACCESS_CONSTRAINED = "FEDERAL_SOURCE_ACCESS_CONSTRAINED"
FEDERAL_KNOWN_GAPS = "FEDERAL_KNOWN_GAPS"

DLA_PUBLIC_COVERAGE_RECONCILED = "DLA_PUBLIC_COVERAGE_RECONCILED"
DLA_PUBLIC_COVERAGE_PARTIAL = "DLA_PUBLIC_COVERAGE_PARTIAL"
DLA_COVERAGE_ACCESS_CONSTRAINED = "DLA_COVERAGE_ACCESS_CONSTRAINED"
DLA_KNOWN_GAPS = "DLA_KNOWN_GAPS"

DIBBS_DIRECT_PUBLIC_MACHINE_ACCESSIBLE = "DIRECT_PUBLIC_MACHINE_ACCESSIBLE"
DIBBS_DIRECT_PARTIAL = "DIRECT_PARTIAL"
DIBBS_SAM_RECONCILED = "SAM_RECONCILED"
DIBBS_ALTERNATE_OFFICIAL_PARTIAL = "ALTERNATE_OFFICIAL_PARTIAL"
DIBBS_PUBLIC_BROWSER_ONLY = "PUBLIC_BROWSER_ONLY"
DIBBS_REGISTRATION_REQUIRED = "REGISTRATION_REQUIRED"
DIBBS_AUTH_REQUIRED = "AUTH_REQUIRED"
DIBBS_BOT_BLOCKED_AUTOMATION = "BOT_BLOCKED_AUTOMATION"
DIBBS_NO_PUBLIC_MACHINE_INTERFACE_FOUND = "NO_PUBLIC_MACHINE_INTERFACE_FOUND"
DIBBS_UNKNOWN = "UNKNOWN"

# Notice semantic classes
NOTICE_BID_OR_QUOTE_READY = "BID_OR_QUOTE_READY"
NOTICE_UPCOMING_PROCUREMENT = "UPCOMING_PROCUREMENT"
NOTICE_MARKET_RESEARCH = "MARKET_RESEARCH"
NOTICE_SOLE_SOURCE_SIGNAL = "SOLE_SOURCE_SIGNAL"
NOTICE_AWARD_OR_HISTORY = "AWARD_OR_HISTORY"
NOTICE_INFORMATIONAL = "INFORMATIONAL"
NOTICE_UNKNOWN = "UNKNOWN"

# DLA relationship
REL_SAM_ONLY = "SAM_ONLY"
REL_DIBBS_ONLY = "DIBBS_ONLY"
REL_SAM_AND_DIBBS = "SAM_AND_DIBBS"
REL_OTHER_OFFICIAL_DLA_ONLY = "OTHER_OFFICIAL_DLA_ONLY"
REL_MULTI_SOURCE = "MULTI_SOURCE"
REL_UNKNOWN = "UNKNOWN_RELATIONSHIP"

# Gap types
GAP_SAM_PAGINATION_INCOMPLETE = "SAM_PAGINATION_INCOMPLETE"
GAP_SAM_COUNT_MISMATCH = "SAM_COUNT_MISMATCH"
GAP_DLA_DIBBS_AUTOMATION_BLOCKED = "DLA_DIBBS_AUTOMATION_BLOCKED"
GAP_DLA_FAMILY_UNDERREPRESENTED = "DLA_FAMILY_UNDERREPRESENTED"
GAP_DLA_SAM_RECONCILIATION_GAP = "DLA_SAM_RECONCILIATION_GAP"
GAP_PIEE_AUTH_REQUIRED = "PIEE_AUTH_REQUIRED"
GAP_CONTROLLED_DATA_REQUIRED = "CONTROLLED_DATA_REQUIRED"
GAP_FEDERAL_ORGANIZATION_CLASSIFICATION_GAP = "FEDERAL_ORGANIZATION_CLASSIFICATION_GAP"
GAP_FEDERAL_HANDOFF_BACKLOG = "FEDERAL_HANDOFF_BACKLOG"
GAP_FEDERAL_SOURCE_STALE = "FEDERAL_SOURCE_STALE"

PURPOSE_FEDERAL_OPPORTUNITIES_ENUMERATION = "FEDERAL_OPPORTUNITIES_ENUMERATION"
PURPOSE_DLA_RECONCILIATION = "DLA_RECONCILIATION"

SAM_SEARCH_URL = "https://api.sam.gov/opportunities/v2/search"
SAM_CHECKPOINT_KEY = "m3_federal_sam_checkpoint_v1"
FEDERAL_COVERAGE_KEY = "m3_federal_dla_coverage_v1"
