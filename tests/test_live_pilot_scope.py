"""Single-use live pilot authorization cannot exceed 10 credits or survive revoke."""
from __future__ import annotations

from m3.research.nextbid_controller import (
    LIVE_PILOT_CREDIT_CAP,
    LIVE_PILOT_SCOPE,
    _limits,
    acquisition_locked,
    revoke_owner_authorization,
    write_owner_authorization,
)
from m3.research.nextbid_killswitch import engage_emergency_stop


def test_live_pilot_scope_caps_at_ten(monkeypatch):
    write_owner_authorization(
        authorized=True,
        approved_allocation_calls=LIVE_PILOT_CREDIT_CAP,
        scope=LIVE_PILOT_SCOPE,
        test_call_cap=LIVE_PILOT_CREDIT_CAP,
        note="unit_test_live_pilot_scope",
        reset_test_used=True,
    )
    lim = _limits()
    assert lim["scope"] == LIVE_PILOT_SCOPE
    assert lim["daily"] == 10
    assert lim["job"] == 10
    assert lim["test"] == 10
    assert lim.get("single_use") is True
    # Cleanup — never leave auth active from unit test
    revoke_owner_authorization()
    engage_emergency_stop(reason="unit_test_live_pilot_scope_cleanup")
    monkeypatch.setenv("NEXTBID_ENABLED", "false")
    monkeypatch.setenv("NEXTBID_EMERGENCY_STOP", "true")
    from m3.config import get_settings

    get_settings.cache_clear()
    assert acquisition_locked() is True
