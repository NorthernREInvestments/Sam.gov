# Phase L.12 — Regression

Covered by `tests/test_phase_l12_auth_history.py` (+ L.11 suite retained):

- BidNet blocked → buyer pivot / `PLATFORM_HISTORY_BLOCKED`
- Access mode taxonomy (free reg, vendor, buyer, captcha, private, no public)
- Buyer path discovery + memory; platform memory
- Exact solicitation variants; bid-tab line match; lot protection
- Gov A/B/C upgrade paths; registration priority / no auto-register
- Prior awardee roles; competition extraction
- Economic recompute shape; no fixed Stage-3 cap; resilient hunt constants
- No outreach / no account flags

Run: `pytest tests/test_phase_l12_auth_history.py tests/test_phase_l11_exact_history.py -q`
