# Phase L.13 — Regression

`tests/test_phase_l13_public_artifacts.py` covers:

- BidNet blocked → public artifact recovery
- exact solicitation queries / print / award / attachment classification
- exact linking + wrong-solicitation rejection
- bid-tab line match + Gov A/B from artifact fields
- observed pattern instantiation (no invention)
- cache behavior
- canonical workflow integration
- no CAPTCHA bypass / no ID brute-force / no outreach
- resilient hunt constants + no Stage-3 cap

Run: `pytest tests/test_phase_l13_public_artifacts.py tests/test_phase_l12_auth_history.py tests/test_phase_l11_exact_history.py -q`
