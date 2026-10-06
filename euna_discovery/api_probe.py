"""Probe authenticated Euna vendor API shape — no secrets, truncated keys only."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

log = logging.getLogger("govtracker.euna_discovery.api_probe")


def _summarize(obj: Any, *, depth: int = 0) -> Any:
    if depth > 3:
        return type(obj).__name__
    if isinstance(obj, dict):
        out = {}
        for i, (k, v) in enumerate(obj.items()):
            if i >= 40:
                out["…"] = f"+{len(obj) - 40} keys"
                break
            if re.search(r"email|phone|token|password|secret|address", str(k), re.I):
                out[str(k)] = "[REDACTED]"
            else:
                out[str(k)] = _summarize(v, depth=depth + 1)
        return out
    if isinstance(obj, list):
        if not obj:
            return []
        return [_summarize(obj[0], depth=depth + 1), f"len={len(obj)}"]
    if isinstance(obj, str):
        return obj[:80]
    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    return type(obj).__name__


def run_euna_api_probe() -> dict[str, Any]:
    from application_clock import now_utc
    from euna_auth.client import EunaAuthenticatedClient
    from m3_data_root import data_path

    report: dict[str, Any] = {
        "kind": "EunaApiProbe",
        "probe_version": "v3g",
        "started_at": now_utc().isoformat(),
    }
    xhr: list[str] = []

    with EunaAuthenticatedClient() as client:
        auth = client.ensure_authenticated()
        report["auth"] = {
            "status": auth.status,
            "authenticated": auth.authenticated,
            "failure_reason": auth.failure_reason,
            "auth_host": auth.auth_host,
        }
        if not auth.authenticated or client.page is None:
            report["error"] = "not_authenticated"
            return report
        page = client.page

        def _on_response(response: Any) -> None:
            try:
                u = response.url or ""
                if response.status == 200 and (
                    "api" in u.lower() or "opportunit" in u.lower() or "project" in u.lower()
                ):
                    if u not in xhr and len(xhr) < 80:
                        xhr.append(u)
            except Exception:
                return

        page.on("response", _on_response)

        # Safe anchors (observed working without Cloudflare interstitial)
        for anchor in (
            "https://vendor.bonfirehub.com/agencies",
            "https://vendor.bonfirehub.com/dashboard",
        ):
            try:
                page.goto(anchor, wait_until="domcontentloaded", timeout=45_000)
                page.wait_for_timeout(1500)
                text = (page.inner_text("body") or "")[:200].lower()
                if "performing security verification" not in text:
                    report["anchor_url"] = page.url
                    break
            except Exception as exc:
                report["anchor_error"] = type(exc).__name__

        # Entitlement gate detection from My Network copy
        try:
            body0 = (page.inner_text("body") or "")
            report["anchor_text_sample"] = body0[:600]
            low0 = body0.lower()
            if re.search(
                r"sign\s+up\s+for\s+euna\s+supplier\s+network|no\s+agencies\s+available",
                low0,
            ):
                report["account_entitlement"] = "SUPPLIER_NETWORK_NOT_ENABLED"
        except Exception:
            pass

        # vendors/me summary + entitlement-ish fields
        try:
            me_payload = page.evaluate(
                """async () => {
                  const r = await fetch('https://common-production-api-global.bonfirehub.com/v1.0/vendors/me', {credentials:'include'});
                  const j = await r.json();
                  return {status: r.status, body: j};
                }"""
            )
            report["vendors_me_status"] = me_payload.get("status")
            report["vendors_me_summary"] = _summarize(me_payload.get("body"))
            body = me_payload.get("body") or {}
            # Response may be flat vendor object or {data: {...}}
            data = body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), dict) else body
            if isinstance(data, dict):
                report["vendor_flags"] = {
                    k: data.get(k)
                    for k in (
                        "id",
                        "isOnboarded",
                        "isPremium",
                        "isPro",
                        "hasSupplierNetwork",
                        "supplierNetworkEnabled",
                        "subscriptionTier",
                        "plan",
                        "tier",
                        "features",
                        "isJoinRequestsEnabled",
                    )
                    if k in data
                }
            # Feature flags (observed XHR on /agencies) — entitlement signal
            vendor_id = data.get("id") if isinstance(data, dict) else None
            ff = page.evaluate(
                """async (vendorId) => {
                  const who = await fetch('https://common-production-api-global.bonfirehub.com/v1.0/authn/whoami', {credentials:'include'});
                  const wj = await who.json();
                  const uid = wj && (wj.id || wj.userId || (wj.data && (wj.data.id || wj.data.userId)));
                  if (!uid) return {status: who.status, error: 'no_user_id', whoami_keys: Object.keys(wj||{}).slice(0,25)};
                  const q = vendorId ? ('?attributes=' + encodeURIComponent(JSON.stringify({globalVendorId: vendorId}))) : '';
                  const r = await fetch('https://common-production-api-global.bonfirehub.com/v1.0/users/' + uid + '/featureFlags' + q, {credentials:'include'});
                  let body = null;
                  try { body = await r.json(); } catch (e) { body = null; }
                  return {status: r.status, body};
                }""",
                vendor_id,
            )
            report["feature_flags_status"] = ff.get("status") if isinstance(ff, dict) else None
            report["feature_flags_summary"] = _summarize(ff.get("body") if isinstance(ff, dict) else None)
        except Exception as exc:
            report["vendors_me_error"] = type(exc).__name__

        # Probe candidate opportunity list endpoints (status + key shape only)
        api_paths = [
            "/v1.0/vendors/me/opportunities",
            "/v1.0/vendors/me/projects",
            "/v1.0/vendors/me/recommended-opportunities",
            "/v1.0/opportunities",
            "/v1.0/opportunities/search",
            "/v1.0/opportunities/open",
            "/v1.0/projects/search",
            "/v1.0/supplier-network/opportunities",
            "/v1.0/esn/opportunities",
            "/v1.0/vendors/me/agencies?region=us",
        ]
        try:
            probe = page.evaluate(
                """async (paths) => {
                  const base = 'https://common-production-api-global.bonfirehub.com';
                  const out = [];
                  for (const p of paths) {
                    try {
                      const r = await fetch(base + p, {credentials:'include'});
                      let keys = null, total = null, len = null;
                      const ct = (r.headers.get('content-type') || '');
                      if (ct.includes('json')) {
                        const j = await r.json();
                        const d = j && (j.data !== undefined ? j.data : j);
                        if (d && typeof d === 'object' && !Array.isArray(d)) {
                          keys = Object.keys(d).slice(0, 20);
                          total = d.total ?? d.count ?? d.reportedTotal ?? null;
                          const arr = d.items || d.results || d.opportunities || d.projects || d.rows;
                          len = Array.isArray(arr) ? arr.length : null;
                        } else if (Array.isArray(d)) {
                          len = d.length;
                          keys = d[0] ? Object.keys(d[0]).slice(0, 15) : [];
                        }
                      }
                      out.push({path: p, status: r.status, keys, total, len});
                    } catch (e) {
                      out.push({path: p, status: 'error', error: String(e).slice(0,80)});
                    }
                  }
                  return out;
                }""",
                api_paths,
            )
            report["api_path_probe"] = probe
        except Exception as exc:
            report["api_path_probe_error"] = type(exc).__name__

        # Click Opportunities / Browse and capture network (SPA nav may avoid full CF)
        for name in (r"^Opportunities$", r"opportunit", r"browse", r"recommend", r"search"):
            try:
                link = page.get_by_role("link", name=re.compile(name, re.I)).first
                if link.count() == 0:
                    continue
                link.click(timeout=8_000)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=20_000)
                    page.wait_for_timeout(3500)
                except Exception:
                    pass
                body_t = (page.inner_text("body") or "")
                report.setdefault("nav_clicks", []).append(
                    {
                        "name": name,
                        "url": page.url,
                        "cf": "security verification" in body_t.lower(),
                        "esn_signup": bool(
                            re.search(r"sign\s+up\s+for\s+euna\s+supplier\s+network", body_t, re.I)
                        ),
                        "text_sample": body_t[:300],
                    }
                )
                # Stay on first Opportunities hit for XHR capture
                if "opportunit" in name.lower() or "opportunit" in (page.url or "").lower():
                    break
            except Exception:
                continue

        report["xhr_urls"] = xhr[:60]
        report["final_url"] = page.url
        try:
            report["final_text_sample"] = (page.inner_text("body") or "")[:600]
        except Exception:
            pass
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            pass

    report["completed_at"] = now_utc().isoformat()
    try:
        path = data_path("euna_auth/last_api_probe.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        report["report_path"] = str(path)
    except Exception:
        pass
    return report
