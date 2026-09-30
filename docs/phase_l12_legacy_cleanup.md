# Phase L.12 — Legacy Cleanup

Reconciled:

- Duplicate auth classifiers → `auth_access` sole taxonomy; L.11 `classify_auth_wall` is a map
- Duplicate buyer-history branches → `auth_history_recovery` + `buyer_history_paths` canonical
- Platform block ≠ history unavailable
- BidNet no longer terminates exact-history recovery
- No auto account creation / CAPTCHA / login
- Lot totals stay `LOT_VALUE` (Gov D), not promoted to unit Gov C
- Canonical live runner → `l12_rescue.run_phase_l12_auth_history`
- Resilient hunt (L.11) retained unchanged

Obsolete rule IDs extended for auth-collapse and BidNet-terminate patterns.
Historical L.10/L.11 rescues retained as compatibility modules, not live alternate funnels.
