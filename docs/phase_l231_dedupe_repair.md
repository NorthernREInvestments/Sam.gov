# L.23.1 Dedupe Repair

## Root cause

L.23 keyed identity on `solicitation_number` (always null in `accessible_latest`) instead of `solicitation_id`, collapsing unrelated rows via weak title/agency fallbacks.

## Repair

- Prefer `solicitation_id` / notice URL / buyer+solicitation
- Exact title+buyer+deadline only as fallback
- No silent fuzzy title merges

```json
{
  "exact_duplicates_or_clones": 2976,
  "merge_reasons": {
    "kept_first": 1465,
    "canonical_url_match": 2402,
    "buyer_solicitation_match": 574
  },
  "suspected_bad_merges": 0,
  "bad_merges_fixed": "solicitation_id now used; fuzzy title-only collapse avoided for multi-sol groups"
}
```

Population: `{"raw_union": 4441, "canonical_unique": 1465, "dedupe_reduction_pct": 67.01}`
