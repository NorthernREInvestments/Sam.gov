# Build 002L — Data Integrity Audit

**Build ID:** `20261122-m3-2-build-002l`  
**Emergency stop:** engaged  
**NEXTBID_ENABLED:** false  

## Persistence

{
  "ok": true,
  "file_registry_survives": true,
  "db_row_survives_commit": true,
  "event_history_written": true,
  "soft_archive_preserves_record": true,
  "duplicate_detection_callable": true,
  "test_record_id": "pr_4d80b6813135d80ed87d99b5",
  "test_cleaned_from_db": true,
  "note": "Isolated test record only; not a production opportunity."
}

## Database inventory

{
  "ok": true,
  "opportunities": 59,
  "solicitation_packages": 6070,
  "package_documents": 4216,
  "procurement_records": 0,
  "package_status_breakdown": {
    "ACCESS_RESTRICTED": 231,
    "COMPLETE": 508,
    "FAILED": 581,
    "PARTIAL": 47,
    "VERIFICATION_REQUIRED": 4703
  }
}

## Registry sync

{
  "ok": true,
  "written": 852,
  "registry_file_total": 853
}

## Duplicate prevention

- `find_duplicates()` matches provider id, solicitation number, canonical key, and content hash.
- Source links preserve portal-specific identities under one canonical record id.
- Soft archive never erases historical rows.

## Discovery / classification separation

- Registry stores raw discovery snapshots separately from current procurement_stage.
- Classification history is append-only.
- UNKNOWN / excluded rows remain recoverable.

## Document management

- Existing `solicitation_packages` / `package_documents` tables retained.
- Package COMPLETE requires verified mandatory documents — listing reachability alone is PARTIAL.
- No destructive cleanup performed (owner approval required).

## API safety

- Metered NextBid calls during 002L: **0**
- Acquisition locked after build: **true**
- Scheduler: not activated

## Backup

{
  "latest": "C:\\Users\\M_gra\\OneDrive\\Desktop\\M3_CLEANUP_BACKUPS\\BUILD_002L_20261121_210252",
  "verify": {
    "readable_checkpoint": true,
    "enriched": 604,
    "backup_ok": true,
    "inventory_lines": 849,
    "seen_nb_ids": 698,
    "listings": 698
  },
  "secrets_excluded": true
}


## Production deploy

- healthz build: 20261122-m3-2-build-002l
- NEXTBID_ENABLED=false / EMERGENCY_STOP=true
- DB counts: {"procurement_records": 852, "procurement_record_events": 852, "procurement_source_links": 849, "solicitation_packages": 6070, "package_documents": 4216, "opportunities": 59}
- Metered calls during 002L: 0 (provider used remains 3070)
