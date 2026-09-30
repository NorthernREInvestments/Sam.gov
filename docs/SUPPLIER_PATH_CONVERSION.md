# Supplier Path Conversion

Converts `DEEP_RESEARCH_COMPLETE` rows into `READY_TO_CALL` via manufacturer channels,
distributor maps, and L.22 call sheets.

## This run

```json
{
  "starting": 235,
  "attempted": 235,
  "supplier_path_found": 9,
  "supplier_path_unresolved": 226,
  "promoted_ready_to_call": 0,
  "ending_deep_complete": 235,
  "stats": {
    "attempted": 235,
    "supplier_path_unresolved": 226,
    "category_fallback_suppliers": 8,
    "supplier_path_found": 9,
    "blocked:authoritative_source_missing": 9
  }
}
```

```json
{
  "starting": 48,
  "added": 0,
  "removed": 0,
  "ending": 48,
  "supplier_call_sheets": 0,
  "promoted_ids": [],
  "demoted_ids": []
}
```
