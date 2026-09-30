# Phase L.13 — Legacy Cleanup

Reconciled:

- Public artifact recovery runs **before** registration classification
- Duplicate BidNet “stop at anti-bot” behavior removed from live path
- URL patterns only from observed public links (no ID brute-force)
- Failed commercial hunt ≠ zero BidNet inventory
- Canonical live runner → `l13_rescue.run_phase_l13_public_artifact`
- L.12 auth-walled recovery retained as prior layer

Obsolete rules added: `LEGACY_ID_BRUTE_FORCE_ARTIFACTS`, `LEGACY_CAPTCHA_BYPASS`, `LEGACY_SKIP_PUBLIC_ARTIFACT_BEFORE_REGISTRATION`.
