# Change Order / Amendment Watch

## Owner rule

Governments issue change orders / addenda / revisions — sometimes before close, sometimes after.

Example: Contract 1234 closes 11/30; a change posts 11/29 or 12/5. M3 must catch it **the day it is live**.

If **no** change appears, after **30 days past close** remove the contract from working files.

**Exception:** solicitations **we bid on** are retained differently (not auto-purged by this policy).

## Software

| Piece | Role |
| --- | --- |
| `m3/research/change_watch.py` | Fingerprints, compare, 30d decision, soft-purge, bid protection |
| `python -m m3.research.jobs change-watch-scan` | Baseline/compare stored package fingerprints |
| `python -m m3.research.jobs closed-retention-30d` | Dry-run soft-archive pass (`--apply` to execute) |
| `deadline_retention.py` | Existing 48h actionable-queue hold (still applies) |

## Detection

Fingerprint = document URL set + amendment count + content hashes when present.

Change reasons include: new/removed URLs, amendment count change, content hash change.

First scan records a **baseline** (not a change). Later scans with new addenda → `CHANGE_DETECTED` + `change_unresolved` until `acknowledge_change`.

## Retention clock

`purge_after = max(close_date, last_change_at) + 30 days`

Unresolved change → do not purge. Bid protected → do not purge.

## Safety

- Scheduler **not** auto-enabled (run jobs manually / owner enable later).
- Soft-archive (`Opportunity.status=archived`, package `ARCHIVED_AFTER_CLOSE`) — not a historical warehouse wipe.
- Paid evidence never deleted by this path.
- Permanent “never delete procurement records” hygiene: this policy **archives working inventory**; bid/award business records stay protected.

## Next pass

Live same-day portal re-discover (per-source `discover_docs`) on watched packages so new FileDownload / PDF links are seen without waiting for a full free-source daily pull.
