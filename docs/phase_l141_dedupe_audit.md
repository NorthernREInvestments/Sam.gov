# Phase L.14.1 — Dedupe Audit

**Build:** `20260928-m3-phase-l141-l14-repair-structured-source-audit`

Dedupe key: `notice_id | external_id | solicitation_number | detail_url | title[:160]`

**Conclusion:** the 22-row figure was **not** caused by over-dedupe. Live productive adapters summed to 22 distinct solicitations.

Risk: title-only keys could over-dedupe — prefer solicitation ID (already first).
