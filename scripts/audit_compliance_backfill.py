import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from database import SessionLocal
from models import Contract, ContractAttachment
from sam_enrich import is_sam_metadata_ready
from screening_pipeline import is_dashboard_ready

session = SessionLocal()
rows = session.query(Contract).order_by(Contract.id).all()
print(f"Total contracts: {len(rows)}\n")
for r in rows:
    files = session.query(ContractAttachment).filter_by(contract_id=r.id).count()
    chars = len(r.attachment_text or "")
    meta = is_sam_metadata_ready(r.sam_raw if isinstance(r.sam_raw, dict) else {})
    pursue = (r.analysis or {}).get("pursue") if isinstance(r.analysis, dict) else None
    dash = is_dashboard_ready(r)
    title = (r.title or "")[:44]
    print(
        f"{title:<44} | meta:{str(meta):5} | files:{files:2} | chars:{chars:7} | "
        f"FAR:{(r.subcontracting_limitation_check or '-'):<18} | pursue:{pursue} | dashboard:{dash}"
    )
session.close()
