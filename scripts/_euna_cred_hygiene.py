"""Sanitize-inspect Railway Euna credentials. Never prints secret values."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def inspect_secret(name: str, val: str) -> dict:
    s = val if isinstance(val, str) else ""
    stripped = s.strip()
    quoted = len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {'"', "'"}
    unquoted = stripped[1:-1] if quoted else stripped
    out = {
        f"{name}_present": bool(stripped),
        f"{name}_length": len(s),
        f"{name}_stripped_length": len(stripped),
        f"{name}_leading_ws": len(s) - len(s.lstrip(" \t\r\n")),
        f"{name}_trailing_ws": len(s) - len(s.rstrip(" \t\r\n")),
        f"{name}_surrounding_quotes": quoted,
        f"{name}_empty_after_normalize": not bool(unquoted.strip()),
    }
    if name == "username":
        out["username_looks_like_email"] = bool(
            re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", unquoted.strip())
        )
    return out


def main() -> int:
    proc = subprocess.run(
        ["railway", "variables", "--json"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
        shell=True,  # Windows PATH resolution for railway.cmd
    )
    if proc.returncode != 0:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "railway_variables_failed",
                    "code": proc.returncode,
                    "stderr": (proc.stderr or "")[:300],
                }
            )
        )
        return 1
    d = json.loads(proc.stdout)
    vars_ = d if isinstance(d, dict) and "EUNA_USERNAME" in d else (d.get("variables") or d)
    out = {"ok": True}
    out.update(inspect_secret("username", vars_.get("EUNA_USERNAME") or ""))
    out.update(inspect_secret("password", vars_.get("EUNA_PASSWORD") or ""))
    out["EUNA_AUTH_ENABLED"] = vars_.get("EUNA_AUTH_ENABLED")
    out["EUNA_HEADLESS"] = vars_.get("EUNA_HEADLESS")
    out["EUNA_LOGIN_URL_set"] = bool(vars_.get("EUNA_LOGIN_URL"))
    out["EUNA_VERIFY_URL_set"] = bool(vars_.get("EUNA_VERIFY_URL"))
    out["M3_DATA_ROOT_set"] = bool(vars_.get("M3_DATA_ROOT"))
    out["euna_keys"] = sorted(
        k for k in vars_ if str(k).upper().startswith("EUNA") or k == "M3_DATA_ROOT"
    )
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
