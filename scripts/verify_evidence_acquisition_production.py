"""Live production evidence acquisition verification against real deferred backlog."""
from __future__ import annotations

import json
import os
import time
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if not (ROOT / ".env").exists() and (ROOT.parent / ".env").exists():
    ROOT = ROOT.parent
BASE = os.environ.get("GOVTRACKER_BASE_URL") or "https://samgov-production.up.railway.app"
OUT = ROOT / "artifacts" / "m3_evidence_acquisition_verification.json"


def load_dotenv():
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        key, val = k.strip(), v.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        os.environ.setdefault(key, val)


def main():
    load_dotenv()
    email = (os.environ.get("APP_EMAIL") or "").strip()
    password = (os.environ.get("APP_PASSWORD") or "").strip()
    if not email or not password:
        raise SystemExit("APP_EMAIL/APP_PASSWORD required")

    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def req(method: str, path: str, body: dict | None = None):
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
        with opener.open(request, timeout=180) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}

    report: dict = {"kind": "M3EvidenceAcquisitionVerification", "base": BASE, "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    req("POST", "/api/login", {"email": email, "password": password})
    _, health = req("GET", "/api/m3/health")
    report["build_version"] = health.get("build_version")
    print("build", report["build_version"], flush=True)

    _, before = req("GET", "/api/m3/evidence/status")
    report["before"] = before
    print("before deferred", before.get("deferred"), "public", before.get("public_recovery_candidates"), flush=True)

    _, trigger = req("POST", "/api/m3/evidence/acquire", {"allow_paid": True})
    report["trigger"] = trigger
    print("trigger cleared", trigger.get("cleared_for_evidence_pass"), "run", (trigger.get("research_run") or {}).get("run_id"), flush=True)

    samples = []
    final_research = {}
    for i in range(72):  # up to ~6 minutes
        time.sleep(5)
        _, rst = req("GET", "/api/m3/research/status")
        _, est = req("GET", "/api/m3/evidence/status")
        sample = {
            "t": i,
            "research_status": rst.get("status"),
            "running": rst.get("running"),
            "processed": (rst.get("display") or {}).get("processed"),
            "rejected": (rst.get("display") or {}).get("rejected"),
            "advanced": (rst.get("display") or {}).get("advanced"),
            "deferred": (rst.get("display") or {}).get("deferred"),
            "currently": (rst.get("display") or {}).get("currently_processing_title"),
            "spend": (rst.get("display") or {}).get("actual_external_spend"),
            "evidence_deferred": est.get("deferred"),
            "packages_recovered": est.get("packages_recovered"),
            "auth_blocked": est.get("auth_registration_blocked"),
            "web_pending": est.get("web_research_pending"),
            "reasons": est.get("primary_reason_counts"),
        }
        samples.append(sample)
        final_research = rst
        print(
            f"[{i}] res={sample['research_status']} proc={sample['processed']} rej={sample['rejected']} "
            f"adv={sample['advanced']} pkg={sample['packages_recovered']} auth={sample['auth_blocked']} current={sample['currently']}",
            flush=True,
        )
        if not rst.get("running") and i >= 2 and any(s.get("running") or (s.get("processed") or 0) > 0 for s in samples):
            break

    _, after = req("GET", "/api/m3/evidence/status")
    _, dash = req("GET", "/api/m3/mobile/dashboard")
    _, pipe = req("GET", "/api/m3/pipeline/status")
    rows = pipe.get("opportunities") or []
    from collections import Counter

    lc = Counter(r.get("lifecycle") for r in rows)
    report["samples"] = samples
    report["after"] = after
    report["dashboard_evidence"] = dash.get("evidence")
    report["lifecycle_counts"] = dict(lc)
    disp = (final_research.get("display") or {})
    last = final_research.get("last_successful_completion") or final_research.get("current_run") or {}
    report["summary"] = {
        "build_version": report["build_version"],
        "starting_deferred": before.get("deferred"),
        "ending_deferred": after.get("deferred"),
        "packages_recovered": after.get("packages_recovered"),
        "auth_registration_blocked": after.get("auth_registration_blocked"),
        "web_research_pending": after.get("web_research_pending"),
        "public_recovery_candidates": after.get("public_recovery_candidates"),
        "genuinely_unresolved": after.get("genuinely_unresolved"),
        "research_processed": disp.get("processed") or last.get("processed"),
        "research_rejected": disp.get("rejected") or last.get("rejected"),
        "research_advanced": disp.get("advanced") or last.get("advanced"),
        "research_deferred": disp.get("deferred") or last.get("deferred"),
        "actual_external_spend": disp.get("actual_external_spend") or last.get("actual_external_spend"),
        "primary_reason_counts": after.get("primary_reason_counts"),
        "lifecycle_counts": dict(lc),
        "improved": (before.get("deferred") or 0) > (after.get("deferred") or 0)
        or (after.get("packages_recovered") or 0) > 0
        or (disp.get("rejected") or last.get("rejected") or 0) > 0
        or (disp.get("advanced") or last.get("advanced") or 0) > 0,
    }
    report["LIVE_OK"] = bool(
        report["build_version"]
        and "evidence" in str(report["build_version"])
        and report["summary"]["improved"]
    )
    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report["summary"], indent=2, default=str), flush=True)
    print(f"LIVE_OK={report['LIVE_OK']} wrote {OUT}", flush=True)
    return 0 if report["LIVE_OK"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
