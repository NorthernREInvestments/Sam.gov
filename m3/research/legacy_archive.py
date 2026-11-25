"""Recoverable legacy-data archive: content-addressed zip, checksum manifest, purge manifest, restore.

Nothing is deleted. Every file under the source roots is hashed (SHA-256); each unique content is
stored once in the zip as ``blobs/<sha256>``, and ``manifest.jsonl`` maps original paths to hashes.
The purge manifest lists active-location files that are fully recoverable from the archive and
marks them eligible for removal only after separate owner authorization. Files feeding active
staged loaders (pricing intelligence, legacy BidNet history) are marked KEEP_ACTIVE.
"""

from __future__ import annotations

import hashlib
import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

BUILD = "20261124-legacy-archive-v1"
RESTORE_README = """M3 legacy archive — restore instructions

1. Verify: python -m m3.research.jobs legacy-archive-verify --archive <this zip>
   (re-hashes every blob and checks it against manifest.jsonl)
2. Restore everything to a new folder:
   python -m m3.research.jobs legacy-archive-restore --archive <this zip> --dest <empty folder>
   Each original file is written to <dest>/<root label>/<relative path> with its original bytes.
3. Restore one root only: add --root <label> (labels are listed in archive_meta.json).
No step deletes anything; restore refuses to overwrite existing files.
"""


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _walk(root: Path) -> Iterable[Path]:
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            yield Path(dirpath) / f


def build_legacy_archive(
    roots: dict[str, Path], *, archive_path: Path, keep_active: Iterable[Path] = (), checkpoint_every: int = 200
) -> dict[str, Any]:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    if archive_path.exists():
        raise FileExistsError(f"archive exists: {archive_path}")
    keep = {str(Path(p).resolve()).lower() for p in keep_active}
    tmp = archive_path.with_suffix(".partial.zip")
    entries: list[dict[str, Any]] = []
    stored: set[str] = set()
    total_bytes = stored_bytes = 0
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as zf:
        for label, root in roots.items():
            for p in _walk(root):
                try:
                    size = p.stat().st_size
                    digest = _sha256(p)
                except OSError as exc:
                    entries.append({"root": label, "path": str(p.relative_to(root)), "error": type(exc).__name__})
                    continue
                total_bytes += size
                if digest not in stored:
                    zf.write(p, f"blobs/{digest}")
                    stored.add(digest)
                    stored_bytes += size
                entries.append({"root": label, "path": str(p.relative_to(root)).replace("\\", "/"), "sha256": digest,
                                "bytes": size, "mtime": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat(),
                                "keep_active": str(p.resolve()).lower() in keep})
        meta = {"build": BUILD, "created_at": _utc(), "roots": {k: str(v) for k, v in roots.items()},
                "files": len(entries), "unique_blobs": len(stored), "source_bytes": total_bytes,
                "unique_bytes": stored_bytes, "errors": sum(1 for e in entries if e.get("error"))}
        zf.writestr("manifest.jsonl", "\n".join(json.dumps(e) for e in entries) + "\n")
        zf.writestr("archive_meta.json", json.dumps(meta, indent=2))
        zf.writestr("RESTORE.txt", RESTORE_README)
    tmp.replace(archive_path)
    archive_sha = _sha256(archive_path)
    side = archive_path.with_suffix(".sha256")
    side.write_text(f"{archive_sha}  {archive_path.name}\n", encoding="utf-8")
    purge = [{"root": e["root"], "path": e["path"], "absolute": str(roots[e["root"]] / e["path"]), "sha256": e["sha256"],
              "bytes": e["bytes"],
              "action": "KEEP_ACTIVE" if e["keep_active"] else "ELIGIBLE_FOR_REMOVAL_AFTER_OWNER_AUTHORIZATION",
              "recoverable_from": archive_path.name}
             for e in entries if not e.get("error")]
    purge_p = archive_path.with_name(archive_path.stem + "_purge_manifest.jsonl")
    purge_p.write_text("\n".join(json.dumps(r) for r in purge) + "\n", encoding="utf-8")
    return {**meta, "archive": str(archive_path), "archive_sha256": archive_sha, "archive_bytes": archive_path.stat().st_size,
            "purge_manifest": str(purge_p), "keep_active": sum(1 for r in purge if r["action"] == "KEEP_ACTIVE"),
            "eligible_after_authorization": sum(1 for r in purge if r["action"] != "KEEP_ACTIVE")}


def verify_legacy_archive(archive_path: Path) -> dict[str, Any]:
    bad: list[str] = []
    with zipfile.ZipFile(archive_path) as zf:
        entries = [json.loads(l) for l in zf.read("manifest.jsonl").decode().splitlines() if l.strip()]
        names = set(zf.namelist())
        needed = {e["sha256"] for e in entries if e.get("sha256")}
        for digest in needed:
            name = f"blobs/{digest}"
            if name not in names:
                bad.append(f"missing:{digest}")
                continue
            h = hashlib.sha256()
            with zf.open(name) as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            if h.hexdigest() != digest:
                bad.append(f"mismatch:{digest}")
    return {"ok": not bad, "files": len(entries), "blobs_checked": len(needed), "problems": bad[:50]}


def restore_legacy_archive(archive_path: Path, dest: Path, *, root: str | None = None) -> dict[str, Any]:
    n = 0
    with zipfile.ZipFile(archive_path) as zf:
        entries = [json.loads(l) for l in zf.read("manifest.jsonl").decode().splitlines() if l.strip()]
        for e in entries:
            if not e.get("sha256") or (root and e["root"] != root):
                continue
            out = dest / e["root"] / e["path"]
            if out.exists():
                raise FileExistsError(f"refusing to overwrite {out}")
            out.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(f"blobs/{e['sha256']}") as src, open(out, "wb") as dst:
                for chunk in iter(lambda: src.read(1 << 20), b""):
                    dst.write(chunk)
            n += 1
    return {"restored": n, "dest": str(dest)}
