"""OpenAI field repair for incomplete Opportunity UI rows.

Unknown Closes / Category / Value / solicitation id = FAIL until repaired.
Do not archive or hide — gather local docs + listing text, ask OpenAI, write
only evidence-backed values.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from m3.config import get_settings
from m3.db import SessionLocal
from m3.models import Opportunity
from m3.research.documents import extract_docx_text, extract_pdf_text
from m3.research.free_municipal_adapters import _guess_category
from m3.research.http_util import fetch_bytes, fetch_text
from m3.research.opportunity_ui_ready import (
    opportunity_ui_ready,
    parse_deadline_value,
    solicitation_ok,
    title_ok,
)
from m3.research.openai_client import openai_ready

from m3.research.product_category import CATEGORY_CHOICES as _CATEGORY_LIST

_CATEGORY_CHOICES = ",".join(_CATEGORY_LIST)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _needs_repair(o: Opportunity) -> list[str]:
    ok, reasons = opportunity_ui_ready(o)
    if not o.product_category or o.product_category in {"unknown", ""}:
        reasons = list(reasons) + (["missing_category"] if "missing_category" not in reasons else [])
    if o.estimated_value_unknown and o.estimated_value_min is None:
        reasons = list(reasons) + (["missing_value"] if "missing_value" not in reasons else [])
    # Still repair even if "ready" but category/value unknown — value is optional
    # unless closes is missing; always repair if opportunity_ui_ready fails.
    if not ok:
        return reasons
    if not o.product_category or o.product_category in {"unknown", ""}:
        return ["missing_category"]
    return []


def _keep_dir_for(o: Opportunity) -> Path | None:
    root = get_settings().data_root() / "free_source_keep" / "by_source"
    sk = (o.source_key or "").strip()
    sol = re.sub(r"[^\w\-]+", "_", str(o.solicitation_number or "").strip())
    if sk and sol:
        p = root / sk / sol
        if p.is_dir():
            return p
    if sk:
        # fuzzy: match folder containing sol tokens
        base = root / sk
        if base.is_dir():
            for d in base.iterdir():
                if d.is_dir() and sol and sol.lower() in d.name.lower():
                    return d
    return None


def _extract_file_text(path: Path, *, max_chars: int = 12000) -> str:
    try:
        data = path.read_bytes()
    except Exception:
        return ""
    name = path.name.lower()
    try:
        if name.endswith(".pdf"):
            ex = extract_pdf_text(data)
            t = str((ex or {}).get("text") or "")
        elif name.endswith(".docx"):
            ex = extract_docx_text(data)
            t = str((ex or {}).get("text") or "")
        elif name.endswith((".txt", ".html", ".htm", ".csv")):
            t = data.decode("utf-8", errors="replace")
        else:
            return ""
    except Exception:
        return ""
    t = re.sub(r"\s+", " ", t).strip()
    return t[:max_chars]


def _gather_evidence(o: Opportunity, *, max_chars: int = 40000) -> dict[str, Any]:
    chunks: list[str] = []
    sources: list[str] = []

    # Prior AI doc-read finals
    ai_dir = get_settings().data_root() / "ai_free_source_doc_read" / (o.source_key or "")
    if ai_dir.is_dir():
        sol = re.sub(r"[^\w\-]+", "_", str(o.solicitation_number or ""))
        for p in sorted(ai_dir.glob("*.json")):
            if sol and sol not in p.stem and str(o.solicitation_number or "") not in p.stem:
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            final = data.get("final") or data
            blob = json.dumps(
                {
                    "close_date": final.get("close_date"),
                    "quantity": final.get("quantity"),
                    "what_they_want": final.get("what_they_want"),
                    "key_requirements": final.get("key_requirements"),
                    "title": final.get("title") or data.get("title"),
                },
                default=str,
            )
            chunks.append(f"[AI_DOC_READ {p.name}]\n{blob}")
            sources.append(f"ai:{p.name}")

    # Local keep package text (real docs only — skip Office XML guts)
    keep = _keep_dir_for(o)
    if keep:
        files = [
            p
            for p in keep.rglob("*")
            if p.is_file()
            and p.suffix.lower() in {".pdf", ".docx", ".doc", ".txt", ".html", ".htm"}
            and "_extracted" not in str(p).replace("\\", "/")
        ]
        files.sort(key=lambda p: (0 if "rfp" in p.name.lower() or "bid" in p.name.lower() else 1, p.stat().st_size))
        for p in files[:8]:
            t = _extract_file_text(p)
            if t:
                chunks.append(f"[LOCAL_DOC {p.name}]\n{t}")
                sources.append(f"local:{p.name}")

    # Official / detail page
    url = (o.official_url or "").strip()
    if url.startswith("http"):
        try:
            if any(url.lower().split("?", 1)[0].endswith(ext) for ext in (".pdf", ".docx", ".doc")):
                status, final, data, _ctype = fetch_bytes(url, timeout=45)
                if status < 400 and data:
                    name = unquote(urlparse(final or url).path.split("/")[-1]) or "download.bin"
                    tmp = Path(name)
                    if tmp.suffix.lower() == ".pdf":
                        ex = extract_pdf_text(data)
                        t = str((ex or {}).get("text") or "")[:12000]
                    elif tmp.suffix.lower() == ".docx":
                        ex = extract_docx_text(data)
                        t = str((ex or {}).get("text") or "")[:12000]
                    else:
                        t = ""
                    if t:
                        chunks.append(f"[OFFICIAL_FILE {name}]\n{re.sub(r'\s+', ' ', t)}")
                        sources.append(f"url_file:{url[:120]}")
            else:
                status, final, html = fetch_text(url, timeout=40)
                if status < 400 and html:
                    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
                    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
                    text = re.sub(r"<[^>]+>", " ", text)
                    text = re.sub(r"\s+", " ", text).strip()[:12000]
                    if text:
                        chunks.append(f"[OFFICIAL_PAGE]\n{text}")
                        sources.append(f"url_page:{url[:120]}")
        except Exception as exc:  # noqa: BLE001
            sources.append(f"url_error:{type(exc).__name__}")

    corpus = "\n\n".join(chunks)
    if len(corpus) > max_chars:
        corpus = corpus[:max_chars]
    return {"corpus": corpus, "sources": sources, "chars": len(corpus)}


def _ask_repair(client: Any, model: str, *, opp: Opportunity, evidence: str) -> tuple[dict[str, Any], Decimal]:
    from m3.research.every_word_doc_read import _ask_json  # reuse JSON helper

    prompt = (
        "Repair incomplete procurement opportunity fields using ONLY the evidence below.\n"
        "Never invent dates, values, solicitation numbers, or categories.\n"
        "If a field is not clearly present in the evidence, return null for that field.\n"
        "close_date must be an ISO date or datetime string when found (include timezone if stated).\n"
        "product_category must be one of: "
        f"{_CATEGORY_CHOICES} — or null.\n"
        "estimated_value_usd is a single number only if a published estimate/budget appears; else null.\n"
        "solicitation_number: short official id only (e.g. RFP 27-109, MT-54361, 26DPP01309); "
        "never a sentence.\n\n"
        "Return JSON keys: solicitation_number, close_date, product_category, "
        "estimated_value_usd, quantity, what_they_want, evidence_quote_close_date, "
        "evidence_quote_value, confidence (high|medium|low), notes.\n\n"
        f"CURRENT_TITLE: {opp.title}\n"
        f"CURRENT_SOLICITATION: {opp.solicitation_number}\n"
        f"CURRENT_BUYER: {opp.buyer_name}\n"
        f"CURRENT_SOURCE: {opp.source_key}\n"
        f"CURRENT_URL: {opp.official_url}\n\n"
        f"EVIDENCE:\n{evidence}"
    )
    return _ask_json(client, model, prompt)


def apply_repair_to_opportunity(o: Opportunity, parsed: dict[str, Any]) -> list[str]:
    """Apply evidence-backed fields. Returns list of fields updated."""
    updated: list[str] = []
    sol = parsed.get("solicitation_number")
    if solicitation_ok(sol) and (
        not solicitation_ok(o.solicitation_number) or not (o.solicitation_number or "").strip()
    ):
        o.solicitation_number = str(sol).strip()[:64]
        updated.append("solicitation_number")

    dt = parse_deadline_value(parsed.get("close_date"))
    if dt is not None and o.deadline_at is None:
        o.deadline_at = dt
        updated.append("deadline_at")

    cat = parsed.get("product_category")
    if isinstance(cat, str) and cat.strip() and cat.strip() not in {"unknown", "null", "None"}:
        if not o.product_category or o.product_category in {"unknown", ""}:
            o.product_category = cat.strip()[:128]
            updated.append("product_category")
    elif not o.product_category or o.product_category in {"unknown", ""}:
        guessed = _guess_category(o.title or "")
        if guessed:
            o.product_category = guessed
            updated.append("product_category_guess")

    val = parsed.get("estimated_value_usd")
    try:
        if val is not None and str(val).strip() and str(val).lower() != "null":
            num = float(val)
            if num > 0 and o.estimated_value_min is None:
                o.estimated_value_min = num
                o.estimated_value_max = num
                o.estimated_value_unknown = False
                o.value_estimate_method = "openai_field_repair_from_evidence"
                updated.append("estimated_value")
    except Exception:
        pass

    qty = parsed.get("quantity")
    if qty and not (o.quantity or "").strip():
        o.quantity = str(qty)[:128]
        updated.append("quantity")

    ev = dict(o.raw_evidence or {})
    ev["openai_field_repair"] = {
        "at": _utc(),
        "parsed": {k: parsed.get(k) for k in (
            "solicitation_number",
            "close_date",
            "product_category",
            "estimated_value_usd",
            "quantity",
            "confidence",
            "evidence_quote_close_date",
            "evidence_quote_value",
            "notes",
        )},
        "updated_fields": updated,
    }
    if parsed.get("what_they_want"):
        ev["what_they_want"] = str(parsed.get("what_they_want"))[:2000]
    o.raw_evidence = ev
    o.updated_at = datetime.now(timezone.utc)
    return updated


def restore_incomplete_from_archive(*, limit: int = 500) -> dict[str, Any]:
    """Re-open rows we soft-archived only for incomplete/Unknown fields."""
    db = SessionLocal()
    restored = 0
    try:
        rows = (
            db.query(Opportunity)
            .filter(Opportunity.status == "archived")
            .limit(limit)
            .all()
        )
        for o in rows:
            ev = o.raw_evidence or {}
            if ev.get("archived_reason") != "unknown_or_incomplete_fields":
                continue
            o.status = "saved"
            ev = dict(ev)
            ev.pop("archived_reason", None)
            ev.pop("archived_at", None)
            ev["restored_for_openai_repair"] = _utc()
            o.raw_evidence = ev
            db.add(o)
            restored += 1
        db.commit()
        return {"restored": restored}
    finally:
        db.close()


def repair_incomplete_opportunities(
    *,
    limit: int = 80,
    max_usd: float = 2.0,
    only_missing_closes: bool = False,
) -> dict[str, Any]:
    """Run OpenAI repair for incomplete Active/Saved opportunities."""
    ready, why = openai_ready()
    if not ready:
        return {"ok": False, "error": why}

    from openai import OpenAI

    settings = get_settings()
    client = OpenAI(api_key=settings.openai_api_key)
    model = getattr(settings, "openai_model", None) or "gpt-4.1-mini"

    db = SessionLocal()
    summary: dict[str, Any] = {
        "started_at": _utc(),
        "limit": limit,
        "max_usd": max_usd,
        "repaired": [],
        "skipped": [],
        "failed": [],
        "openai_spent_usd": 0.0,
    }
    spent = Decimal("0")
    try:
        # Include Active rows AND incompletes we held out of UI (archived with fail flag).
        rows = (
            db.query(Opportunity)
            .filter(Opportunity.status.in_(["saved", "pursuing", "archived"]))
            .order_by(Opportunity.updated_at.desc())
            .limit(800)
            .all()
        )
        targets: list[Opportunity] = []
        for o in rows:
            ev = o.raw_evidence if isinstance(o.raw_evidence, dict) else {}
            held_incomplete = bool(ev.get("blocked_from_ui_incomplete")) or (
                o.status == "archived" and "INCOMPLETE_FAIL" in (o.notes or "")
            )
            if o.status == "archived" and not held_incomplete:
                continue
            if not title_ok(o.title or o.display_title):
                summary["skipped"].append(
                    {"id": str(o.id), "reason": "junk_title", "title": (o.title or "")[:60]}
                )
                continue
            if not (o.official_url or "").strip():
                summary["skipped"].append({"id": str(o.id), "reason": "no_url"})
                continue
            reasons = _needs_repair(o)
            if only_missing_closes and "unknown_closes" not in reasons:
                continue
            if not reasons:
                continue
            # Prefer closes repair first
            targets.append(o)
            if len(targets) >= limit:
                break

        for o in targets:
            if float(spent) >= max_usd:
                summary["skipped"].append({"id": str(o.id), "reason": "budget_cap"})
                break
            ev = _gather_evidence(o)
            if ev["chars"] < 200:
                summary["skipped"].append(
                    {
                        "id": str(o.id),
                        "sol": o.solicitation_number,
                        "reason": "insufficient_evidence",
                        "sources": ev["sources"],
                    }
                )
                continue
            try:
                parsed, cost = _ask_repair(client, model, opp=o, evidence=ev["corpus"])
                spent += cost
                updated = apply_repair_to_opportunity(o, parsed)
                ok_after, still = opportunity_ui_ready(o)
                # Restore to Active only when complete — incomplete stays held out.
                if ok_after and o.status == "archived":
                    o.status = "saved"
                    rev = dict(o.raw_evidence or {}) if isinstance(o.raw_evidence, dict) else {}
                    rev.pop("blocked_from_ui_incomplete", None)
                    rev["restored_after_field_repair"] = _utc()
                    o.raw_evidence = rev
                    updated = list(updated) + ["status_restored_saved"]
                db.add(o)
                db.commit()
                summary["repaired"].append(
                    {
                        "id": str(o.id),
                        "sol": o.solicitation_number,
                        "title": (o.title or "")[:80],
                        "updated_fields": updated,
                        "ui_ready_after": ok_after,
                        "still_failing": still,
                        "confidence": parsed.get("confidence"),
                        "openai_usd": float(cost),
                        "evidence_sources": ev["sources"][:8],
                    }
                )
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                summary["failed"].append(
                    {
                        "id": str(o.id),
                        "sol": o.solicitation_number,
                        "error": f"{type(exc).__name__}:{exc}"[:200],
                    }
                )
        summary["openai_spent_usd"] = float(spent)
        summary["finished_at"] = _utc()
        summary["ok"] = True
        out = get_settings().data_root() / "acceptance" / "opportunity_field_repair_last.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
        summary["report"] = str(out)
        return summary
    finally:
        db.close()
