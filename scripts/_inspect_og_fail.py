from __future__ import annotations
import json, os, time
from pathlib import Path
import httpx
ROOT = Path(__file__).resolve().parents[1]
BASE = "https://samgov-production.up.railway.app"

def main():
    for line in (ROOT/".env").read_text(encoding="utf-8").splitlines():
        line=line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k,_,v=line.partition("=")
        k=k.strip()
        if k and k not in os.environ: os.environ[k]=v.strip().strip('"').strip("'")
    c=httpx.Client(base_url=BASE, timeout=60, follow_redirects=True)
    c.post("/api/login", json={"email":os.environ["APP_EMAIL"],"password":os.environ["APP_PASSWORD"]}).raise_for_status()
    d=c.get("/api/m3/auth-jobs/OGD-a48ae0412f91").json()
    print(json.dumps({k:d.get(k) for k in ("status","error","result","progress")}, indent=2, default=str)[:2000])

if __name__=="__main__":
    main()
