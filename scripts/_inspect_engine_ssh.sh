#!/bin/bash
set -e
echo "===PS_CHROME==="
ps aux 2>/dev/null | egrep -i 'chrome|chromium|playwright|uvicorn|gunicorn' | head -40 || true
echo "===PS_COUNT==="
ps aux 2>/dev/null | wc -l || true
pgrep -af chrome 2>/dev/null | wc -l || true
pgrep -af chromium 2>/dev/null | wc -l || true
pgrep -af playwright 2>/dev/null | wc -l || true
echo "===MEM==="
free -h 2>/dev/null || head -8 /proc/meminfo 2>/dev/null || true
echo "===LOAD==="
cat /proc/loadavg 2>/dev/null || true
echo "===DF==="
df -h /data 2>/dev/null | tail -1 || true
echo "===FILES==="
ls -la /data/m3_bidnet_engine_v1_progress.json /data/m3_bidnet_engine_v1_last_report.json /data/m3_bidnet_downstream_v1_checkpoint.json /data/m3_bidnet_engine_v1_state.json 2>&1 || true
echo "===PYTHON==="
python - <<'PY'
import json, os, glob, time
from pathlib import Path

def load(p):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception as e:
        return {"_err": str(e)}

prog = load("/data/m3_bidnet_engine_v1_progress.json")
print("PROGRESS", {k: prog.get(k) for k in ("stage","progress_pct","workers","completed","total","rate","throughput","heartbeat_at","_err")})

ckpt = load("/data/m3_bidnet_downstream_v1_checkpoint.json")
if "_err" not in ckpt:
    rows = ckpt.get("rows") or []
    deep = sum(1 for r in rows if isinstance(r, dict) and r.get("deep_complete"))
    print("CKPT", {
        "deep_processed_field": ckpt.get("deep_processed"),
        "deep_counted": deep,
        "updated_at": ckpt.get("updated_at"),
        "build": ckpt.get("build"),
        "engine": ckpt.get("engine"),
        "mtime": time.ctime(os.path.getmtime("/data/m3_bidnet_downstream_v1_checkpoint.json")),
    })
else:
    print("CKPT", ckpt)

rep = load("/data/m3_bidnet_engine_v1_last_report.json")
if "_err" not in rep:
    print("REPORT", {
        "PASS_FAIL": rep.get("PASS_FAIL"),
        "selected_workers": rep.get("selected_workers"),
        "baseline": rep.get("baseline"),
        "after": rep.get("after"),
        "runtime_s": rep.get("runtime_s"),
        "mtime": time.ctime(os.path.getmtime("/data/m3_bidnet_engine_v1_last_report.json")) if os.path.exists("/data/m3_bidnet_engine_v1_last_report.json") else None,
    })
else:
    print("REPORT", rep)

# find job persistence
cands = []
for p in glob.glob("/data/**/*", recursive=True):
    name = os.path.basename(p).lower()
    if "bne-7fc7c8ba0dfa" in name or ("auth" in name and "job" in name and p.endswith(".json")):
        cands.append(p)
print("JOB_CANDS", cands[:30])
for p in cands[:10]:
    try:
        d = json.loads(Path(p).read_text(encoding="utf-8"))
        if isinstance(d, dict) and ("BNE-7fc7c8ba0dfa" in json.dumps(d) or d.get("job_id") == "BNE-7fc7c8ba0dfa"):
            print("JOBFILE", p, {k: d.get(k) for k in ("job_id","status","completed_at","error","progress","result") if k in d or True})
            print("JOB_PROGRESS", d.get("progress"))
            print("JOB_RESULT_KEYS", list((d.get("result") or {}).keys())[:20])
    except Exception:
        # maybe a dict of jobs
        try:
            d = json.loads(Path(p).read_text(encoding="utf-8"))
            if isinstance(d, dict) and "BNE-7fc7c8ba0dfa" in d:
                j = d["BNE-7fc7c8ba0dfa"]
                print("JOBMAP", p, j.get("status"), j.get("progress"), (j.get("error") or "")[:200])
        except Exception as e:
            print("JOB_READ_ERR", p, e)
PY
