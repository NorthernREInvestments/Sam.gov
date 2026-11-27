# Historical Research Workflow (on demand)

Historical pricing is **optional**. Missing history → `UNKNOWN`. Do not block evaluation solely because awards were not found.

Canonical code: `run_historical_award_research` (`m3/research/engine.py`), `historical_match.py`, `historical_usaspending.py`, commercial panel, opportunity detail UI.

## When it starts

Only for an **active** opportunity with enough product identity to search (title + MPN/NSN when available).  
Do not scrape unrelated histories in the background. Scheduler stays off for historical jobs.

## Steps

1. **Understand current solicitation** — products, MPN/NSN, qty, UoM, delivery (existing SOP / AI read).
2. **Select sources** — e.g. NSN → DIBBS when unlocked; federal keyword → USAspending lead; state product → MO / SmartBuy / BSO / PA / LA as documented. Do not blindly hit every source.
3. **Targeted search** — exact NSN → MPN → model → description. Prefer free structured APIs.
4. **Retrieve only relevant evidence** — award notice, tab, PO, catalog schedule. No archive dumps.
5. **OpenAI comprehension** — read retrieved text; emit `match_class` + **`match_reason`** (why it matches / doesn’t) + `pricing_type`.
6. **Verify comparison** — deterministic ID overrides when both sides have NSN/MPN (`classify_identifier_match`).
7. **Apply** — `build_commercial_evidence` uses unit price only when relevance is not `MARKET_CONTEXT_ONLY` / `UNUSABLE`.
8. **Stop** — enough verified/qualified evidence, or sources exhausted → finish. Empty list is success with UNKNOWN.

## Stopping conditions

- At least one `VERIFIED_COMPARABLE` or `QUALIFIED_COMPARABLE` unit price for the line(s) in scope, **or**
- No remaining qualified free sources without login/CAPTCHA/paid spend, **or**
- Owner budget / OpenAI monthly comfort band exhausted for the period.

## OpenAI role

Research + comprehension + **match explanation**. Never invent awards. Every unit price must cite a retrieved source URL/document.
