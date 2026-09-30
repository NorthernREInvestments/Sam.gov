# Phase L.12 — Buyer History Paths

## Record: `BuyerHistoryPath`

Persisted at `data/phase_l12_buyer_history_paths.json`.

Fields: buyer, procurement portal, award-results URL, bid-tab path, board/agenda system,
finance/open-data system, PO/check-register location, auth requirements, document naming
patterns, last successful recovery, supported evidence types, search strategy, agenda system type.

## Discovery

For unknown buyers, seed `.gov` bases and path keywords (award, bid tab, agenda, check register,
opendata, contracts, …). Detect common agenda platforms (Legistar, Granicus, BoardDocs, CivicClerk, …)
when URLs/HTML match.

## Reuse

Future opportunities for the same buyer consult memory **first**, then broaden discovery.
