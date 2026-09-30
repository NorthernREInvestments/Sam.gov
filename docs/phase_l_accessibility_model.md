# Phase L — Accessibility Model

Updated in **Phase L.1**.

## Fields

| Field | Values |
|-------|--------|
| `competition_access_type` | `OPEN_MARKET`, `TOTAL_SMALL_BUSINESS`, `UNRESTRICTED`, `VEHICLE_ONLY`, `BPA_ONLY`, `IDIQ_ONLY`, `GWAC_ONLY`, `MAS_ONLY`, `SEWP_ONLY`, `SOLE_SOURCE`, `LIMITED_SOURCE`, `SPECIAL_SET_ASIDE`, `SOURCE_APPROVAL_REQUIRED`, `UNKNOWN` |
| `our_bid_access` | `YES` / `NO` / `CONDITIONAL` / `UNKNOWN` — **can we legally/contractually compete** |
| `can_compete` | `true` iff `our_bid_access == YES` |
| `submission_readiness` | `READY` / `REGISTRATION_PENDING` / `NOT_READY` — administrative completeness |
| `access_blocker` | True eligibility blockers only |
| `readiness_blocker` | Admin/setup gaps (registration, SAM incomplete, bond unconfirmed, …) |
| `registration_gate_type` | `NONE`, `EASY_REGISTRATION`, `REGISTRATION_WITH_DOCS`, `LONG_LEAD_REGISTRATION`, `RESTRICTIVE_ELIGIBILITY`, `UNKNOWN` |
| `registration_action` | `NONE`, `REGISTER_NOW`, `REGISTER_BEFORE_BID`, `REGISTER_NOW_RECURRING_BUYER`, `VERIFY_REGISTRATION_TIMING`, `BLOCKED` |
| `is_easy_registration` | Ordinary vendor/portal signup (not a true eligibility block) |

## State/local extras

- `jurisdiction`, `portal_source`
- `vendor_registration_required` / `status` / `lead_time_days`
- `local_preference_exists` / `pct` (ranking signal; not auto-reject)
- `resident_vendor_required` → typically `NO`
- `bond_required`, `license_required`
- `cooperative_membership_required` → typically `NO`

## Decision rules (L.1)

1. Wrap `eligibility_gate` — do not invent vehicle holdings. `ELIGIBILITY_NOT_APPLICABLE` → baseline **YES** (no vehicle gate).
2. Vehicle-only / BPA / IDIQ / GWAC / MAS / SEWP / sole-source / ASL → `NO` when company does not hold them.
3. Total SB: consult `company_eligibility.held_certifications` / `set_aside_eligibility` and profile `sba_size_status`. Default env SB → **YES**. `OTHER_THAN_SMALL` → **NO**.
4. Special socioeconomic set-asides (WOSB/SDVOSB/8(a)/…) without held cert → `NO` / not YES.
5. Open/unrestricted with no **true** blockers → **YES**. Empty SAM / unknown unrelated fields must **not** force CONDITIONAL.
6. Easy vendor registration → keep **YES**, set `registration_required` / `REGISTER_BEFORE_BID`, economics **still runs**.
7. Registration lead time vs runway: only when **documented** lead time exists. `runway − reg < 3` → `NO`; `< 5` → `CONDITIONAL` + `VERIFY_REGISTRATION_TIMING`. Unknown lead time does **not** auto-block.
8. **Hard rule:** actionable / READY_FOR_OWNER_REVIEW requires `our_bid_access = YES`. Easy registration alone is never a reject.

## Why this matters

Historical low offer counts often reflect **pre-filtered** pools (BPA/IDIQ/SEWP). Accessibility must be decided **before** expensive retail/history work — but easy admin signup must not starve economics.
