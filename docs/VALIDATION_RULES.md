# Validation Rules (mandatory)

## Package completeness

- **Forbidden:** text-length thresholds as proof of complete package.
- Labels (`document_inventory.package_completeness`): `listing_only`, `substantive_solicitation_body`, `partial_document_package`, `complete_relevant_package`, `complete_package_reviewed`, `package_completeness_unverified`.
- Inventory ≠ contents. Filename ≠ downloaded. Extracted text length ≠ reviewed.

## Document statuses

`DISCOVERED` → `DOWNLOAD_PENDING` → `DOWNLOADED` → `TEXT_EXTRACTED` → `ANALYZED` → `VERIFIED`; also `SUPERSEDED`, `RESTRICTED`, `FAILED`.

## Supplier-pricing ready (full)

Requires open + verified deadline, agency, all line items in award scope, quantities (or documented usage structure), UoM, material specs, brand/substitution rules, delivery info needed for quote, relevant docs reviewed, no invalidating amendment, no eligibility/service blockers, evidence-backed critical fields.

Unknown mandatory quantity on any verified line → **blocks full** supplier-quote ready (may allow `partial_supplier_quote`).

## Bid-ready

Requires supplier-pricing ready **plus** supplier pricing, availability/commitment, delivery/freight costs, financing feasibility, profitability, forms/certs/submission, amendment acknowledgment. Owner approval still required to submit.

## AI

- Snippets must appear in assembled corpus.
- `supplier_quote_ready` / `bid_submission_ready` from the model are ignored unless deterministic gates pass.
- `complete_solicitation_reviewed` only when coverage allows + package label complete + no pending downloads.

## Deadlines / retention / rejection

- Expired → leave actionable queues immediately; 48h post-close retention for short working hold; then archive from actionable inventory.
- **Change orders / amendments:** tracked packages are fingerprinted; new docs/URLs the day they appear → `CHANGE_DETECTED` (invalidates prior approvals until acknowledged). See `change_watch.py`.
- **30-day closed purge:** if closed and **no** unresolved change and **we did not bid**, soft-archive working opportunity/package after `close` (or last change) **+ 30 days**. Bid-protected records (`status=pursuing` / `bid_submitted`) are retained differently — not auto-purged here.
- Soft-archive only in this pass — **never delete paid evidence** (NextBid paid bodies, etc.).
- Unsuitable → rejection fingerprint; block reacquisition/AI re-spend.
- Amendments invalidate affected approvals; do not auto-reactivate rejects.

## Spending

No metered call without authorization + acquisition decision; emergency stop fails closed; uncertain timeouts stay reserved until provider reconcile.

## Historical pricing

See **`HISTORICAL_PRICING_VALIDATION_RULES.md`**. Do not invent unit prices from contract totals; do not show `$0` for missing history; optional for bid evaluation.
