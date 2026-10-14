"""BidNet purchasing-group registration via authenticated client.

Rules:
- Never invent company data.
- Never auto-claim MBE/DBE/WBE unless verified held.
- Never purchase / accept fees.
- Pause for owner on missing EIN, legal attestations, paid actions.
- Remember completed registrations in purchasing_group_tracker.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from application_clock import now_utc
from bidnet_auth.purchasing_group_tracker import (
    STATUS_ERROR,
    STATUS_INCOMPLETE,
    STATUS_OWNER_APPROVAL_REQUIRED,
    STATUS_PAID_REQUIRED,
    STATUS_PREPARED,
    STATUS_REGISTERED,
    get_group,
    is_registered,
    load_tracker,
    upsert_group,
)

log = logging.getLogger("govtracker.bidnet_pg_registration")

BUILD = "20261013-m3-bidnet-pg-registration-v1"

EMPIRE_STATE_PG_ID = "8411151"
EMPIRE_STATE_NAME = "Empire State Purchasing Group"

# Cert tokens that mean NY MBE / federal DBE — only answer Yes if held
_MBE_TOKENS = frozenset({"MBE", "WBE", "MWBE", "NYMBE", "NYWBE"})
_DBE_TOKENS = frozenset({"DBE", "ACDBE"})


def _utc() -> str:
    return now_utc().isoformat()


def extract_pg_id_from_url(url: str) -> str | None:
    u = url or ""
    m = re.search(r"purchasingGroupId=(\d+)", u, re.I)
    if m:
        return m.group(1)
    m = re.search(r"/purchasing-groups/(\d+)", u, re.I)
    return m.group(1) if m else None


def extract_pg_gate_from_html(html: str, *, page_url: str = "") -> dict[str, Any] | None:
    """Parse Incomplete Purchasing Group Form dialog from detail HTML."""
    text = html or ""
    if "incomplete purchasing group form" not in text.lower() and not re.search(
        r"registration form for this purchasing group is incomplete", text, re.I
    ):
        # Still try proceed button
        if "interceptorProceedBtn" not in text and "group-form/edit" not in text:
            return None
    name = None
    m = re.search(
        r"<h3>([^<]*Purchasing Group[^<]*)</h3>",
        text,
        re.I,
    ) or re.search(
        r"Empire State Purchasing Group|([A-Z][A-Za-z ]+Purchasing Group)",
        text,
    )
    if m:
        name = (m.group(1) if m.lastindex else m.group(0)).strip()
    proceed = None
    pm = re.search(
        r'id="interceptorProceedBtn"[^>]*data-href="([^"]+)"',
        text,
        re.I,
    )
    if pm:
        proceed = pm.group(1).replace("&amp;", "&")
        if proceed.startswith("/"):
            proceed = "https://www.bidnetdirect.com" + proceed
    pg_id = extract_pg_id_from_url(proceed or "") or extract_pg_id_from_url(page_url)
    fee = bool(re.search(r"\$\d|payment|credit card|purchase a plan|subscription fee", text, re.I))
    return {
        "detected": True,
        "name": name,
        "purchasing_group_id": pg_id,
        "complete_form_url": proceed,
        "message": (
            "Your organization registration form for this purchasing group is incomplete. "
            "Please complete the registration form before continuing."
            if re.search(r"registration form for this purchasing group is incomplete", text, re.I)
            else "An action is required before access to this solicitation is granted."
        ),
        "fee_mentioned": fee,
        "gate_kind": "PAID_REQUIRED" if fee else "PURCHASING_GROUP_REGISTRATION_INCOMPLETE",
    }


def load_verified_company_answers() -> dict[str, Any]:
    """Map verified company facts → registration answers. Never invent."""
    held: set[str] = set()
    try:
        from company_eligibility import held_certifications

        held = {str(x).upper().replace(" ", "").replace("-", "") for x in held_certifications()}
    except Exception:
        held = set()

    ein_present = False
    ein_value = None
    ein_source = None
    legal_name = None
    try:
        from response_engine.company_profile_r3 import load_company_compliance_profile

        prof = load_company_compliance_profile()
        legal = prof.get("legal_name")
        if legal and str(legal).upper() not in {"UNKNOWN", "NONE", ""}:
            legal_name = str(legal)
    except Exception:
        pass

    import os

    def _accept_ein(raw: str, source: str) -> bool:
        nonlocal ein_present, ein_value, ein_source
        raw = (raw or "").strip()
        if not raw or raw.upper() in {"UNKNOWN", "NONE", "REDACTED"}:
            return False
        digits = re.sub(r"\D", "", raw)
        if len(digits) not in {9, 8, 10}:
            return False
        ein_present = True
        ein_value = raw
        ein_source = source
        return True

    for key in ("COMPANY_EIN", "M3_COMPANY_EIN", "BUSINESS_EIN", "FEDERAL_TAX_ID", "EIN"):
        if _accept_ein(os.environ.get(key) or "", f"env:{key}"):
            break
    if not ein_present:
        try:
            from settings_store import get_owner_settings

            owner = get_owner_settings() or {}
            if not legal_name:
                ln = owner.get("legal_business_name")
                if ln and str(ln).strip():
                    legal_name = str(ln).strip()
            _accept_ein(str(owner.get("ein") or ""), "owner_settings.ein")
        except Exception:
            pass

    mbe_held = bool(held & _MBE_TOKENS)
    dbe_held = bool(held & _DBE_TOKENS)

    return {
        "legal_name": legal_name,
        "ein_present": ein_present,
        "ein_value": ein_value,  # only used in-process for fill; never persisted to tracker
        "ein_source": ein_source,
        "certified_mbe": "YES" if mbe_held else "NO",
        "other_mbe_dbe": "YES" if dbe_held else "NO",
        "held_certifications": sorted(held),
        "mbe_verified_held": mbe_held,
        "dbe_verified_held": dbe_held,
    }


def audit_purchasing_groups_from_store(*, limit: int = 500) -> dict[str, Any]:
    """Count live BidNet opportunities by purchasingGroupId from L23 URLs."""
    from phase_l.l23_full_population_funnel import load_store

    store = load_store()
    by_pg: dict[str, dict[str, Any]] = {}
    for oid, row in store.items():
        if not isinstance(row, dict):
            continue
        url = str(row.get("authoritative_url") or row.get("detail_url") or "")
        if "bidnet" not in url.lower():
            continue
        pg = extract_pg_id_from_url(url)
        if not pg:
            continue
        bucket = by_pg.setdefault(
            pg,
            {
                "purchasing_group_id": pg,
                "live_eligible_opportunity_count": 0,
                "sample_titles": [],
                "buyers": set(),
            },
        )
        bucket["live_eligible_opportunity_count"] += 1
        if len(bucket["sample_titles"]) < 3:
            bucket["sample_titles"].append(str(row.get("title") or "")[:80])
        if row.get("buyer"):
            bucket["buyers"].add(str(row.get("buyer")))
        if len(by_pg) >= limit and pg not in by_pg:
            break

    # Persist relevance counts (status unknown until inspected)
    for pg, meta in sorted(
        by_pg.items(), key=lambda kv: -kv[1]["live_eligible_opportunity_count"]
    )[:80]:
        upsert_group(
            pg,
            relevant=True,
            live_eligible_opportunity_count=meta["live_eligible_opportunity_count"],
            sample_titles=meta["sample_titles"],
            buyers=sorted(meta["buyers"])[:12],
            geographic_hint=(sorted(meta["buyers"])[0] if meta["buyers"] else None),
        )
    # Seed Empire State known
    upsert_group(
        EMPIRE_STATE_PG_ID,
        name=EMPIRE_STATE_NAME,
        relevant=True,
        geographic_hint="New York / Empire State",
    )
    return {
        "groups_from_urls": len(by_pg),
        "top": [
            {
                "purchasing_group_id": pg,
                "count": meta["live_eligible_opportunity_count"],
                "buyers": sorted(meta["buyers"])[:5],
            }
            for pg, meta in sorted(
                by_pg.items(), key=lambda kv: -kv[1]["live_eligible_opportunity_count"]
            )[:15]
        ],
        "tracker": load_tracker().get("metrics"),
    }


def inspect_group_form(client: Any, *, form_url: str, timeout_ms: int = 30_000) -> dict[str, Any]:
    """Open group-form/edit and list fields. Does not submit."""
    if not getattr(client, "is_authenticated", False) or getattr(client, "_page", None) is None:
        return {"ok": False, "error": "NOT_AUTHENTICATED"}
    if not form_url:
        return {"ok": False, "error": "FORM_URL_MISSING"}
    page = client._page
    page.goto(form_url, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        page.wait_for_load_state("networkidle", timeout=5_000)
    except Exception:
        pass
    page.wait_for_timeout(1_200)
    final = str(page.url or "")
    body = ""
    try:
        body = re.sub(r"\s+", " ", page.locator("body").inner_text() or "")[:4000]
    except Exception:
        pass
    fields = page.evaluate(
        """() => {
          const out = [];
          for (const n of Array.from(document.querySelectorAll('input, select, textarea'))) {
            const type = (n.getAttribute('type') || n.tagName).toLowerCase();
            if (type === 'hidden') continue;
            let label = '';
            if (n.id) {
              const lab = document.querySelector('label[for=\"'+n.id+'\"]');
              if (lab) label = (lab.innerText||'').trim();
            }
            const nearby = n.closest('.mets-field, .form-group, tr, .field');
            if (!label && nearby) {
              const lf = nearby.querySelector('.mets-field-label, label, th, .label');
              if (lf) label = (lf.innerText||'').trim().slice(0,160);
            }
            out.push({
              name: n.getAttribute('name') || '',
              id: n.id || '',
              type,
              label: label.slice(0,160),
              required: !!n.required || /\\*/.test(label),
              has_value: !!(n.value && String(n.value).trim()),
              checked: !!n.checked,
            });
          }
          return out;
        }"""
    )
    fee = bool(re.search(r"\$\d|payment|credit card|purchase|subscription fee", body, re.I))
    attestation = bool(
        re.search(r"I (certify|attest|declare|agree)|under penalty|legally binding", body, re.I)
    )
    member_completed = bool(
        re.search(r"Member Since.{0,40}\bCOMPLETED\b", body, re.I)
        or re.search(r"Member Since.{0,40}\bCOMPLETE\b", body, re.I)
    ) and not re.search(r"Member Since.{0,40}\bINCOMPLETE\b", body, re.I)
    return {
        "ok": True,
        "final_url": final[:220],
        "fields": fields or [],
        "body_excerpt": body[:2000],
        "fee_mentioned": fee,
        "attestation_mentioned": attestation,
        "has_save": "save" in body.lower(),
        "member_status_incomplete": bool(re.search(r"\bINCOMPLETE\b", body, re.I))
        and not member_completed,
        "member_status_completed": member_completed,
    }


def prepare_empire_state_answers(answers: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build fill plan for Empire State form from verified company data."""
    ans = answers or load_verified_company_answers()
    plan = {
        "purchasing_group_id": EMPIRE_STATE_PG_ID,
        "name": EMPIRE_STATE_NAME,
        "fills": [
            {
                "name": "fields[certifiedMBE]",
                "strategy": "radio",
                "value": ans["certified_mbe"],
                "reason": (
                    "verified_held_mbe"
                    if ans["mbe_verified_held"]
                    else "not_verified_held_answer_NO"
                ),
            },
            {
                "name": "fields[otherMBE]",
                "strategy": "radio",
                "value": ans["other_mbe_dbe"],
                "reason": (
                    "verified_held_dbe"
                    if ans["dbe_verified_held"]
                    else "not_verified_held_answer_NO"
                ),
            },
        ],
        "skip_fields": [],
        "missing_fields": [],
        "owner_required": False,
        "owner_reasons": [],
        "can_submit": False,
    }
    if ans["other_mbe_dbe"] == "YES":
        plan["missing_fields"].append(
            {
                "name": "fields[otherMBENames]",
                "label": "Which department/authority(s) are you registered with?",
                "reason": "required_when_dbe_yes",
            }
        )
    if not ans["ein_present"]:
        plan["missing_fields"].append(
            {
                "name": "fields[federalTaxPayerId]",
                "label": "Taxpayer ID (Federal Employer Number)",
                "reason": "EIN_NOT_IN_SECURE_CONFIG",
                "how_to_provide": "Set COMPANY_EIN (or M3_COMPANY_EIN) in Railway / secure env for the BidNet legal entity, then re-run.",
            }
        )
        plan["owner_required"] = True
        plan["owner_reasons"].append("Missing verified Federal EIN in secure configuration")
    else:
        plan["fills"].append(
            {
                "name": "fields[federalTaxPayerId]",
                "strategy": "text",
                "value_from": "ein_secure_env",
                "reason": "verified_ein_present",
            }
        )
    plan["can_submit"] = not plan["owner_required"] and not plan["missing_fields"]
    # Strip any ein value from returned plan
    plan["company_answers_meta"] = {
        "ein_present": ans["ein_present"],
        "certified_mbe": ans["certified_mbe"],
        "other_mbe_dbe": ans["other_mbe_dbe"],
        "legal_name_known": bool(ans.get("legal_name")),
        "held_certifications": ans.get("held_certifications") or [],
    }
    return plan


def apply_fills(client: Any, plan: dict[str, Any], *, answers: dict[str, Any]) -> dict[str, Any]:
    """Populate eligible fields on the open form page. Does not click Save."""
    page = client._page
    applied = []
    for fill in plan.get("fills") or []:
        name = fill.get("name")
        strategy = fill.get("strategy")
        if strategy == "radio":
            val = str(fill.get("value") or "NO").upper()
            # Empire State ids embed a dot: certifiedMBE_NY.NO / otherMBE_NY.YES
            known_ids = {
                ("fields[certifiedMBE]", "YES"): "certifiedMBE_NY.YES",
                ("fields[certifiedMBE]", "NO"): "certifiedMBE_NY.NO",
                ("fields[otherMBE]", "YES"): "otherMBE_NY.YES",
                ("fields[otherMBE]", "NO"): "otherMBE_NY.NO",
            }
            eid = known_ids.get((name, val))
            try:
                ok = False
                if eid:
                    ok = bool(
                        page.evaluate(
                            """(id) => {
                              const el = document.getElementById(id);
                              if (!el) return false;
                              el.click();
                              el.checked = true;
                              el.dispatchEvent(new Event('change', {bubbles: true}));
                              el.dispatchEvent(new Event('input', {bubbles: true}));
                              return !!el.checked;
                            }""",
                            eid,
                        )
                    )
                if not ok:
                    # Fallback: nth radio by name (YES=0, NO=1 for Empire State pairs)
                    page.locator(f"input[type='radio'][name=\"{name}\"]").nth(
                        0 if val == "YES" else 1
                    ).check(force=True, timeout=3_000)
                    ok = True
                applied.append({"name": name, "value": val, "ok": ok, "element_id": eid})
            except Exception as exc:
                applied.append({"name": name, "value": val, "ok": False, "error": type(exc).__name__})
        elif strategy == "text" and fill.get("value_from") == "ein_secure_env":
            ein = answers.get("ein_value")
            if not ein:
                applied.append({"name": name, "ok": False, "error": "EIN_MISSING"})
                continue
            try:
                page.locator("#federalTaxPayerId").first.fill(ein, timeout=3_000)
                applied.append({"name": name, "ok": True, "value": "REDACTED"})
            except Exception as exc:
                applied.append({"name": name, "ok": False, "error": type(exc).__name__})
    return {"applied": applied}


def run_empire_state_registration(
    client: Any,
    *,
    solicitation_id: str = "9797265001",
    form_url: str | None = None,
    allow_submit: bool = False,
    owner_approved_submit: bool = False,
) -> dict[str, Any]:
    """Bounded Empire State registration: inspect → prepare → fill → pause or save."""
    if is_registered(EMPIRE_STATE_PG_ID):
        return {
            "ok": True,
            "status": STATUS_REGISTERED,
            "skipped": True,
            "reason": "already_registered",
            "purchasing_group_id": EMPIRE_STATE_PG_ID,
        }

    answers = load_verified_company_answers()
    plan = prepare_empire_state_answers(answers)

    if not form_url:
        # Resolve via solicitation detail gate
        resolved = client.resolve_detail_via_authenticated_search(
            title="Copy Paper", timeout_ms=25_000
        )
        client._page.wait_for_timeout(1_500)
        html = client._page.content() or ""
        gate = extract_pg_gate_from_html(html, page_url=str(client._page.url or ""))
        form_url = (gate or {}).get("complete_form_url")
        if not form_url:
            # known pattern with supplier org from prior diagnostics
            form_url = (
                f"https://www.bidnetdirect.com/private/supplier/9785456456/purchasing-groups/"
                f"{EMPIRE_STATE_PG_ID}/group-form/edit?accessedElementId={solicitation_id}"
                f"&originalAction=SOLICITATION_OPEN"
            )

    inspected = inspect_group_form(client, form_url=form_url)
    if not inspected.get("ok"):
        upsert_group(
            EMPIRE_STATE_PG_ID,
            name=EMPIRE_STATE_NAME,
            status=STATUS_ERROR,
            last_error=inspected.get("error"),
        )
        return {"ok": False, **inspected, "plan": plan}

    # BidNet already shows COMPLETED — persist and never re-submit
    if inspected.get("member_status_completed"):
        upsert_group(
            EMPIRE_STATE_PG_ID,
            name=EMPIRE_STATE_NAME,
            status=STATUS_REGISTERED,
            registration_completed_at=_utc(),
            registration_required=True,
            fee_required=False,
            relevant=True,
            confirmation_excerpt=(inspected.get("body_excerpt") or "")[:400],
            form_url_path=re.sub(r"https?://[^/]+", "", form_url)[:200],
        )
        return {
            "ok": True,
            "status": STATUS_REGISTERED,
            "skipped": True,
            "reason": "bidnet_form_already_completed",
            "purchasing_group_id": EMPIRE_STATE_PG_ID,
        }

    if inspected.get("fee_mentioned"):
        upsert_group(
            EMPIRE_STATE_PG_ID,
            name=EMPIRE_STATE_NAME,
            status=STATUS_PAID_REQUIRED,
            owner_blocker_reason="Fee mentioned on purchasing-group form — owner authorization required",
            form_url_path=re.sub(r"https?://[^/]+", "", form_url)[:200],
        )
        return {
            "ok": False,
            "status": STATUS_PAID_REQUIRED,
            "blocker": "PAID_ACTION_REQUIRES_OWNER",
            "plan": plan,
            "inspected": {k: inspected[k] for k in inspected if k != "body_excerpt"},
        }

    fill_result = apply_fills(client, plan, answers=answers)

    if plan["owner_required"] or not plan["can_submit"]:
        upsert_group(
            EMPIRE_STATE_PG_ID,
            name=EMPIRE_STATE_NAME,
            status=STATUS_OWNER_APPROVAL_REQUIRED,
            registration_required=True,
            fee_required=False,
            owner_blocker_reason="; ".join(plan["owner_reasons"]) or "missing_fields",
            missing_fields=plan["missing_fields"],
            prepared_fills=[
                {k: v for k, v in f.items() if k != "value"} for f in plan.get("fills") or []
            ],
            form_url_path=re.sub(r"https?://[^/]+", "", form_url)[:200],
            last_inspection_at=_utc(),
            relevant=True,
        )
        return {
            "ok": True,
            "status": STATUS_OWNER_APPROVAL_REQUIRED,
            "submitted": False,
            "populated": fill_result,
            "plan": plan,
            "inspected_fields": inspected.get("fields"),
            "owner_next_steps": [
                "Provide Federal EIN for the BidNet legal entity via COMPANY_EIN in Railway secure env",
                "Confirm MBE/DBE answers (M3 will answer NO unless those certs are verified held)",
                "Re-run Empire State registration proof after EIN is set",
                "M3 will then Save the form only when allow_submit+owner_approved_submit are set",
            ],
            "purchasing_group_id": EMPIRE_STATE_PG_ID,
        }

    # Free administrative submit when form has no fee/attestation and fields are complete.
    # Owner-approved flag (arg or tracker) or M3_BIDNET_PG_AUTO_SUBMIT=1 authorizes Save.
    import os

    auto = (os.environ.get("M3_BIDNET_PG_AUTO_SUBMIT") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    tracked = get_group(EMPIRE_STATE_PG_ID) or {}
    owner_approved_submit = bool(owner_approved_submit or tracked.get("owner_approved_submit"))
    attestation = bool(inspected.get("attestation_mentioned"))
    # Free mapped admin forms (no fee / no attestation) may Save when fields are complete.
    free_admin_ok = bool(plan.get("can_submit") and not attestation and not inspected.get("fee_mentioned"))
    authorized = bool(
        allow_submit and (owner_approved_submit or auto or free_admin_ok)
    )
    if not authorized:
        upsert_group(
            EMPIRE_STATE_PG_ID,
            name=EMPIRE_STATE_NAME,
            status=STATUS_PREPARED,
            registration_required=True,
            prepared_at=_utc(),
            form_url_path=re.sub(r"https?://[^/]+", "", form_url)[:200],
            relevant=True,
            owner_blocker_reason=(
                "Prepared — set M3_BIDNET_PG_AUTO_SUBMIT=1 or owner_approved_submit to Save"
                if not attestation
                else "Attestation language present — owner approval required before Save"
            ),
        )
        return {
            "ok": True,
            "status": STATUS_PREPARED if not attestation else STATUS_OWNER_APPROVAL_REQUIRED,
            "submitted": False,
            "populated": fill_result,
            "plan": plan,
            "attestation_mentioned": attestation,
            "note": "Form prepared; waiting for submit authorization",
        }

    # Refuse Save if any fill failed
    bad = [a for a in (fill_result.get("applied") or []) if not a.get("ok")]
    if bad:
        upsert_group(
            EMPIRE_STATE_PG_ID,
            name=EMPIRE_STATE_NAME,
            status=STATUS_ERROR,
            last_error="FILL_FAILED",
            fill_errors=bad,
            relevant=True,
        )
        return {
            "ok": False,
            "status": STATUS_ERROR,
            "submitted": False,
            "error": "FILL_FAILED",
            "populated": fill_result,
            "purchasing_group_id": EMPIRE_STATE_PG_ID,
        }

    # Submit Save
    try:
        client._page.locator("#saveButton").click(timeout=5_000)
        client._page.wait_for_timeout(2_500)
        url = str(client._page.url or "")
        after = re.sub(r"\s+", " ", client._page.locator("body").inner_text() or "")[:2000]
        failed_check = bool(re.search(r"Failed Check", after, re.I))
        still_on_edit = "group-form/edit" in url
        # Success signals: leave edit form without Failed Check, or Member COMPLETE banner
        member_complete = bool(
            re.search(r"Member Since.{0,40}\bCOMPLETE\b", after, re.I)
        ) and not re.search(r"Member Since.{0,40}\bINCOMPLETE\b", after, re.I)
        redirected_ok = (not still_on_edit) and (not failed_check) and (
            "group-form" in url
            or "solicitations" in url
            or "search" in url
        )
        if failed_check or (still_on_edit and re.search(r"\bINCOMPLETE\b", after)):
            status = STATUS_INCOMPLETE
        elif member_complete or redirected_ok:
            status = STATUS_REGISTERED
        else:
            status = STATUS_INCOMPLETE
        upsert_group(
            EMPIRE_STATE_PG_ID,
            name=EMPIRE_STATE_NAME,
            status=status,
            registration_completed_at=_utc() if status == STATUS_REGISTERED else None,
            last_submit_at=_utc(),
            confirmation_excerpt=after[:400],
            confirmation_url=url[:220],
            relevant=True,
            fill_errors=None,
        )
        return {
            "ok": status == STATUS_REGISTERED,
            "status": status,
            "submitted": True,
            "confirmation_excerpt": after[:400],
            "confirmation_url": url[:220],
            "failed_check": failed_check,
            "purchasing_group_id": EMPIRE_STATE_PG_ID,
        }
    except Exception as exc:
        upsert_group(
            EMPIRE_STATE_PG_ID,
            name=EMPIRE_STATE_NAME,
            status=STATUS_ERROR,
            last_error=type(exc).__name__,
        )
        return {"ok": False, "status": STATUS_ERROR, "error": type(exc).__name__}


def resolve_access_gate_registration(
    client: Any,
    *,
    html: str,
    page_url: str,
    allow_submit: bool = True,
    owner_approved_submit: bool = False,
) -> dict[str, Any]:
    """Called when detail access gate detected — prepare/execute PG registration if allowed."""
    gate = extract_pg_gate_from_html(html, page_url=page_url)
    if not gate:
        return {"ok": False, "status": "NO_PG_GATE"}
    pg_id = gate.get("purchasing_group_id") or EMPIRE_STATE_PG_ID
    upsert_group(
        pg_id,
        name=gate.get("name"),
        registration_required=True,
        status=STATUS_INCOMPLETE,
        fee_required=bool(gate.get("fee_mentioned")),
        last_gate_message=gate.get("message"),
        complete_form_url_path=re.sub(r"https?://[^/]+", "", gate.get("complete_form_url") or "")[:200],
        relevant=True,
    )
    if gate.get("fee_mentioned"):
        upsert_group(pg_id, status=STATUS_PAID_REQUIRED, owner_blocker_reason="Fee on gate/form")
        return {"ok": False, "status": STATUS_PAID_REQUIRED, "gate": gate}
    if is_registered(pg_id):
        return {"ok": True, "status": STATUS_REGISTERED, "gate": gate, "already_registered": True}

    if pg_id == EMPIRE_STATE_PG_ID or (gate.get("name") or "").lower().startswith("empire state"):
        return run_empire_state_registration(
            client,
            form_url=gate.get("complete_form_url"),
            allow_submit=allow_submit,
            owner_approved_submit=owner_approved_submit,
        )

    # Generic groups: inspect only until mapped
    if gate.get("complete_form_url"):
        inspected = inspect_group_form(client, form_url=gate["complete_form_url"])
        upsert_group(
            pg_id,
            name=gate.get("name"),
            status=STATUS_OWNER_APPROVAL_REQUIRED,
            owner_blocker_reason="Generic PG form not yet mapped for auto-fill — owner review required",
            missing_fields=inspected.get("fields") or [],
            form_field_count=len(inspected.get("fields") or []),
        )
        return {
            "ok": True,
            "status": STATUS_OWNER_APPROVAL_REQUIRED,
            "gate": gate,
            "inspected": {
                "ok": inspected.get("ok"),
                "fee_mentioned": inspected.get("fee_mentioned"),
                "attestation_mentioned": inspected.get("attestation_mentioned"),
                "member_status_incomplete": inspected.get("member_status_incomplete"),
            },
            "field_count": len(inspected.get("fields") or []),
        }
    return {"ok": False, "status": STATUS_ERROR, "gate": gate}
