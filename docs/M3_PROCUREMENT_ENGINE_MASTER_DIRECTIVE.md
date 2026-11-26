# M3 — PROCUREMENT ENGINE MASTER DIRECTIVE

**Status:** Permanent operating standard  
**Adopted:** Build 002Q (2026-11-22)  
**Applies to:** All future procurement builds unless explicitly superseded by owner written authorization

## Primary business objective

M3 Supply LLC is a tangible-product resale business. This software exists to generate revenue by discovering, qualifying, preparing, and facilitating as many legitimate, profitable procurement bids as possible.

Governing philosophy:

**DISCOVER BROADLY. FILTER QUICKLY. VERIFY ACCURATELY. BID AGGRESSIVELY.**

There is no arbitrary maximum number of opportunities to discover. Never manufacture opportunities or relax qualification standards to achieve numerical targets. Maximize actual accessible market coverage.

## Non-negotiable operating constraints

- Zero additional paid procurement subscriptions without owner authorization.
- Zero unauthorized API spending.
- Zero metered NextBid calls while the emergency lock is engaged.
- SAM.gov and DLA/DIBBS remain disabled until explicitly authorized.
- OpenAI daily ceiling $1.00; per-job ceiling $0.50 unless owner authorizes a one-time change.
- No paid OpenAI web searches unless owner-authorized.
- No automatic spending-limit increases.
- No premium-dependent procurement sources / temporary free trials / paid document downloads.
- No automatic bid submissions.
- No destructive database resets.
- No automatic Railway deployment without owner acceptance.

## Pipeline stages (functional)

1. Discovery — persist original solicitation immediately.
2. Rapid screening — deterministic duplicates, expired/canceled, forecasts, confirmed construction/services/irrelevant IT.
3. Product identification — tangible merchandise (not an exclusive whitelist).
4. Document acquisition — original package, specs, schedules, amendments, Q&A, submission instructions.
5. Document analysis — products, quantities, units, specs, brand/equivalents, delivery, deadlines, eligibility, install/insurance/bonding/licensing, submission method.
6. Bid qualification — meets current M3 bidding requirements.
7. Commercial evaluation — suppliers, pricing, margins, financing, freight, fulfillment.
8. Bid preparation — owner-approved bid information package.

Stage C (verified product + firm quantity) alone is **not** proof an opportunity is fully bidable.

## Permanent solicitation processing SOP (executable)

Canonical enforcement module: `m3/research/solicitation_sop.py`.

- Ordered durable stages: SOURCE_REGISTERED → … → OWNER_QUEUE_UPDATED.
- No paid NextBid call without `plan_acquisition_decision` + `set_acquisition_decision` (wired into `nextbid_controller._reserve`).
- OpenAI path remains `local_evidence` → `ai_document_analysis` → `ai_output_verify` → commercial gates; AI confidence alone never promotes.
- Preview-only packages terminate as incomplete; never treated as full evaluation.
- Job: `python -m m3.research.jobs solicitation-sop` (zero spend validation / fresh inventory).

## Permanent ordered solicitation analysis (all sources)

Canonical owner operating rule. Applies to BidNet, NextBid, free portals, and every future source. Do not invent parallel pipelines.

1. **Read and understand the complete solicitation body first** (detail/body text). Do not start with the attachment pile.
2. **Reject** service, installation, maintenance, and construction contracts; move on.
3. If it is a **product purchase**, extract all available information directly from the solicitation body.
4. Identify **missing** information and examine **relevant** attachments only for those gaps.
5. Examine relevant specifications, contractual requirements, and amendments for **hidden obligations**, even when product fields already look complete.
6. Reconcile duplicate product descriptions against **actual quantities and line items**.
7. Verify closing date, eligibility, and submission requirements.
8. **Only** promote to supplier-quote / verified-ready when necessary information is complete and verified.

Additional permanent constraints for this path:

- Prefer existing stored full text, package excerpts, and inventory before any network retrieval.
- Do not spend additional API / metered credits without owner authorization.
- Never guess. Never fabricate fields. Never promote incomplete information as verified.
- Objective: read, understand, extract, verify — not overcomplicate.

### Hard rule — no listing previews as solicitation bodies

Never treat thin/listing previews as the solicitation. Before analysis, assemble **all** locally retained evidence (`m3/research/local_evidence.py`). Do not claim a complete solicitation was reviewed unless required available documents were examined.

### Hard rule — deadlines

A past authoritative submission deadline **blocks** actionable bidding queues even if inventory says `open`. Inventory open must never override a past deadline. Expired records remain in history; evidence is never auto-deleted.

### Actual AI document understanding (not keyword scanning)

Production AI path: `m3/research/ai_document_analysis.py` → `analyze_solicitation_documents`.

- Language-model analysis of assembled source text (tables/schedules/amendments/attachments when locally retained).
- Deterministic verification: `m3/research/ai_output_verify.py`.
- Cheap prefilter may skip obvious non-products; **rules are never the final understanding step** when AI is authorized.
- Cache by corpus hash + model + prompt version.
- Fail-closed budgets. **No live OpenAI or NextBid calls without explicit owner authorization.**
- Mocked/offline validation must never be described as live AI analysis.
- AI analysis alone never satisfies promotion gates (`supplier_quote_ready` / `currently_open_verified` / `bid_submission_ready`).

## Owner-facing queues

- Qualified opportunities ready for commercial evaluation
- Opportunities awaiting supplier quotations
- Opportunities awaiting financial evaluation
- Bids ready for owner approval
- Promising opportunities requiring verification
- Missing-document opportunities
- Restricted-source opportunities
- Future recurring-purchase opportunities
- Archived and confirmed disqualified records

Unresolved opportunities must never appear as verified/bid-eligible fits.

## Source verification standard

Maintain separate **verified** and **unverified/restricted** source registries. A connector is operational only when it retrieves legitimate procurement records (or correctly establishes zero active records), not merely HTTP 200.

## Owner-approved source allowlist (Build 002Q controlled reset)

- Production acquisition executes **only** sources on the owner allowlist with `live_acquisition=true`.
- A discovered source is **not** an approved source. Discovery must not auto-activate connectors.
- NextBid remains the paid baseline inventory source; live NextBid calls require lock release + owner authorization.
- Free sources enter production only after the owner provides verified authority, URL, document access, method, pagination, and cost restrictions.
- Preserve connector code and historical data when disabling unapproved sources.

## Efficiency and incremental acquisition

Bounded concurrency, checkpoints, content hashing, document dedupe, cached extraction/classification, timeouts, circuit breakers, failure isolation. Daily runs are incremental updates, not full historical rebuilds.

## Decision test

Every major engineering decision must support at least one of: more legitimate opportunities, faster acquisition, faster qualification, better document access, lower operating cost, less manual work, higher bid throughput, better execution/profitability.

Do not confuse software complexity with business progress. The owner needs real opportunities M3 can pursue.

## 23. Mandatory file hygiene and project maintenance

M3 must remain clean, organized, and maintainable throughout development.

### Permanent requirements

- Maintain one canonical production implementation of each component.
- Maintain one canonical acquisition runner.
- Maintain one canonical scheduler.
- Maintain one canonical configuration system.
- Maintain one organized automated test suite.
- Update existing functionality instead of creating redundant versioned scripts.

### Prohibited practices

Do not accumulate:

- Temporary RUN scripts
- Build-specific execution scripts
- Duplicate test scripts
- Obsolete acquisition scripts
- Temporary diagnostic files
- Redundant configuration files
- Duplicate implementations
- Abandoned experimental code
- Unnecessary database copies
- Intermediate output files
- Obsolete build reports in production directories

### Build completion cleanup

Before declaring ANY build complete:

1. Identify all temporary files created during the build.
2. Identify obsolete or superseded components.
3. Verify that no active functionality depends on them.
4. Consolidate reusable functionality into canonical production modules.
5. Incorporate reusable tests into the permanent test suite.
6. Remove verified disposable files.
7. Archive necessary historical records outside the active production structure.
8. Verify application startup and functionality after cleanup.

### Data protection

Never delete:

- Procurement records
- Solicitation documents
- Supplier information
- User data
- Active configuration files
- Production credentials
- Required migrations
- Unique test coverage
- Uncommitted owner changes

Preserve recoverable version history through Git rather than maintaining multiple copies of production code.

Keep build acceptance records in one organized archive. Do not retain redundant copies elsewhere.

### Acceptance requirements

Every completed build must report:

- Redundant files removed
- Temporary files removed
- Components consolidated
- Historical records archived
- Production functionality verified after cleanup

No build is complete until its cleanup requirements have passed.

This section is enforced permanently across all future M3 development.
