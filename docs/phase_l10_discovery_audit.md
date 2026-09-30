# Phase L.10 — Discovery Coverage Audit

## Method

`phase_l.discovery_audit.audit_discovery_coverage` scores every accessible row by `raw_ref.source_id` / platform for live/open, stale, duplicate, tangible, commercial, authoritative ID, attachments, and award/history access.

Platform history adapters are **separate** from discovery adapters (`phase_l.platform_history`).

## Platforms inventoried

SAM, DLA/DIBBS, BidNet, OpenGov, Bonfire, Public Purchase, DemandStar, PlanetBids, IonWave, Jaggaer/SciQuest, state portals, cooperatives.

## Findings (L.10 accessible corpus)

- **BidNet-heavy**: majority of YES-access rows are `network_bidnet_*` state feeds.
- **SAM**: ~292 federal public-search rows.
- **City/state agency**: small Milwaukee slice + thin direct agency portals.
- **Missing / zero in accessible set**: OpenGov, Bonfire, IonWave, PlanetBids, DemandStar, Jaggaer, Public Purchase, DLA/DIBBS, cooperatives — present in inventory/hunt mix but not retained in current accessible Stage-3 path at meaningful volume.
- Attachment fields sparse (`ATTACHMENT_GAP`).
- Award/history on rows mostly absent except historical unit price on some federal (`AWARD_HISTORY_GAP`).
- Auth-walled tabulations dominate blocker for buyer-exact Gov A.

Prior hunt (COMPLETE): ~12,029 unique raw / 41 sources successful / accessible ~1,944.

**National coverage is not claimed.**
