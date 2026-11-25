# M3 NextBid Offline Recovery — Summary

- Ran: `2026-11-25T05:01:45.044082+00:00`
- Runtime: `3.84s`
- API expenditure: **ZERO**
- Acquisition locked: **True**
- Ledger: used 3454 / remaining 2546 (verified via `controller_status()/load_budget_state`)

## Counts

1. Retained responses inspected: **450**
2. Markdown bodies recovered: **5**
3. Markdown chars: **26579**
4. Attachment refs: **52** (source_documents=38)
5. Local text bodies: **5** (PDFs not claimed)
6. Enrichment rows (not search-only): **12**
7. Full-text store updated/kept: **0** / **5**
8. Evidence vault puts: **5**
9. Legacy per-NB dumps removed: **7954**
10. Category C paid gaps: **2**
11. Pricing-now delta this run: **0** (notice text recovered; firm qty/specs still in unretrieved originals)

## Recommended one-call (owner authorize later)

```json
{
  "nb_id": "NB00321439",
  "title": "Oliver Hill New Courtroom Furniture",
  "source_documents_count": 18,
  "stored_original_refs_metadata_only": 18,
  "document0_md_paths": [
    "nb2/published/opengov/document0/NB00321439/NB00321439.document0.md"
  ],
  "recommended_endpoint": "read_file",
  "expected_credit_cost": 1
}
```

Evidence: `enrichment.jsonl` + `solicitation_full_text` + evidence vault. No per-solicitation audit files.

**STOP FOR OWNER REVIEW.**

## Owner inventory after reprocess

| Metric | Count |
|---|---:|
| all_active_products | 6578 |
| potential_products | 5639 |
| requirements_incomplete | 161 |
| access_required | 6407 |
| pricing_now | 14 |
| needs_investigation | 2301 |
| submission_verified | 0 |

Pricing-now unchanged at 14 — recovered notices lack firm ordered quantities.
