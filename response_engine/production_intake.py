"""Production solicitation intake — real files → R1 ResponseProject.

0 SAM API calls. Uses local evidence artifacts, public URLs already known,
and operator uploads. Reuses document_ingestion / table_extractors / pdf_text.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from application_clock import now_utc
from response_engine.constants import (
    BUILD,
    CONTROLLING,
    DOCUMENTS_INCOMPLETE,
    DOCUMENTS_READY,
    INTAKE_IN_PROGRESS,
    SUPERSEDED_DOC,
)
from response_engine.document_graph import add_document_to_graph, register_amendment
from response_engine.intake import classify_document_type, ingest_document
from response_engine.models import content_hash, new_hard_block
from response_engine.package_completeness import evaluate_package_completeness
from response_engine.parsers import (
    AUTH_REQUIRED,
    CONTENT_TYPE_MISMATCH,
    FETCHED,
    FETCH_FAILED,
    NOT_FOUND,
    OCR_REQUIRED,
    PARSER_VERSION,
    UNSUPPORTED_FORMAT,
    classify_buyer_template,
    parse_file_bytes,
    sha256_bytes,
)
from response_engine.service import compile_project
from response_engine.store import load_project, save_project

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"
EVIDENCE = ARTIFACTS / "transactional_procurement_evidence"
INBOX = ARTIFACTS / "document_inbox"
BINARY_STORE = ARTIFACTS / "response_engine" / "binaries"
BUILD_R11 = "20260929-m3-r11-production-intake-legacy-cutover"

# In-memory intake job status (simple; persists lightly on project)
_INTAKE_JOBS: dict[str, dict[str, Any]] = {}


def _utc() -> str:
    return now_utc().isoformat()


def intake_status(response_project_id: str) -> dict[str, Any]:
    job = _INTAKE_JOBS.get(response_project_id) or {}
    project = load_project(response_project_id)
    return {
        "response_project_id": response_project_id,
        "phase": job.get("phase") or (project or {}).get("intake", {}).get("phase") or "idle",
        "message": job.get("message") or "",
        "updated_at": job.get("updated_at"),
        "package_completeness": (project or {}).get("package_completeness"),
        "intake": (project or {}).get("intake"),
    }


def _set_phase(project: dict[str, Any], phase: str, message: str = "") -> None:
    rid = project["response_project_id"]
    project.setdefault("intake", {})["phase"] = phase
    project["intake"]["message"] = message
    project["intake"]["updated_at"] = _utc()
    _INTAKE_JOBS[rid] = {
        "phase": phase,
        "message": message,
        "updated_at": _utc(),
    }


def discover_local_package_files(
    *,
    solicitation_number: str | None = None,
    buyer: str | None = None,
    title: str | None = None,
) -> list[dict[str, Any]]:
    """Find already-downloaded solicitation files under artifacts (0 network)."""
    found: list[dict[str, Any]] = []
    keys = []
    if solicitation_number:
        keys.append(re.sub(r"[^\w.\-]+", "", str(solicitation_number)))
        keys.append(str(solicitation_number))
    if title:
        keys.append(str(title)[:40])

    search_roots = [EVIDENCE, ARTIFACTS]
    for root in search_roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".xlsm", ".csv", ".html", ".htm", ".txt", ".zip"}:
                continue
            if "_t" in path.parts or "_temp" in str(path) or "fixtures_round2" in path.name:
                # allow some fixtures but skip scratch
                if "_t" in path.parts or "_temp" in str(path):
                    continue
            rel = str(path)
            try:
                rel = str(path.relative_to(ARTIFACTS))
            except ValueError:
                pass
            name_blob = f"{path.name} {path.parent.name}".lower()
            matched = False
            if not keys:
                # only include known evidence PDFs when no key
                if "transactional_procurement_evidence" in rel.replace("\\", "/") or path.name == "iowa_wildflower_event.pdf":
                    matched = True
            else:
                for k in keys:
                    if k and k.lower().replace(" ", "")[:12] in name_blob.replace(" ", "").replace("-", ""):
                        matched = True
                        break
                    if k and any(part and part.lower() in name_blob for part in re.split(r"[-_\s]+", k) if len(part) > 4):
                        matched = True
                        break
            if matched:
                found.append(
                    {
                        "path": str(path),
                        "filename": path.name,
                        "source": "local_artifact",
                        "source_url": f"file://{path}",
                    }
                )
    # Dedupe by path
    uniq = {}
    for f in found:
        uniq[f["path"]] = f
    return list(uniq.values())


def discover_opportunity_seed(canonical_opportunity_id: str) -> dict[str, Any]:
    """Pull authoritative hints from L.23 store without SAM calls."""
    out: dict[str, Any] = {"canonical_opportunity_id": canonical_opportunity_id}
    try:
        from phase_l.l23_full_population_funnel import load_store

        rec = load_store().get(canonical_opportunity_id) or {}
        out.update(
            {
                "buyer": rec.get("buyer"),
                "title": rec.get("title"),
                "solicitation_number": rec.get("solicitation_event_id"),
                "jurisdiction": "FEDERAL" if rec.get("is_federal") else rec.get("jurisdiction"),
                "discovery_source": rec.get("platform"),
                "authoritative_url": rec.get("authoritative_url"),
                "submission_path": rec.get("submission_path"),
                "is_federal": rec.get("is_federal"),
            }
        )
    except Exception:
        pass
    return out


def preserve_binary(
    *,
    response_project_id: str,
    filename: str,
    data: bytes,
) -> dict[str, Any]:
    BINARY_STORE.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^\w.\-]+", "_", filename)[:160]
    h = sha256_bytes(data)
    dest = BINARY_STORE / response_project_id / f"{h[:16]}_{safe_name}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        dest.write_bytes(data)
    return {"path": str(dest), "file_hash": h, "bytes": len(data), "original_filename": filename}


def ingest_bytes_into_project(
    project: dict[str, Any],
    *,
    data: bytes,
    filename: str,
    source_url: str | None = None,
    authoritative: bool = True,
    acquisition: str = "LOCAL_ARTIFACT",
    document_type: str | None = None,
    title: str | None = None,
    mark_authoritative: bool = False,
) -> dict[str, Any]:
    """Parse bytes, store original, add to R1 document graph (idempotent by hash)."""
    parse = parse_file_bytes(data, filename=filename)
    stored = preserve_binary(
        response_project_id=project["response_project_id"],
        filename=filename,
        data=data,
    )
    file_hash = stored["file_hash"]

    # Same-name changed-hash → supersede prior controlling with same filename
    supersedes_id = None
    for d in project.get("documents") or []:
        same_name = (d.get("filename") or "").lower() == filename.lower()
        if same_name and d.get("file_hash") and d.get("file_hash") != file_hash:
            if d.get("controlling_status") == CONTROLLING:
                supersedes_id = d["document_id"]
                break
        # Exact byte duplicate of same logical filename → skip re-ingest
        if d.get("file_hash") == file_hash and same_name:
            return {"ok": True, "duplicate": True, "document_id": d["document_id"], "parse": parse}
        # Same bytes under a different name — reuse binary, record alias (no second parse store)
        if d.get("file_hash") == file_hash and not same_name:
            d.setdefault("provenance_refs", []).append(
                {"filename": filename, "source_url": source_url, "retrieved_at": _utc()}
            )
            return {"ok": True, "duplicate": True, "aliased_from": d["document_id"], "document_id": d["document_id"], "parse": parse}

    if parse.get("parse_status") == CONTENT_TYPE_MISMATCH:
        project.setdefault("intake", {}).setdefault("fetch_results", []).append(
            {"filename": filename, "status": CONTENT_TYPE_MISMATCH, "source_url": source_url}
        )
        return {"ok": False, "error": CONTENT_TYPE_MISMATCH, "parse": parse}

    template_class = classify_buyer_template(filename, parse.get("text"), parse.get("workbook"))
    dtype = document_type or classify_document_type(title=title or filename, filename=filename, text=parse.get("text"))
    is_template = bool(template_class) or dtype in {"PRICING_SHEET", "BUYER_TEMPLATE", "COST_SHEET"}

    # Amendment number
    amd_num = None
    if dtype == "AMENDMENT":
        m = re.search(r"amendment\s*(?:no\.?|#)?\s*0*(\d+)", f"{title or ''} {filename}", re.I)
        if m:
            amd_num = m.group(1)

    text = parse.get("text") or ""
    # Lower confidence OCR-derived requirements will be review-gated in compile
    doc = ingest_document(
        project,
        title=title or filename,
        filename=filename,
        source_url=source_url,
        text=text,
        document_type=dtype,
        authoritative_source=project.get("authoritative_source") if authoritative else None,
        source_system=acquisition,
        amendment_number=amd_num,
        supersedes_document_id=supersedes_id,
        is_buyer_template=is_template,
    )
    # Enrich with binary/parse metadata (not in basic ingest)
    for d in project.get("documents") or []:
        if d["document_id"] == doc["document_id"]:
            d["file_hash"] = file_hash
            d["content_hash"] = file_hash if not text else content_hash(text)
            d["binary_path"] = stored["path"]
            d["original_filename"] = filename
            d["parse_method"] = parse.get("method")
            d["parse_status"] = parse.get("parse_status")
            d["extraction_confidence"] = parse.get("confidence")
            d["ocr_required"] = parse.get("ocr_required")
            d["parser_version"] = PARSER_VERSION
            d["flags"] = parse.get("flags") or []
            d["workbook"] = parse.get("workbook")
            d["buyer_template_class"] = template_class
            d["acquisition"] = acquisition
            d["mark_authoritative"] = mark_authoritative or authoritative
            if parse.get("ocr"):
                d["ocr"] = parse["ocr"]
            if acquisition == "OWNER_UPLOADED_FROM_BUYER":
                d["source_tag"] = "OWNER_UPLOADED_FROM_BUYER"
            if supersedes_id:
                d["supersedes_document_id"] = supersedes_id
                # Amendment / revision structural diff
                try:
                    from response_engine.amendment_diff import apply_amendment_diff_to_project

                    old = next(x for x in project.get("documents") or [] if x["document_id"] == supersedes_id)
                    apply_amendment_diff_to_project(
                        project,
                        before_doc=old,
                        after_doc=d,
                        amendment_number=amd_num or "REV",
                    )
                except Exception:
                    pass
            # Graph edge ATTACHED_TO base
            bases = [x for x in project.get("documents") or [] if x.get("document_type") == "BASE_SOLICITATION"]
            if bases and d["document_id"] != bases[0]["document_id"]:
                project.setdefault("document_graph", {}).setdefault("edges", []).append(
                    {"from": d["document_id"], "to": bases[0]["document_id"], "relation": "ATTACHED_TO"}
                )
            break

    if amd_num:
        register_amendment(project, number=amd_num, document_id=doc["document_id"], changes={"other": True})

    # Master solicitation shared cache reference
    if dtype == "MASTER_SOLICITATION":
        project.setdefault("document_graph", {}).setdefault("edges", []).append(
            {"from": doc["document_id"], "to": project["response_project_id"], "relation": "MASTER_FOR"}
        )

    # ZIP package: expand supported members into related documents (preserve package relationship)
    member_ids: list[str] = []
    if (filename or "").lower().endswith(".zip") and parse.get("ok"):
        try:
            from table_extractors import safe_inspect_zip

            z = safe_inspect_zip(data)
            for mem in z.get("extracted") or []:
                mem_bytes = mem.get("bytes") or b""
                mem_name = Path(mem.get("safe_name") or mem.get("name") or "member.bin").name
                if not mem_bytes:
                    continue
                child = ingest_bytes_into_project(
                    project,
                    data=mem_bytes,
                    filename=mem_name,
                    source_url=source_url,
                    authoritative=authoritative,
                    acquisition=acquisition,
                    title=mem_name,
                )
                if child.get("ok") and child.get("document_id"):
                    member_ids.append(child["document_id"])
                    project.setdefault("document_graph", {}).setdefault("edges", []).append(
                        {
                            "from": child["document_id"],
                            "to": doc["document_id"],
                            "relation": "ATTACHED_TO",
                            "package": filename,
                        }
                    )
        except Exception:
            pass

    return {
        "ok": True,
        "duplicate": False,
        "document_id": doc["document_id"],
        "parse": parse,
        "document": doc,
        "zip_member_ids": member_ids,
    }


def run_production_intake(
    project: dict[str, Any],
    *,
    local_paths: list[str] | None = None,
    compile_after: bool = True,
    try_url_fetch: bool = False,
) -> dict[str, Any]:
    """
    Main R1.1 entry: discover local package files, parse, graph, compile.
    try_url_fetch defaults False to guarantee 0 SAM / avoid burning credits;
    public non-SAM URLs can be enabled explicitly.
    """
    project["response_status"] = INTAKE_IN_PROGRESS
    project["build_r11"] = BUILD_R11
    _set_phase(project, "Fetching solicitation", "Discovering package files")
    seed = discover_opportunity_seed(project.get("canonical_opportunity_id") or "")
    if seed.get("authoritative_url") and not project.get("authoritative_source"):
        project["authoritative_source"] = seed.get("authoritative_url")
    if seed.get("discovery_source") and not project.get("discovery_source"):
        project["discovery_source"] = seed["discovery_source"]
    # submission_path may be dict
    sp = seed.get("submission_path")
    if isinstance(sp, dict) and not project.get("submission_system"):
        project["submission_system"] = sp.get("system") or sp.get("portal") or "UNKNOWN"
    elif isinstance(sp, str) and not project.get("submission_system"):
        project["submission_system"] = sp

    fetch_results: list[dict[str, Any]] = []
    files = list(local_paths or [])
    discovered = discover_local_package_files(
        solicitation_number=project.get("solicitation_number") or seed.get("solicitation_number"),
        buyer=project.get("buyer") or seed.get("buyer"),
        title=project.get("title") or seed.get("title"),
    )
    # If specific sol number match empty, fall back to evidence corpus for validation projects
    if not discovered and not files:
        # Prefer iowa/evidence PDFs as last resort only when solicitation_number matches evidence folder names
        sol = str(project.get("solicitation_number") or "")
        for p in discover_local_package_files():
            if sol and sol.lower() in p["path"].lower().replace("\\", "/"):
                discovered.append(p)

    for item in discovered:
        files.append(item["path"])

    _set_phase(project, "Reading attachments", f"{len(files)} candidate file(s)")
    ingested = 0
    duplicates = 0
    failed = 0
    for fpath in files:
        path = Path(fpath)
        if not path.exists():
            fetch_results.append({"filename": str(fpath), "status": NOT_FOUND})
            failed += 1
            continue
        data = path.read_bytes()
        result = ingest_bytes_into_project(
            project,
            data=data,
            filename=path.name,
            source_url=f"file://{path}",
            authoritative=True,
            acquisition="LOCAL_ARTIFACT",
            title=path.stem,
        )
        if result.get("duplicate"):
            duplicates += 1
            fetch_results.append({"filename": path.name, "status": FETCHED, "duplicate": True})
        elif result.get("ok"):
            ingested += 1
            fetch_results.append(
                {
                    "filename": path.name,
                    "status": result.get("parse", {}).get("parse_status") or FETCHED,
                    "parse_method": result.get("parse", {}).get("method"),
                    "confidence": result.get("parse", {}).get("confidence"),
                }
            )
        else:
            failed += 1
            fetch_results.append({"filename": path.name, "status": result.get("error") or FETCH_FAILED})

    # URL fetch optional (non-SAM only)
    auth_url = project.get("authoritative_source") or seed.get("authoritative_url")
    if try_url_fetch and auth_url and str(auth_url).startswith("http") and "api.sam.gov" not in str(auth_url).lower():
        _set_phase(project, "Checking amendments", "Optional public URL fetch")
        fr = _safe_public_fetch(auth_url, project)
        fetch_results.append(fr)
        if fr.get("status") in {"LOGIN_REQUIRED", AUTH_REQUIRED, "AUTH_REQUIRED"}:
            project.setdefault("intake", {})["auth_url"] = auth_url

    if auth_url and "sam.gov" in str(auth_url).lower() and not files and not ingested:
        # Do not call SAM API — mark auth/review
        fetch_results.append(
            {
                "filename": "sam.gov",
                "status": AUTH_REQUIRED,
                "note": "SAM notice page requires separate package retrieval; 0 SAM API calls used.",
                "url": auth_url,
            }
        )

    project.setdefault("intake", {})
    project["intake"].update(
        {
            "build": BUILD_R11,
            "parser_version": PARSER_VERSION,
            "fetch_results": fetch_results,
            "ingested": ingested,
            "duplicates": duplicates,
            "failed": failed,
            "local_files_considered": len(files),
            "sam_api_calls": 0,
        }
    )

    _set_phase(project, "Compiling requirements", "Document graph + atomic requirements")
    # Completeness before compile so missing refs are known
    evaluate_package_completeness(project)

    if compile_after and project.get("documents"):
        compile_project(project, persist=False)
        # Re-evaluate after compile; inject missing-ref hard blocks
        evaluate_package_completeness(project)
        for mref in project.get("intake_missing_refs") or []:
            project.setdefault("hard_blocks", []).append(
                new_hard_block(
                    reason=f"REFERENCED_DOCUMENT_MISSING: {mref.get('label')}",
                    source=mref.get("excerpt"),
                    resolution_action=f"Locate or upload '{mref.get('label')}' from authoritative source.",
                )
            )
        # OCR-derived material → REVIEW / OCR_REVIEW_REQUIRED
        try:
            from response_engine.ocr import build_ocr_review_queue, gate_ocr_requirements

            gate_ocr_requirements(project)
            build_ocr_review_queue(project)
        except Exception:
            for req in project.get("requirements") or []:
                src = next((d for d in project.get("documents") or [] if d.get("document_id") == req.get("source_document_id")), None)
                if src and (src.get("ocr_required") or "OCR_REQUIRED" in (src.get("flags") or [])):
                    if req.get("mandatory") and req.get("compliance_status") == "UNKNOWN":
                        req["compliance_status"] = "REVIEW_REQUIRED"
                        req["confidence"] = "LOW"
                        req["notes"] = (req.get("notes") or "") + " OCR-derived — review required."
        from response_engine.compliance import build_compliance_matrix, compute_hard_blocks

        build_compliance_matrix(project)
        compute_hard_blocks(project)

        # Cap readiness — never READY_FOR_SUBMISSION in R1.1
        if project.get("response_status") in {"READY_FOR_SUBMISSION", "READY_FOR_PREFLIGHT", "READY_FOR_OWNER_REVIEW"}:
            project["response_status"] = "READY_FOR_RESPONSE_BUILD"
        pc = project.get("package_completeness") or {}
        if not pc.get("document_package_complete"):
            if project.get("response_status") not in {"BLOCKED", "CLARIFICATION_REQUIRED"}:
                project["response_status"] = DOCUMENTS_INCOMPLETE
        elif project.get("hard_block_count", 0) == 0 and (project.get("compliance_matrix") or {}).get("summary", {}).get("material_unresolved", 1) == 0:
            project["response_status"] = "READY_FOR_RESPONSE_BUILD"
        else:
            # Compiled but unresolved material — compliance review
            if project.get("response_status") not in {"BLOCKED", "CLARIFICATION_REQUIRED"}:
                project["response_status"] = "COMPLIANCE_REVIEW"

    _set_phase(project, "Done", "Intake complete")
    project["intake"]["completed_at"] = _utc()
    save_project(project)
    return {
        "ok": True,
        "response_project_id": project["response_project_id"],
        "ingested": ingested,
        "duplicates": duplicates,
        "failed": failed,
        "documents": len(project.get("documents") or []),
        "package_completeness": project.get("package_completeness"),
        "response_status": project.get("response_status"),
        "sam_api_calls": 0,
        "build": BUILD_R11,
    }


def _safe_public_fetch(url: str, project: dict[str, Any]) -> dict[str, Any]:
    """Best-effort public fetch without SAM API. Failures become AUTH/FETCH states."""
    if "api.sam.gov" in url.lower():
        return {"filename": url, "status": AUTH_REQUIRED, "note": "SAM API blocked by policy"}
    try:
        import urllib.request

        req = urllib.request.Request(url, headers={"User-Agent": "M3-GovTracker-R11/1.0"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = resp.read(8_000_000)
            ctype = resp.headers.get("Content-Type", "")
        name = Path(url.split("?")[0]).name or "download.bin"
        if "html" in ctype.lower() and not name.lower().endswith((".html", ".htm")):
            # may be login page
            if b"login" in data[:3000].lower() or b"sign in" in data[:3000].lower():
                return {"filename": name, "status": AUTH_REQUIRED, "url": url}
        result = ingest_bytes_into_project(
            project,
            data=data,
            filename=name if "." in name else (name + (".html" if "html" in ctype.lower() else ".bin")),
            source_url=url,
            authoritative=True,
            acquisition="PUBLIC_FETCH",
        )
        return {"filename": name, "status": FETCHED if result.get("ok") else result.get("error"), "url": url}
    except Exception as exc:
        msg = str(exc).lower()
        status = AUTH_REQUIRED if "401" in msg or "403" in msg else FETCH_FAILED
        return {"filename": url, "status": status, "error": str(exc)[:200]}


def manual_upload_document(
    project: dict[str, Any],
    *,
    data: bytes,
    filename: str,
    mark_as_authoritative: bool = False,
    document_type: str | None = None,
) -> dict[str, Any]:
    """Operator upload fallback for auth/anti-bot portals."""
    safe = Path(filename).name  # path traversal guard
    if ".." in filename or "/" in filename.replace("\\", "/").lstrip(".") and filename.startswith("/"):
        safe = Path(filename).name
    result = ingest_bytes_into_project(
        project,
        data=data,
        filename=safe,
        source_url=None,
        authoritative=mark_as_authoritative,
        acquisition="OWNER_UPLOADED_FROM_BUYER",
        document_type=document_type,
        mark_authoritative=mark_as_authoritative,
    )
    evaluate_package_completeness(project)
    if project.get("documents"):
        compile_project(project, persist=True)
    else:
        save_project(project)
    return result


def start_bid_prep_production(
    canonical_opportunity_id: str,
    *,
    local_paths: list[str] | None = None,
    force_new: bool = False,
) -> dict[str, Any]:
    """Owner UI START BID PREP — create project + production intake."""
    from response_engine.service import create_or_get_project_from_opportunity, get_project_view

    seed = discover_opportunity_seed(canonical_opportunity_id)
    project = create_or_get_project_from_opportunity(
        canonical_opportunity_id=canonical_opportunity_id,
        buyer=seed.get("buyer"),
        solicitation_number=seed.get("solicitation_number"),
        title=seed.get("title"),
        jurisdiction=seed.get("jurisdiction"),
        discovery_source=seed.get("discovery_source"),
        authoritative_source=seed.get("authoritative_url"),
        submission_system=None,
        force_new=force_new,
    )
    # Idempotent: if already has documents and no force paths, skip re-ingest unless refresh
    if project.get("documents") and not local_paths and project.get("intake", {}).get("completed_at"):
        return {
            "ok": True,
            "skipped_reingest": True,
            "view": get_project_view(project["response_project_id"]),
            "intake": project.get("intake"),
            "sam_api_calls": 0,
        }
    result = run_production_intake(project, local_paths=local_paths, compile_after=True, try_url_fetch=False)
    return {
        **result,
        "view": get_project_view(project["response_project_id"]),
    }


def refresh_solicitation(project: dict[str, Any]) -> dict[str, Any]:
    """Controlled refresh — re-discover local/new files; only ingest new hashes."""
    before = {d.get("file_hash") for d in project.get("documents") or []}
    result = run_production_intake(project, compile_after=True, try_url_fetch=False)
    after = {d.get("file_hash") for d in project.get("documents") or []}
    result["new_hashes"] = list(after - before)
    result["refreshed"] = True
    return result
