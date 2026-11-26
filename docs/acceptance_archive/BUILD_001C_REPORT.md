# M3 2.0 Build 001C — Final User Management Simplification

**Date:** 2026-11-21  
**Build ID:** `20261121-m3-2-build-001c`  
**Deploy:** https://samgov-production.up.railway.app  
**Scope:** Team UX / invitations / activity labels only. No Build 002.

## Correction status

| # | Correction | Status |
|---|------------|--------|
| 1 | Clean Team list (Name, Email, Role, Status, Edit, Remove) | **VERIFIED** |
| 2 | Add User focused form → Create Invitation + Copy link | **VERIFIED** |
| 3 | Edit user focused page (name/email/role/status/password) | **VERIFIED** |
| 4 | Remove with confirmation; archive vs delete; protect owner | **VERIFIED** |
| 5 | Pending invitations in team list; copy/reissue/revoke | **VERIFIED** |
| 6 | Readable recent activity + actor names + human times | **VERIFIED** |
| 7 | Desktop/mobile + Railway deploy | **VERIFIED** |

## Workflow for replacing a wrong test account

1. Settings → Team → **Remove** on the incorrect account (confirm).
2. Unused test accounts are deleted; accounts with history are archived (login revoked, attribution kept).
3. Click **Add User** → enter correct name/email/role → **Create Invitation**.
4. **Copy Invitation Link** and send it.
5. Invitee opens Sign up, sets their own password.

Samantha’s account is never removed automatically; the remove screen warns if the name/email looks like Samantha and still requires explicit confirmation.

## Notes

- Primary owner cannot be removed or edited via team tools.
- Email changes invalidate sessions and require invitation acceptance on the new address.
- No public registration; invitation token required.
- Recent Activity shows labels like “Searched for contracts” with the person’s name when known.

## Deploy

- Live: https://samgov-production.up.railway.app
- `GET /healthz` → `{"ok":true,"build":"20261121-m3-2-build-001c"}` (**verified** 2026-11-20)
- Railway deployment `1c1bbba9` · SUCCESS
- Local smoke: remove unused test → invite → accept/password → email-change session revoke → owner-only invite gate — **passed** (no production test accounts left behind).
- Additive schema only (`team_status`, `pending_email`, invitation purpose/name). No credential wipe. Samantha not auto-removed.

## Owner workflow (wrong test email → correct invite)

1. Settings → Team → **Remove** on the incorrect account (confirm).
2. **Add User** with the correct name/email/role → **Create Invitation**.
3. **Copy Invitation Link** (note expiry) and send it.
4. Invitee opens the link, sets their own password.

## Await

Owner acceptance. Do not begin Build 002 automatically.
