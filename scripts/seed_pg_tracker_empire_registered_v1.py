"""Seed Empire State REGISTERED into persistent tracker (Railway /data)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    from bidnet_auth.purchasing_group_registration import (
        EMPIRE_STATE_NAME,
        EMPIRE_STATE_PG_ID,
        audit_purchasing_groups_from_store,
    )
    from bidnet_auth.purchasing_group_tracker import (
        STATUS_REGISTERED,
        load_tracker,
        tracker_path,
        upsert_group,
    )

    audit = audit_purchasing_groups_from_store()
    upsert_group(
        EMPIRE_STATE_PG_ID,
        name=EMPIRE_STATE_NAME,
        status=STATUS_REGISTERED,
        registration_completed_at="2026-10-14T00:43:00+00:00",
        relevant=True,
        geographic_hint="New York / Empire State",
        solicitations_unlocked=1,
        confirmation_excerpt=(
            "Member Since COMPLETED verified live; solicitation 9797265001 unlocked"
        ),
    )
    t = load_tracker()
    out = {
        "ok": True,
        "path": str(tracker_path()),
        "metrics": t.get("metrics"),
        "empire_status": (t.get("groups") or {}).get(EMPIRE_STATE_PG_ID, {}).get("status"),
        "audit_groups": audit.get("groups_from_urls"),
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
