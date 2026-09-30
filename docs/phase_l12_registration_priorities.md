# Phase L.12 — Registration Priorities

## `REGISTRATION_HISTORY_OPPORTUNITY`

Created when exact award evidence appears reachable after free/vendor registration.
**Never** creates the account.

Fields: platform, buyer, registration URL, cost, account type, documents unlocked (unknown until
registered), award/bid-tab unlock flags, setup effort, blocked opportunity count, potential value,
buyers on same platform.

## `HistoryAccessRegistrationPriority`

Score factors: blocked opportunities, possible Gov D→A/B upgrades, projected profit blocked,
recurring buyer value, buyers on same platform (network leverage), cost, setup complexity.

Platform-wide leverage (one BidNet vendor account → many buyers) ranks above one-buyer portals.

Owner roadmap artifact: `artifacts/phase_l/l12_registration_priorities.json`.
