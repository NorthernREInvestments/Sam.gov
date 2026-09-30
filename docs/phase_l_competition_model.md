# Phase L — Competition Model

## Principle

Offer counts are only meaningful when paired with competition/accessibility type.

| Situation | Signal |
|-----------|--------|
| 2 offers on open-market SAP | Attractive |
| 2 offers among 4 BPA holders | `PRE_FILTERED_COMPETITION` — not comparable |

## `effective_competition_signal`

- `VERY_LOW_OPEN_COMPETITION` (≤1 offer, open/accessible history)
- `LOW_OPEN_COMPETITION` (2–3)
- `MODERATE_OPEN_COMPETITION` (4–5)
- `HIGH_OPEN_COMPETITION` (6+)
- `PRE_FILTERED_COMPETITION` (vehicle / sole / ASL history)
- `UNKNOWN`

## Ranking bands (open-comparable only)

| Prior offers | Attractiveness |
|-------------:|----------------|
| 1 | Extremely attractive |
| 2–3 | Very attractive |
| 4–5 | Attractive |
| 6–10 | Normal |
| 11–20 | Competitive |
| 20+ | Heavily competitive |

`PRE_FILTERED_COMPETITION` gets competition rank score **0** — never promoted as a “low competition” win.

## Reporting

Live Phase L report separates:

- open-market / SB direct offer distributions
- pre-filtered / vehicle distributions

Do not mix them.
