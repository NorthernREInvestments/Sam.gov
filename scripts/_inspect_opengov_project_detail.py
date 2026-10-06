"""Inspect GET /api/v1/project/{id} attachment fields."""
from __future__ import annotations

import json
import re
from pathlib import Path

import httpx

OUT = Path(__file__).resolve().parents[1] / "data" / "opengov_project_303241_detail.json"
API = "https://api.procurement.opengov.com/api/v1"
H = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Origin": "https://procurement.opengov.com",
    "Referer": "https://procurement.opengov.com/portal/santacruzca/projects/303241",
}


def main() -> int:
    with httpx.Client(timeout=40.0, follow_redirects=True, headers=H) as c:
        r = c.get(f"{API}/project/303241")
        print("status", r.status_code)
        j = r.json()
        OUT.write_text(json.dumps(j, indent=2, default=str), encoding="utf-8")
        print("keys", sorted(j.keys()))
        for k in (
            "attachments",
            "documentAttachment",
            "addendums",
            "document_attachment_id",
            "files",
            "documents",
        ):
            v = j.get(k)
            print("===", k, type(v).__name__, "===")
            print(json.dumps(v, indent=2, default=str)[:3000])
        urls = sorted(set(re.findall(r"https?://[^\s\"']+", json.dumps(j, default=str))))
        print("URLS", len(urls))
        for u in urls[:50]:
            print(u[:220])
        # Probe documentAttachment id if present
        da = j.get("documentAttachment") or {}
        da_id = j.get("document_attachment_id") or (da.get("id") if isinstance(da, dict) else None)
        atts = j.get("attachments") or []
        print("n_attachments", len(atts) if isinstance(atts, list) else None, "doc_attachment_id", da_id)
        if da_id:
            for path in (
                f"{API}/attachment/{da_id}",
                f"{API}/documentAttachment/{da_id}",
                f"{API}/document-attachment/{da_id}",
                f"{API}/file/{da_id}",
                f"{API}/download/{da_id}",
            ):
                rr = c.get(path)
                print("probe", rr.status_code, path, (rr.headers.get("content-type") or "")[:40], len(rr.content or b""))
        if isinstance(atts, list):
            for a in atts[:5]:
                if not isinstance(a, dict):
                    continue
                print("att keys", sorted(a.keys()))
                print(json.dumps(a, indent=2, default=str)[:800])
                aid = a.get("id") or a.get("attachmentId")
                if aid:
                    for path in (
                        f"{API}/attachment/{aid}",
                        f"{API}/attachments/{aid}",
                        f"{API}/file/{aid}",
                        f"{API}/project/303241/attachment/{aid}",
                    ):
                        rr = c.get(path)
                        print(
                            " attprobe",
                            rr.status_code,
                            path,
                            (rr.headers.get("content-type") or "")[:50],
                            len(rr.content or b""),
                        )
    print("wrote", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
