# Phase K — Economics Recalculation (Live Qty/UOM)

**Method:** Phase J AGED exact-NSN unit prices × **current live quantity basis**.  
**Profit floor:** ≥ $10,000 after known costs (engine: `maximum_allowable_supplier_cost`).  
**Do not** reuse Phase J max supplier costs blindly — they used historical award quantities.

Freight, packaging, compliance, financing, contingency: **not quantified from live quotes** (outreach forbidden). Max supplier costs below assume those are still inside the residual after $10k profit — i.e. they are **upper bounds if all other costs ≈ $0**. Real max is lower once packaging/freight/compliance are known.

---

## Margin cases (illustrative)

For each deal, after projected revenue \(R\):

| Case | Max supplier cost (if other costs = 0) |
|------|----------------------------------------|
| $10k profit floor | \(R - 10{,}000\) |
| 15% margin | \(0.85 R\) |
| 20% margin | \(0.80 R\) |
| 25% margin | \(0.75 R\) |

Margins are **not** assumed achievable.

---

## 1. Pipefitter toolkit — 5180-00-596-1509

| Input | Value |
|-------|-------|
| History | SPRDL119F0107 / 26 EA / $124,120.88 → **$4,773.88 / EA** |
| Recency | AGED |
| Live qty | **40 EA** (base; option 40 excluded) |
| Projected revenue basis | 40 × 4,773.88 = **$190,955.20** |
| Phase J max (26 EA basis) | $114,120.88 — **superseded** |

| Metric | Amount |
|--------|--------|
| `MAX_DELIVERED_SUPPLIER_COST` ($10k) | **$180,955.20** |
| `MAX_UNIT_SUPPLIER_COST` | **$4,523.88** |
| 15% margin max supplier | $162,311.92 |
| 20% margin max supplier | $152,764.16 |
| 25% margin max supplier | $143,216.40 |

**Funding:** `FINANCING_REQUIRED` — ~$140–180k inventory/build before payment; zero-cash only if supplier terms + PO finance exist (unproven) → not `ZERO_CASH_PLAUSIBLE`.

**Quote outreach justified?** No — eligibility/TDP/assembly path unresolved.

---

## 2. Turbine support — 2840-00-411-8852

| Input | Value |
|-------|-------|
| History | SPRTA120P0012 FMS / 25 EA / $188,890 → **$7,555.60 / EA** |
| Live qty | **39 EA** |
| Projected revenue | 39 × 7,555.60 = **$294,668.40** |

| Metric | Amount |
|--------|--------|
| `MAX_DELIVERED_SUPPLIER_COST` | **$284,668.40** |
| `MAX_UNIT_SUPPLIER_COST` | **$7,299.19** |
| 15% / 20% / 25% | $250,468 / $235,735 / $221,001 |

**Status:** Hypothetical only — solicitation **appears expired**. Do not fund or outreach.

---

## 3. Diesel engine — 2815-01-536-9262

| Input | Value |
|-------|-------|
| History | W56HZV20F0427 / 38 EA / $318,014.40 → **$8,368.80 / EA** |
| Live qty | Est. **90 EA year 1** — **not firm** |
| Illustrative revenue | 90 × 8,368.80 = **$753,192** |

| Metric | Amount |
|--------|--------|
| Illustrative `MAX_DELIVERED_SUPPLIER_COST` | $743,192 |
| Gate | **`LIVE_QTY_UOM_UNCONFIRMED`** + OEM restriction |

**Economics blocked** for bid pricing until firm order quantity exists and HATZ path exists.

---

## 4. Transmission kit — 2520-01-682-2226

| Input | Value |
|-------|-------|
| History | SPRDL120C0108 / 100 EA / $1,952,080 → **$19,520.80 / EA** |
| Live qty basis | Guaranteed min **180 EA** (Amd 0001) |
| Projected revenue (min) | 180 × 19,520.80 = **$3,513,744** |
| Est. annual 225 | $4,392,180 |
| Phase J max (100 EA) | $1,942,080 — **superseded** |

| Metric | Amount (180 min) |
|--------|------------------|
| `MAX_DELIVERED_SUPPLIER_COST` | **$3,503,744** |
| `MAX_UNIT_SUPPLIER_COST` | **$19,465.24** |
| 15% / 20% / 25% | $2,986,682 / $2,810,995 / $2,635,308 |

**Funding:** `FINANCING_REQUIRED` → effectively `OWNER_CASH_REQUIRED` under current posture without arranged multi-million PO finance. **Not** zero-cash plausible for a first transaction.

---

## Funding summary

| Deal | Funding class | Zero-cash? |
|------|---------------|------------|
| Pipefitter | `FINANCING_REQUIRED` / `SUPPLIER_TERMS_REQUIRED` | Unproven |
| Turbine | N/A (expired) | — |
| Diesel | `FINANCING_REQUIRED` + auth | No |
| Transmission | `FINANCING_REQUIRED` / capital demotion | No |

---

## Bottom line

Recalculated ceilings are **higher** than Phase J for pipefitter and transmission because live quantities exceed historical award quantities. That does **not** make them quote-ready — it increases capital and compliance burden.
