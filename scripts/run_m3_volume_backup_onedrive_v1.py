"""Independent Railway volume backup → OneDrive via `railway volume files`.

Does not overwrite production. Does not store the only copy on the volume.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKUP_ROOT = Path(os.environ.get("M3_BACKUP_ROOT") or r"c:\Users\M_gra\OneDrive\Desktop\M3_Backups")
VOLUME_ID = os.environ.get("M3_RAILWAY_VOLUME_ID") or "d675e560-815b-418d-a50f-0f225ffb9eab"

# Critical remote paths (volume mount root = /data)
CRITICAL_FILES = [
    "/l23_canonical_population_store.json",
    "/m3_canonical_product_lines_v1.json",
    "/m3_canonical_product_lines_v1_index.json",
    "/supplier_profiles.json",
    "/BIDNET_BUYER_DIRECTORY.json",
    "/BUYER_PACKAGE_SOURCE_MEMORY.json",
]

CRITICAL_DIRS = [
    "/_og_commercial_research_pipeline_out",
]


def run(cmd: list[str], *, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)


def _railway_bin() -> list[str]:
    """Resolve railway CLI for Windows subprocess (ps1 wrappers are not executable)."""
    npm = Path.home() / "AppData/Roaming/npm"
    cmd = npm / "railway.cmd"
    if cmd.exists():
        return [str(cmd)]
    rjs = npm / "node_modules/@railway/cli/bin/railway.js"
    if rjs.exists():
        return ["node", str(rjs)]
    return ["railway"]


def volume_cmd(*args: str) -> list[str]:
    return [*_railway_bin(), "volume", "files", "--volume", VOLUME_ID, *args]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def list_remote(path: str = "/") -> dict:
    proc = run(volume_cmd("list", path, "--json"), timeout=120)
    if proc.returncode != 0:
        return {"ok": False, "stderr": (proc.stderr or "")[-2000:], "stdout": (proc.stdout or "")[-1000:]}
    # stdout may include selection noise before JSON
    text = proc.stdout or ""
    start = text.find("{")
    if start < 0:
        return {"ok": False, "raw": text[-2000:]}
    return {"ok": True, "data": json.loads(text[start:])}


def download_remote(remote: str, local: Path) -> dict:
    local.parent.mkdir(parents=True, exist_ok=True)
    if local.exists():
        local.unlink()
    t0 = time.time()
    proc = run(
        volume_cmd("download", remote, str(local), "--json", "--overwrite"),
        timeout=1800,
    )
    elapsed = round(time.time() - t0, 2)
    ok = proc.returncode == 0 and local.exists()
    meta = {
        "remote": remote,
        "local": str(local),
        "ok": ok,
        "rc": proc.returncode,
        "elapsed_s": elapsed,
        "stderr_tail": (proc.stderr or "")[-800:],
    }
    if ok:
        if local.is_file():
            meta["bytes"] = local.stat().st_size
            meta["sha256"] = sha256_file(local)
        else:
            # directory download
            files = [p for p in local.rglob("*") if p.is_file()]
            meta["file_count"] = len(files)
            meta["bytes"] = sum(p.stat().st_size for p in files)
    return meta


def backup_local_source(dest: Path) -> dict:
    src_tar = dest / "local_source_and_artifacts.tar.gz"
    include = [
        "buyer_intelligence",
        "discovery/sam_budgeted_client.py",
        "discovery/federal_sam_ingest.py",
        "discovery/dla_fallback.py",
        "discovery/federal_description_recovery.py",
        "openai_runtime.py",
        "sam_client.py",
        "sam_enrich.py",
        "phase_l/owner_ui_service.py",
        "scripts/run_m3_research_rotation_pilot_v1.py",
        "scripts/run_commercial_execution_queue_v1.py",
        "scripts/run_revenue_strategy_engine_v1.py",
        "scripts/live_stage1_once.py",
        "scripts/run_m3_volume_backup_onedrive_v1.py",
        "tests/test_commercial_budget_control_v1.py",
        "tests/test_research_novelty_engine_v1.py",
        "tests/test_revenue_strategy_engine_v1.py",
        "tests/test_product_line_store_v1.py",
        "data/_og_commercial_research_pipeline_out",
        "static/operator.js",
        "railway.toml",
        "Dockerfile",
        "scripts/start_railway.sh",
    ]
    exclude = {".env", ".env.local", "credentials.json"}
    n = 0
    with tarfile.open(src_tar, "w:gz") as tar:
        for rel in include:
            p = ROOT / rel
            if not p.exists():
                continue
            if p.is_file():
                if p.name in exclude:
                    continue
                tar.add(p, arcname=f"local/{rel}")
                n += 1
            else:
                for f in p.rglob("*"):
                    if not f.is_file() or f.name in exclude or "__pycache__" in f.parts:
                        continue
                    tar.add(f, arcname=f"local/{f.relative_to(ROOT).as_posix()}")
                    n += 1
    return {"path": str(src_tar), "files": n, "bytes": src_tar.stat().st_size, "sha256": sha256_file(src_tar)}


def capture_meta(dest: Path) -> dict:
    meta: dict = {"at": datetime.now(timezone.utc).isoformat(), "volume_id": VOLUME_ID}
    try:
        meta["git_head"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        meta["git_status"] = subprocess.check_output(["git", "status", "-sb"], cwd=ROOT, text=True)[:5000]
        meta["git_log"] = subprocess.check_output(["git", "log", "-5", "--oneline"], cwd=ROOT, text=True)
    except Exception as exc:
        meta["git_error"] = str(exc)
    try:
        st = run([*_railway_bin(), "status"], timeout=60)
        meta["railway_status"] = (st.stdout or "") + (st.stderr or "")
    except Exception as exc:
        meta["railway_status_error"] = str(exc)
    # env keys only
    env = ROOT / ".env"
    if env.exists():
        keys = []
        for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                keys.append(line.split("=", 1)[0].strip())
        meta["env_keys"] = keys
    (dest / "predeploy_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def try_pg_dump(dest: Path) -> dict:
    """Optional Postgres dump using local DATABASE_URL (production if .env points there)."""
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        return {"ok": False, "reason": "DATABASE_URL missing"}
    out = dest / "postgres_dump.sql.gz"
    # Use python zlib stream via psycopg if pg_dump absent
    try:
        proc = run(["pg_dump", "--version"], timeout=20)
        if proc.returncode != 0:
            raise RuntimeError("pg_dump missing")
        # Don't print URL
        env = os.environ.copy()
        env["PGPASSWORD"] = ""  # rely on URL
        p = subprocess.run(
            ["pg_dump", "--no-owner", "--no-acl", url],
            cwd=str(ROOT),
            capture_output=True,
            timeout=600,
        )
        if p.returncode != 0:
            return {"ok": False, "reason": "pg_dump_failed", "stderr_tail": (p.stderr or b"")[-500:].decode("utf-8", "replace")}
        import gzip

        out.write_bytes(gzip.compress(p.stdout))
        return {"ok": True, "path": str(out), "bytes": out.stat().st_size, "sha256": sha256_file(out)}
    except Exception as exc:
        return {"ok": False, "reason": type(exc).__name__, "detail": str(exc)[:200]}


def main() -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_ROOT / f"predeploy_{stamp}"
    data_dir = dest / "railway_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    report: dict = {
        "kind": "m3_volume_backup_onedrive_v1",
        "backup_dir": str(dest),
        "independent_of_railway_volume": True,
        "destination": "OneDrive Desktop M3_Backups",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    print(f"BACKUP_DIR={dest}")
    report["meta"] = capture_meta(dest)

    root_list = list_remote("/")
    (dest / "volume_root_listing.json").write_text(json.dumps(root_list, indent=2), encoding="utf-8")
    if not root_list.get("ok"):
        report["status"] = "BLOCKED_VOLUME_LIST"
        report["root_list"] = root_list
        (dest / "backup_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2)[:3000])
        return 2
    names = {f.get("name") for f in (root_list.get("data") or {}).get("files") or []}
    report["root_names_sample"] = sorted(names)[:80]
    print(f"Root entries: {len(names)}")

    downloads = []
    for remote in CRITICAL_FILES:
        name = remote.lstrip("/")
        if name not in names and not any(n == name for n in names):
            # still try — nested files may not be in root list
            pass
        local = data_dir / name
        print(f"Downloading {remote} ...")
        meta = download_remote(remote, local)
        downloads.append(meta)
        print(f"  -> ok={meta.get('ok')} bytes={meta.get('bytes')} sha={str(meta.get('sha256'))[:12]}")

    for remote in CRITICAL_DIRS:
        # check existence via list
        parent = "/"
        dname = remote.strip("/")
        if dname in names:
            local = data_dir / dname
            print(f"Downloading dir {remote} ...")
            meta = download_remote(remote, local)
            downloads.append(meta)
            print(f"  -> ok={meta.get('ok')} files={meta.get('file_count')} bytes={meta.get('bytes')}")
        else:
            # list for m3_* commercial artifacts at root
            downloads.append({"remote": remote, "ok": False, "reason": "dir_not_in_root"})

    # Also download all root-level m3_*.json (bounded)
    m3_root = [
        f
        for f in (root_list.get("data") or {}).get("files") or []
        if f.get("type") == "file" and str(f.get("name") or "").startswith("m3_") and int(f.get("size") or 0) < 80_000_000
    ]
    for f in sorted(m3_root, key=lambda x: -int(x.get("size") or 0))[:60]:
        remote = f["path"]
        local = data_dir / f["name"]
        if local.exists():
            continue
        print(f"Downloading {remote} ...")
        downloads.append(download_remote(remote, local))

    report["downloads"] = downloads
    ok_n = sum(1 for d in downloads if d.get("ok"))
    fail_n = sum(1 for d in downloads if not d.get("ok"))
    report["download_summary"] = {"ok": ok_n, "failed": fail_n}

    # Integrity: require l23 present
    l23 = data_dir / "l23_canonical_population_store.json"
    if not l23.exists() or l23.stat().st_size < 1000:
        report["status"] = "FAIL_MISSING_L23"
        (dest / "backup_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print("CRITICAL: l23 store missing from backup")
        return 3

    # Restore smoke: load JSON header of l23
    try:
        with l23.open("r", encoding="utf-8") as fh:
            head = fh.read(5000)
        report["l23_smoke"] = {
            "bytes": l23.stat().st_size,
            "sha256": sha256_file(l23),
            "starts_with_brace": head.lstrip().startswith("{"),
            "has_opportunities_key": '"opportunities"' in head or "'opportunities'" in head,
        }
        # count opportunities lightly
        blob = json.loads(l23.read_text(encoding="utf-8"))
        opps = blob.get("opportunities") or {}
        report["l23_smoke"]["opportunity_count"] = len(opps)
        report["l23_smoke"]["store_keys"] = list(blob.keys())[:30]
    except Exception as exc:
        report["status"] = "FAIL_L23_PARSE"
        report["l23_error"] = str(exc)[:300]
        (dest / "backup_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        return 4

    print("Backing up local source ...")
    report["local_source_backup"] = backup_local_source(dest)
    print("Attempting postgres dump ...")
    report["postgres_dump"] = try_pg_dump(dest)

    report["restore_procedure"] = [
        "1. Keep this OneDrive folder as the independent restore source",
        "2. Code rollback: railway deployment list + redeploy prior ID, or git revert + push",
        "3. Data restore (selective): railway volume files upload <local> <remote> --overwrite ONLY after diff review",
        "4. Never overwrite newer valid product-line records without merge",
        "5. Re-verify /api/m3/health and opportunity counts after restore",
    ]
    report["status"] = "BACKUP_OK" if ok_n >= 1 and report.get("l23_smoke", {}).get("opportunity_count", 0) > 0 else "BACKUP_PARTIAL"
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    (dest / "backup_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    art = ROOT / "data" / "_og_commercial_research_pipeline_out" / "m3_predeploy_backup_report_v1.json"
    art.parent.mkdir(parents=True, exist_ok=True)
    art.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report["status"],
                "backup_dir": report["backup_dir"],
                "download_summary": report["download_summary"],
                "l23_smoke": report.get("l23_smoke"),
                "local_source_backup": {k: report["local_source_backup"].get(k) for k in ("files", "bytes", "sha256")},
                "postgres_dump": report.get("postgres_dump"),
            },
            indent=2,
        )
    )
    return 0 if report["status"] in {"BACKUP_OK", "BACKUP_PARTIAL"} and report.get("l23_smoke", {}).get("opportunity_count") else 5


if __name__ == "__main__":
    raise SystemExit(main())
