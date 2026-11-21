# Build 002C — Pre-implementation Audit (Build 002B baseline)

## Why 3,661 remain uncertain

Quantified causes (from BUILD_002B_ACCEPTANCE / report):

| Cause | Approx. scale | Mechanism |
|-------|--------------:|-----------|
| Thin HTML-harvest titles only (no description/line items) | Majority of ~2,863 `requires_further_verification` | `html_link_harvest` stores title + URL; classifier lacks purchasing context |
| Detail-page sampling capped at **80** | 3,700 investigated but only 80 details fetched | `resolve_uncertain_batch(detail_limit=80)` |
| Forecast / buying-plan rows kept in population | **798** marked forecast in uncertain pass | Status=forecast; not forced to product |
| Commodity label without product terms | Subset of unresolved | Explicitly kept uncertain (correct) |
| Unknown solicitation_status (2,926) | Correlated with thin evidence | Status unknown ≠ closed; classification still needs requirement text |

## Why original 811 (002A) were not fully resolved

002A uncertain (~811) sat inside a smaller product-lane population. 002B **expanded** discovery to 5,181 unrestricted records, then reclassified centrally. New HTML/state-portal titles entered as `unknown_mixed`, so the uncertain count **rose** even while a few prior items resolved. The 811 were not abandoned; they were absorbed into a larger, less evidence-rich pool, and Stage 4 only deep-fetched 80 pages.

## Why OpenAI-assisted run showed 8,797 vs harness 5,181

| Factor | Effect |
|--------|--------|
| OpenAI portal discovery enabled | +44 suggestions; **18** new `html_link_harvest` registry rows enabled |
| Prior discovered Socrata/HTML sources already in DB | Second run searched a larger enabled-source set |
| Harness used `max_pages=8` and `enable_openai_sources=False` | Lower population than the OpenAI-enabled follow-up |
| Live listing churn | Some legitimate deltas expected |

Not all +3,616 were unique *active product* solicitations — many were additional unique notices (including services/construction/uncertain) from newly enabled portals and deeper harvest.

## Source permanence

OpenAI/catalog discoveries are written to `SourceRegistry` with `config.discovered=True` and remain enabled across runs (subject to schema quarantine for non-solicitation Socrata). They are **persisted**, but Build 002B did not fully populate yield/pagination/detail-retrieval intelligence fields — 002C must harden that.

## Classification reconciliation (5,181)

Sum of breakdown classes = **5,181** exactly (3661+796+411+96+75+63+33+28+11+7).

`verified_product=600` ≠ sum of product classes (864) because verified active excludes forecast/closed/cancelled statuses.

`confirmed_non_product=656` ≈ services+construction+professional+maintenance+software (411+96+75+63+11=656).

## Incomplete descriptions / listing-only sources

- **html_link_harvest** state portals: listing links / titles only → primary uncertain source.
- **Socrata** seeds: richer fields when mapped; still may lack line items/attachments.
- **Forecast** datasets (e.g. Cook County): not currently biddable; status-tagged.

## Parsing / retrieval failures blocking classification

1. Detail fetch limited (80).  
2. Official URL often points to dataset homepage or PDF binary with NUL bytes (sanitized; OCR not run in 002B).  
3. No attachment/PDF/DOCX/XLSX extraction pipeline in 002B.  
4. No Level-4 batched AI for hard cases under budget.
