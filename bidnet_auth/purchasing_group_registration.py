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
    note_blocked_opportunity,
    release_registration_lock,
    try_begin_registration,
    upsert_group,
)

log = logging.getLogger("govtracker.bidnet_pg_registration")

BUILD = "20261014-m3-bidnet-pg-registration-v2"
DEFAULT_SUPPLIER_ORG_ID = "9785456456"


def _bind_portal_membership(
    pg_id: str,
    *,
    status: str,
    name: str | None = None,
    opportunity_id: str | None = None,
) -> None:
    """Persist portal/account/PG mapping (no secrets). Reuses BidNet account."""
    try:
        from m3_portal_access_map import PORTAL_BIDNET, bind_membership, seed_bidnet_defaults

        seed_bidnet_defaults()
        bind_membership(
            PORTAL_BIDNET,
            str(pg_id),
            kind="purchasing_group",
            status=status,
            name=name,
            opportunity_id=opportunity_id,
        )
    except Exception:
        log.debug("portal access map bind failed", exc_info=True)

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


def inspect_group_form(client: Any, *, form_url: str, timeout_ms: int = 60_000) -> dict[str, Any]:
    """Open group-form/edit and list fields. Does not submit."""
    if not getattr(client, "is_authenticated", False) or getattr(client, "_page", None) is None:
        return {"ok": False, "error": "NOT_AUTHENTICATED"}
    if not form_url:
        return {"ok": False, "error": "FORM_URL_MISSING"}
    page = client._page
    # domcontentloaded only — networkidle hangs on BidNet analytics beacons
    try:
        page.goto(form_url, wait_until="domcontentloaded", timeout=max(30_000, int(timeout_ms)))
    except Exception as exc:
        return {
            "ok": False,
            "error": type(exc).__name__,
            "form_url": form_url[:220],
            "message": str(exc)[:160],
        }
    try:
        page.wait_for_load_state("load", timeout=8_000)
    except Exception:
        pass
    try:
        page.wait_for_timeout(800)
    except Exception:
        pass
    final = str(page.url or "")
    # Login / challenge redirects are not form pages
    if re.search(r"/login|/challenge|captcha", final, re.I):
        return {
            "ok": False,
            "error": "FORM_NAV_REDIRECT",
            "final_url": final[:220],
            "form_url": form_url[:220],
        }
    body = ""
    try:
        body = re.sub(r"\s+", " ", page.locator("body").inner_text() or "")[:4000]
    except Exception:
        pass
    try:
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
    except Exception as exc:
        return {
            "ok": False,
            "error": f"FIELD_SCAN:{type(exc).__name__}",
            "final_url": final[:220],
            "body_excerpt": body[:500],
        }
    fee = bool(re.search(r"\$\d|payment|credit card|purchase|subscription fee", body, re.I))
    attestation = bool(
        re.search(r"I (certify|attest|declare|agree)|under penalty|legally binding", body, re.I)
    )
    member_completed = bool(
        re.search(r"Member Since.{0,40}\bCOMPLETED\b", body, re.I)
        or re.search(r"Member Since.{0,40}\bCOMPLETE\b", body, re.I)
    ) and not re.search(r"Member Since.{0,40}\bINCOMPLETE\b", body, re.I)
    # Capture visible validation / Failed Check text for operators
    validation_errors = []
    for m in re.finditer(
        r"(Failed Check[^.]{0,120}|This field is required[^.]{0,80}|Please (?:enter|select|complete)[^.]{0,100})",
        body,
        re.I,
    ):
        validation_errors.append(m.group(0).strip()[:160])
    missing_required = [
        {"name": f.get("name"), "label": f.get("label")}
        for f in (fields or [])
        if isinstance(f, dict)
        and f.get("required")
        and not f.get("has_value")
        and str(f.get("type") or "") not in {"checkbox", "radio", "submit", "button"}
    ]
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
        "validation_errors": validation_errors[:8],
        "missing_required_fields": missing_required[:12],
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
                if not ok and name:
                    # Generic: pick radio by name — prefer id/value containing YES/NO
                    ok = bool(
                        page.evaluate(
                            """({name, val}) => {
                              const nodes = Array.from(document.querySelectorAll(
                                'input[type=radio][name=\"'+name+'\"]'
                              ));
                              if (!nodes.length) return false;
                              const want = String(val||'NO').toUpperCase();
                              let el = nodes.find(n => {
                                const id = (n.id||'') + ' ' + (n.value||'');
                                return id.toUpperCase().includes('.'+want) ||
                                       id.toUpperCase().endsWith(want) ||
                                       String(n.value||'').toUpperCase() === want;
                              });
                              if (!el) el = want === 'YES' ? nodes[0] : nodes[nodes.length-1];
                              el.click();
                              el.checked = true;
                              el.dispatchEvent(new Event('change', {bubbles: true}));
                              return !!el.checked;
                            }""",
                            {"name": name, "val": val},
                        )
                    )
                if not ok and name:
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
                filled = False
                for sel in ("#federalTaxPayerId", f"[name=\"{name}\"]", "input[name*='federalTax']"):
                    try:
                        loc = page.locator(sel).first
                        if loc.count() == 0:
                            continue
                        loc.fill(ein, timeout=3_000)
                        filled = True
                        break
                    except Exception:
                        continue
                applied.append(
                    {
                        "name": name,
                        "ok": filled,
                        "value": "REDACTED" if filled else None,
                        "error": None if filled else "EIN_FIELD_NOT_FOUND",
                    }
                )
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


def build_group_form_url(
    *,
    purchasing_group_id: str,
    solicitation_id: str | None = None,
    supplier_org_id: str | None = None,
) -> str | None:
    """Construct BidNet group-form/edit URL from known org + PG + solicitation ids."""
    pg = str(purchasing_group_id or "").strip()
    if not pg:
        return None
    org = str(supplier_org_id or DEFAULT_SUPPLIER_ORG_ID).strip() or DEFAULT_SUPPLIER_ORG_ID
    base = (
        f"https://www.bidnetdirect.com/private/supplier/{org}/purchasing-groups/"
        f"{pg}/group-form/edit"
    )
    sol = str(solicitation_id or "").strip()
    if sol and re.fullmatch(r"\d{6,}", sol):
        return f"{base}?accessedElementId={sol}&originalAction=SOLICITATION_OPEN"
    return base


def extract_solicitation_id_from_url(url: str) -> str | None:
    u = url or ""
    for pat in (
        r"/statewide/(\d{6,})",
        r"/solicitations/(?:public/)?(?:statewide/)?(\d{6,})",
        r"/private/solicitations/(\d{6,})",
        r"accessedElementId=(\d{6,})",
        r"/open-bids/(\d{6,})",
    ):
        m = re.search(pat, u, re.I)
        if m:
            return m.group(1)
    return None


def prepare_generic_pg_answers(
    fields: list[dict[str, Any]],
    answers: dict[str, Any] | None = None,
    *,
    purchasing_group_id: str,
    name: str | None = None,
) -> dict[str, Any]:
    """Build fill plan for arbitrary BidNet PG forms from verified company data only."""
    ans = answers or load_verified_company_answers()
    plan: dict[str, Any] = {
        "purchasing_group_id": purchasing_group_id,
        "name": name,
        "fills": [],
        "skip_fields": [],
        "missing_fields": [],
        "owner_required": False,
        "owner_reasons": [],
        "can_submit": False,
    }
    for f in fields or []:
        if not isinstance(f, dict):
            continue
        fname = str(f.get("name") or "")
        label = str(f.get("label") or "")
        ftype = str(f.get("type") or "").lower()
        blob = f"{fname} {label}".lower()
        if f.get("has_value") or f.get("checked"):
            plan["skip_fields"].append({"name": fname, "reason": "already_populated"})
            continue
        # Certification radios — answer NO unless verified held
        if ftype == "radio" and re.search(r"certifiedmbe|mbe|wbe|mwbe", blob):
            if "other" in blob or "dbe" in blob:
                plan["fills"].append(
                    {
                        "name": fname,
                        "strategy": "radio",
                        "value": ans["other_mbe_dbe"],
                        "reason": "verified_held_dbe" if ans["dbe_verified_held"] else "not_verified_held_answer_NO",
                    }
                )
            else:
                plan["fills"].append(
                    {
                        "name": fname,
                        "strategy": "radio",
                        "value": ans["certified_mbe"],
                        "reason": "verified_held_mbe" if ans["mbe_verified_held"] else "not_verified_held_answer_NO",
                    }
                )
            continue
        if ftype == "radio" and re.search(r"\bdbe\b|disadvantaged", blob):
            plan["fills"].append(
                {
                    "name": fname,
                    "strategy": "radio",
                    "value": ans["other_mbe_dbe"],
                    "reason": "verified_held_dbe" if ans["dbe_verified_held"] else "not_verified_held_answer_NO",
                }
            )
            continue
        # EIN / taxpayer id
        if re.search(r"federaltax|taxpayer|employer.?number|\bein\b|tax.?id", blob):
            if ans["ein_present"]:
                plan["fills"].append(
                    {
                        "name": fname,
                        "strategy": "text",
                        "value_from": "ein_secure_env",
                        "reason": "verified_ein_present",
                    }
                )
            else:
                plan["missing_fields"].append(
                    {
                        "name": fname,
                        "label": label[:160],
                        "reason": "EIN_NOT_IN_SECURE_CONFIG",
                    }
                )
                plan["owner_required"] = True
                plan["owner_reasons"].append("Missing verified Federal EIN in secure configuration")
            continue
        # Required unknown fields → owner
        if f.get("required") and ftype not in {"submit", "button", "checkbox"}:
            # Skip duplicate radios already handled by name grouping
            if ftype == "radio":
                continue
            plan["missing_fields"].append(
                {
                    "name": fname,
                    "label": label[:160],
                    "reason": "unmapped_required_field",
                }
            )
            plan["owner_required"] = True
            if "unmapped required fields" not in plan["owner_reasons"]:
                plan["owner_reasons"].append("unmapped required fields")
    plan["can_submit"] = not plan["owner_required"] and not plan["missing_fields"]
    plan["company_answers_meta"] = {
        "ein_present": ans["ein_present"],
        "certified_mbe": ans["certified_mbe"],
        "other_mbe_dbe": ans["other_mbe_dbe"],
        "legal_name_known": bool(ans.get("legal_name")),
        "held_certifications": ans.get("held_certifications") or [],
    }
    return plan


def run_purchasing_group_registration(
    client: Any,
    *,
    purchasing_group_id: str,
    form_url: str | None = None,
    name: str | None = None,
    solicitation_id: str | None = None,
    allow_submit: bool = True,
    owner_approved_submit: bool = False,
    opportunity_id: str | None = None,
) -> dict[str, Any]:
    """Group-level registration: lock → inspect → fill verified fields → save when eligible.

    Reuses the Empire State workflow for all purchasing groups. Never invents data.
    Never pays. Pauses for owner on missing EIN, attestations, or unmapped required fields.
    """
    pg_id = str(purchasing_group_id or "").strip()
    if not pg_id:
        return {"ok": False, "status": STATUS_ERROR, "error": "missing_purchasing_group_id"}
    if opportunity_id:
        note_blocked_opportunity(pg_id, opportunity_id)
    if is_registered(pg_id):
        _bind_portal_membership(
            pg_id, status=STATUS_REGISTERED, name=name, opportunity_id=opportunity_id
        )
        return {
            "ok": True,
            "status": STATUS_REGISTERED,
            "skipped": True,
            "already_registered": True,
            "reason": "already_registered",
            "purchasing_group_id": pg_id,
            "portal_account": "bidnet",
        }

    # Cross-check portal access map (survives tracker edge cases)
    try:
        from m3_portal_access_map import PORTAL_BIDNET, is_membership_registered

        if is_membership_registered(PORTAL_BIDNET, pg_id):
            upsert_group(pg_id, name=name, status=STATUS_REGISTERED, relevant=True)
            return {
                "ok": True,
                "status": STATUS_REGISTERED,
                "skipped": True,
                "already_registered": True,
                "reason": "portal_access_map_registered",
                "purchasing_group_id": pg_id,
                "portal_account": "bidnet",
            }
    except Exception:
        pass

    lock = try_begin_registration(pg_id, worker_token=f"reg-{pg_id}")
    if not lock.get("acquired"):
        if lock.get("already_registered"):
            _bind_portal_membership(
                pg_id, status=STATUS_REGISTERED, name=name, opportunity_id=opportunity_id
            )
            return {
                "ok": True,
                "status": STATUS_REGISTERED,
                "skipped": True,
                "already_registered": True,
                "purchasing_group_id": pg_id,
                "portal_account": "bidnet",
            }
        return {
            "ok": False,
            "status": lock.get("status") or STATUS_INCOMPLETE,
            "skipped": True,
            "reason": lock.get("reason"),
            "owner_blocker_reason": lock.get("owner_blocker_reason"),
            "purchasing_group_id": pg_id,
            "portal_account": "bidnet",
        }

    answers = load_verified_company_answers()
    # Empire-specific field plan when applicable; otherwise built after inspect
    empire = pg_id == EMPIRE_STATE_PG_ID or (name or "").lower().startswith("empire state")
    plan = prepare_empire_state_answers(answers) if empire else None

    if not form_url:
        form_url = build_group_form_url(
            purchasing_group_id=pg_id, solicitation_id=solicitation_id
        )
    if not form_url:
        release_registration_lock(pg_id, worker_token=f"reg-{pg_id}", final_status=STATUS_ERROR)
        return {
            "ok": False,
            "status": STATUS_ERROR,
            "error": "FORM_URL_MISSING",
            "purchasing_group_id": pg_id,
        }

    try:
        inspected = inspect_group_form(client, form_url=form_url)
        if not inspected.get("ok"):
            upsert_group(
                pg_id,
                name=name,
                status=STATUS_ERROR,
                last_error=inspected.get("error"),
                relevant=True,
            )
            release_registration_lock(pg_id, worker_token=f"reg-{pg_id}", final_status=STATUS_ERROR)
            return {"ok": False, **inspected, "purchasing_group_id": pg_id}

        if inspected.get("member_status_completed"):
            upsert_group(
                pg_id,
                name=name or (EMPIRE_STATE_NAME if empire else None),
                status=STATUS_REGISTERED,
                registration_completed_at=_utc(),
                registration_required=True,
                fee_required=False,
                relevant=True,
                confirmation_excerpt=(inspected.get("body_excerpt") or "")[:400],
                form_url_path=re.sub(r"https?://[^/]+", "", form_url)[:200],
                portal_account="bidnet",
            )
            release_registration_lock(
                pg_id, worker_token=f"reg-{pg_id}", final_status=STATUS_REGISTERED
            )
            _bind_portal_membership(
                pg_id,
                status=STATUS_REGISTERED,
                name=name or (EMPIRE_STATE_NAME if empire else None),
                opportunity_id=opportunity_id,
            )
            return {
                "ok": True,
                "status": STATUS_REGISTERED,
                "skipped": True,
                "already_registered": True,
                "reason": "bidnet_form_already_completed",
                "purchasing_group_id": pg_id,
                "portal_account": "bidnet",
            }

        if inspected.get("fee_mentioned"):
            upsert_group(
                pg_id,
                name=name,
                status=STATUS_PAID_REQUIRED,
                owner_blocker_reason="Fee mentioned on purchasing-group form — owner authorization required",
                form_url_path=re.sub(r"https?://[^/]+", "", form_url)[:200],
                relevant=True,
            )
            release_registration_lock(
                pg_id, worker_token=f"reg-{pg_id}", final_status=STATUS_PAID_REQUIRED
            )
            return {
                "ok": False,
                "status": STATUS_PAID_REQUIRED,
                "blocker": "PAID_ACTION_REQUIRES_OWNER",
                "purchasing_group_id": pg_id,
            }

        if not plan:
            plan = prepare_generic_pg_answers(
                inspected.get("fields") or [],
                answers,
                purchasing_group_id=pg_id,
                name=name,
            )

        fill_result = apply_fills(client, plan, answers=answers)

        if plan.get("owner_required") or not plan.get("can_submit"):
            upsert_group(
                pg_id,
                name=name,
                status=STATUS_OWNER_APPROVAL_REQUIRED,
                registration_required=True,
                fee_required=False,
                owner_blocker_reason="; ".join(plan.get("owner_reasons") or []) or "missing_fields",
                missing_fields=plan.get("missing_fields"),
                prepared_fills=[
                    {k: v for k, v in f.items() if k != "value"} for f in plan.get("fills") or []
                ],
                form_url_path=re.sub(r"https?://[^/]+", "", form_url)[:200],
                last_inspection_at=_utc(),
                relevant=True,
            )
            release_registration_lock(
                pg_id,
                worker_token=f"reg-{pg_id}",
                final_status=STATUS_OWNER_APPROVAL_REQUIRED,
            )
            return {
                "ok": True,
                "status": STATUS_OWNER_APPROVAL_REQUIRED,
                "submitted": False,
                "populated": fill_result,
                "plan": {k: v for k, v in plan.items() if k != "company_answers_meta"}
                | {"company_answers_meta": plan.get("company_answers_meta")},
                "owner_next_steps": [
                    "Provide Federal EIN via COMPANY_EIN in Railway secure env if missing",
                    "Review unmapped required fields in purchasing-group UI",
                    "Approve submit only for free administrative forms",
                ],
                "purchasing_group_id": pg_id,
            }

        import os

        auto = (os.environ.get("M3_BIDNET_PG_AUTO_SUBMIT") or "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        tracked = get_group(pg_id) or {}
        owner_approved_submit = bool(owner_approved_submit or tracked.get("owner_approved_submit"))
        attestation = bool(inspected.get("attestation_mentioned"))
        free_admin_ok = bool(
            plan.get("can_submit") and not attestation and not inspected.get("fee_mentioned")
        )
        authorized = bool(allow_submit and (owner_approved_submit or auto or free_admin_ok))
        if not authorized:
            st = STATUS_PREPARED if not attestation else STATUS_OWNER_APPROVAL_REQUIRED
            upsert_group(
                pg_id,
                name=name,
                status=st,
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
            release_registration_lock(pg_id, worker_token=f"reg-{pg_id}", final_status=st)
            return {
                "ok": True,
                "status": st,
                "submitted": False,
                "populated": fill_result,
                "attestation_mentioned": attestation,
                "purchasing_group_id": pg_id,
            }

        bad = [a for a in (fill_result.get("applied") or []) if not a.get("ok")]
        if bad:
            upsert_group(
                pg_id,
                name=name,
                status=STATUS_ERROR,
                last_error="FILL_FAILED",
                fill_errors=bad,
                relevant=True,
            )
            release_registration_lock(pg_id, worker_token=f"reg-{pg_id}", final_status=STATUS_ERROR)
            return {
                "ok": False,
                "status": STATUS_ERROR,
                "submitted": False,
                "error": "FILL_FAILED",
                "populated": fill_result,
                "purchasing_group_id": pg_id,
            }

        client._page.locator("#saveButton").click(timeout=5_000)
        client._page.wait_for_timeout(2_500)
        url = str(client._page.url or "")
        after = re.sub(r"\s+", " ", client._page.locator("body").inner_text() or "")[:2000]
        failed_check = bool(re.search(r"Failed Check", after, re.I))
        still_on_edit = "group-form/edit" in url
        member_complete = bool(
            re.search(r"Member Since.{0,40}\bCOMPLETE\b", after, re.I)
        ) and not re.search(r"Member Since.{0,40}\bINCOMPLETE\b", after, re.I)
        redirected_ok = (not still_on_edit) and (not failed_check) and (
            "group-form" in url or "solicitations" in url or "search" in url
        )
        # Re-inspect after Save — submitted ≠ registered
        post = inspect_group_form(client, form_url=form_url, timeout_ms=45_000)
        if post.get("ok") and post.get("member_status_completed"):
            member_complete = True
            after = str(post.get("body_excerpt") or after)
            url = str(post.get("final_url") or url)
        validation_errors = list(post.get("validation_errors") or [])
        missing_required = list(post.get("missing_required_fields") or [])
        if failed_check or (still_on_edit and re.search(r"\bINCOMPLETE\b", after)):
            status = STATUS_INCOMPLETE
        elif member_complete or (redirected_ok and not post.get("member_status_incomplete")):
            status = STATUS_REGISTERED
        else:
            status = STATUS_INCOMPLETE
        owner_reason = None
        if status == STATUS_INCOMPLETE:
            parts = []
            if failed_check:
                parts.append("Failed Check after Save")
            if validation_errors:
                parts.append("; ".join(validation_errors[:3]))
            if missing_required:
                parts.append(
                    "missing: "
                    + ", ".join(
                        str(m.get("label") or m.get("name") or "?")[:40]
                        for m in missing_required[:5]
                    )
                )
            if post.get("attestation_mentioned"):
                parts.append("attestation language present — owner approval required")
                status = STATUS_OWNER_APPROVAL_REQUIRED
            owner_reason = "; ".join(parts) if parts else "Member status still INCOMPLETE after Save"
        upsert_group(
            pg_id,
            name=name or (EMPIRE_STATE_NAME if empire else None),
            status=status,
            registration_completed_at=_utc() if status == STATUS_REGISTERED else None,
            last_submit_at=_utc(),
            confirmation_excerpt=after[:400],
            confirmation_url=url[:220],
            relevant=True,
            fill_errors=None,
            portal_account="bidnet",
            owner_blocker_reason=owner_reason,
            missing_fields=missing_required or None,
            validation_errors=validation_errors or None,
        )
        release_registration_lock(pg_id, worker_token=f"reg-{pg_id}", final_status=status)
        if status == STATUS_REGISTERED:
            _bind_portal_membership(
                pg_id,
                status=status,
                name=name or (EMPIRE_STATE_NAME if empire else None),
                opportunity_id=opportunity_id,
            )
        return {
            "ok": status == STATUS_REGISTERED,
            "status": status,
            "submitted": True,
            "confirmation_excerpt": after[:400],
            "confirmation_url": url[:220],
            "validation_errors": validation_errors,
            "missing_required_fields": missing_required,
            "owner_blocker_reason": owner_reason,
            "purchasing_group_id": pg_id,
            "portal_account": "bidnet",
        }
    except Exception as exc:
        upsert_group(pg_id, name=name, status=STATUS_ERROR, last_error=type(exc).__name__, relevant=True)
        release_registration_lock(pg_id, worker_token=f"reg-{pg_id}", final_status=STATUS_ERROR)
        return {
            "ok": False,
            "status": STATUS_ERROR,
            "error": type(exc).__name__,
            "purchasing_group_id": pg_id,
        }


def resolve_access_gate_registration(
    client: Any,
    *,
    html: str,
    page_url: str,
    allow_submit: bool = True,
    owner_approved_submit: bool = False,
    opportunity_id: str | None = None,
    source_url: str | None = None,
) -> dict[str, Any]:
    """Called when detail access gate detected — prepare/execute PG registration if allowed."""
    gate = extract_pg_gate_from_html(html, page_url=page_url) or {}
    pg_id = (
        gate.get("purchasing_group_id")
        or extract_pg_id_from_url(page_url)
        or extract_pg_id_from_url(source_url or "")
    )
    if not gate and not pg_id:
        return {"ok": False, "status": "NO_PG_GATE"}
    if not gate:
        gate = {
            "detected": True,
            "name": None,
            "purchasing_group_id": pg_id,
            "complete_form_url": None,
            "fee_mentioned": False,
            "gate_kind": "PURCHASING_GROUP_REGISTRATION_INCOMPLETE",
            "message": "purchasingGroupId present on solicitation URL",
        }
    pg_id = gate.get("purchasing_group_id") or pg_id
    sol_id = extract_solicitation_id_from_url(page_url) or extract_solicitation_id_from_url(
        source_url or ""
    )
    form_url = gate.get("complete_form_url") or build_group_form_url(
        purchasing_group_id=str(pg_id), solicitation_id=sol_id
    )
    gate["complete_form_url"] = form_url
    upsert_group(
        str(pg_id),
        name=gate.get("name"),
        registration_required=True,
        status=STATUS_INCOMPLETE,
        fee_required=bool(gate.get("fee_mentioned")),
        last_gate_message=gate.get("message"),
        complete_form_url_path=re.sub(r"https?://[^/]+", "", form_url or "")[:200],
        relevant=True,
    )
    if opportunity_id:
        note_blocked_opportunity(str(pg_id), opportunity_id)
    if gate.get("fee_mentioned"):
        upsert_group(str(pg_id), status=STATUS_PAID_REQUIRED, owner_blocker_reason="Fee on gate/form")
        return {"ok": False, "status": STATUS_PAID_REQUIRED, "gate": gate, "purchasing_group_id": pg_id}
    if is_registered(str(pg_id)):
        return {
            "ok": True,
            "status": STATUS_REGISTERED,
            "gate": gate,
            "already_registered": True,
            "purchasing_group_id": pg_id,
        }

    # Empire State keeps its dedicated entry for backward-compatible proofs; same engine underneath.
    if str(pg_id) == EMPIRE_STATE_PG_ID or (gate.get("name") or "").lower().startswith("empire state"):
        out = run_empire_state_registration(
            client,
            form_url=form_url,
            allow_submit=allow_submit,
            owner_approved_submit=owner_approved_submit,
            solicitation_id=sol_id or "9797265001",
        )
        out["gate"] = gate
        out["purchasing_group_id"] = out.get("purchasing_group_id") or pg_id
        return out

    out = run_purchasing_group_registration(
        client,
        purchasing_group_id=str(pg_id),
        form_url=form_url,
        name=gate.get("name"),
        solicitation_id=sol_id,
        allow_submit=allow_submit,
        owner_approved_submit=owner_approved_submit,
        opportunity_id=opportunity_id,
    )
    out["gate"] = gate
    return out
