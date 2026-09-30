# Phase L.14 — Buyer-Type Yield

**Build:** `20260928-m3-phase-l14-nonbidnet-source-expansion`

## Priority buyer types

state agencies, cities, counties, schools, universities, utilities, transit, airports, public authorities, cooperatives.

Do **not** overfocus on defense buyers. Keep DLA/DIBBS for specialty + `MILSPEC_OPEN_CHANNEL`.

## Measured (L.14)

| Buyer type | Signal |
|------------|--------|
| Cooperative | Strongest new live discovery (Sourcewell, HGACBuy) |
| City | Boston bid-listings productive; many OpenGov CDN cities blocked |
| Airport | LAWA path partial |
| University / school / utility | Seeds present; live yield weak this crawl |
| Defense/DLA | DIBBS empty this crawl; SAM federal still dominant inventory |

## Commercial buyer priority

Repeated procurers of: IT, electronics, vehicles/fleet, equipment, tools, MRO, facility, lab/test, office/furniture, safety. No food/perishables.

## Registry

`ProcurementBuyerRegistry` tracks: legal name, state, type, platform, procurement URL, awards/history URL, board system, open-data, auth state, commercial/history yield, last successful crawl.
