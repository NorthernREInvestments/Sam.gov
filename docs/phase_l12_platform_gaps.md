# Phase L.12 — Platform Gaps

Weak/absent feeds remain (OpenGov, Bonfire, IonWave, PlanetBids, DemandStar, Jaggaer,
Public Purchase, DLA/DIBBS, cooperatives). L.12 prioritizes **exact-history access** over
another discovery expansion pass.

Root-cause classes: no public enumeration, auth required, adapter broken, parser stale,
anti-bot, buyer registry incomplete, no live records, unsupported implementation.

Only fix actual implementation defects. BidNet: anti-bot / auth-walled awards → classify and
buyer-pivot; do not treat as terminal history failure.

Platform memory: `data/phase_l12_platform_history_memory.json`.
