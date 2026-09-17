"""List local contract classifications — read only."""
from __future__ import annotations

import json
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def main() -> None:
    from ai_funnel import classify_opportunity
    from database import SessionLocal
    from models import Contract, CsvOpportunity

    s = SessionLocal()
    try:
        rows = []
        for c in s.query(Contract).order_by(Contract.id):
            cls, conf = classify_opportunity(c)
            psc = (c.sam_raw or {}).get("classificationCode") if isinstance(c.sam_raw, dict) else None
            rows.append(
                {
                    "id": c.id,
                    "class": cls,
                    "conf": conf,
                    "naics": c.naics_code,
                    "psc": psc,
                    "title": (c.title or "")[:90],
                    "text_len": len(c.attachment_text or ""),
                }
            )
        print(json.dumps({"contracts": rows, "csv": [
            {"id": c.id, "title": (c.title or "")[:90], "naics": c.naics_code}
            for c in s.query(CsvOpportunity).all()
        ]}, indent=2))
    finally:
        s.close()


if __name__ == "__main__":
    main()
