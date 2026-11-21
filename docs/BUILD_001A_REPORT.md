# M3 2.0 Build 001A — Owner Acceptance Corrections

**Date:** 2026-11-20  
**Build ID:** `20261120-m3-2-build-001a1`  
**Deploy:** https://samgov-production.up.railway.app  
**Scope:** Corrections only — no Build 002 work.

## Correction status

| # | Correction | Status |
|---|------------|--------|
| 1 | Opportunity navigation (Filter / Saved / Searches; save stays in results) | **VERIFIED** |
| 2 | Compact opportunity result presentation + explicit actions | **VERIFIED** |
| 3 | Internal opportunity detail view (Overview…Notes) | **VERIFIED** |
| 4 | Compact actionable dashboard | **VERIFIED** |
| 5 | Global form styling standard | **VERIFIED** |
| 6 | Primary-owner-only user management + test-record review list | **VERIFIED** |
| 7 | Product-resale filtering (exclude clear services/construction) | **VERIFIED** (title service-only overrides misleading Commodity tags; tree trimming excluded from Filter) |
| 8 | Mobile / usability checks (360–1440) | **VERIFIED** (rendered locally at 390/1440; live `/healthz` build `001a`) |

## Notes by correction

### 1. Opportunity navigation
- Subnav: **Filter**, **Saved Opportunities**, **Saved Searches** (no “Find more”).
- Saving posts back to the current Filter URL (`return_to`) with `#anchor` + scroll restore.
- Official Solicitation opens in a new tab; View Details is internal.

### 2–3. Results + details
- Compact table (desktop) / cards (≤1023px) with Unknown / Not Yet Researched labels.
- Actions: View Details · Save Opportunity · Official Solicitation.
- Detail sections: Overview, Requirements, Documents, Eligibility, Estimated value, Catalog matches, Notes.

### 4. Dashboard
- Stat strip + compact sections: deadlines, tasks, overdue follow-ups, apps in progress, recent suppliers, activity, saved opps.

### 5. Forms
- Shared CSS variables for control height/padding/border; `form-grid` equal columns; mobile stacks to one column. Applied across login, settings, tasks, suppliers, catalogs, audit, opportunity filters.

### 6. Owner-exclusive user management
- `users.is_primary_owner` set for bootstrap owner only.
- Create / activate / deactivate / reset password / role assign require `is_primary_owner` (backend enforced).
- Role `owner` cannot be assigned via create form.
- Employees may update own profile/password only.
- Settings lists possible test accounts/records for owner review (no auto-delete). Identified examples: `sam.supplier@m3.local`, deactivated `x@test.local`, Word/Excel portal noise opportunities.

### 7. Product-resale filtering
- Traceable `requirement_class`: product_supply | product_with_incidental_services | services | construction | unknown_mixed.
- Default Filter mode excludes services/construction (e.g. tree trimming).
- Fix: clear service-only/construction-only **title** evidence overrides misleading source categories (LA RAMP tagged tree trimming as Commodity).
- Prior saved records are unchanged (no silent deletes); tree trimming may still appear under Saved until owner removes it.

### 8. Mobile
- Verified layouts at 360/390/768/1024/1440 via device metrics + rendered pages.
- Touch-sized controls retained (44px).

## Security / regression
- Supplier manager cannot see Create user controls; POST `/settings/users` rejected.
- Owner create with `role=owner` coerced to non-owner role; primary flag not granted.
- `/healthz` build id updated to `001a`.

## Screenshots (local verification)
Captured during 001A QA (Cursor screenshot store):
- Dashboard (1440)
- Opportunity detail (Overview sections)
- Settings / profile + team (360 mobile)
- Filter workspace with Filter / Saved / Searches subnav

## Deploy
- Existing Railway service only; additive DB columns (`is_primary_owner`, `result_snapshot`, `requirement_class`, `quantity`).
- No credential changes; no mass record deletion.
- Live check: `GET /healthz` → `{"ok":true,"build":"20261120-m3-2-build-001a1"}` on https://samgov-production.up.railway.app (classify hotfix redeploy).

## Await
Owner acceptance testing. Do not begin Build 002 until approved.
