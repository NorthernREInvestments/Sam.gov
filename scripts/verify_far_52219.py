"""Verify FAR 52.219-14 is not checked on any contract with stored attachment text."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from attachment_pipeline import (
    _find_applicable_subcontracting_match,
    check_subcontracting_limitation,
    rerun_subcontracting_check,
)
from database import SessionLocal
from models import Contract

session = SessionLocal()
rows = session.query(Contract).order_by(Contract.id).all()

found_any = False
print("=== Independent re-check (fresh logic, not cached DB field) ===\n")

for r in rows:
    text = r.attachment_text or ""
    if len(text) < 500:
        print(f"SKIP (no text): {(r.title or '')[:50]}")
        continue

    fresh = check_subcontracting_limitation(text)
    match = _find_applicable_subcontracting_match(text)
    stored = r.subcontracting_limitation_check

    status = "OK"
    if fresh.check == "FOUND":
        status = "*** FOUND (CHECKED) ***"
        found_any = True
    elif fresh.check != stored:
        status = f"MISMATCH stored={stored} fresh={fresh.check}"

    print(f"{(r.title or '')[:50]}")
    print(f"  stored={stored}  fresh={fresh.check}  {status}")
    if match:
        print(f"  match_reason={match[2]}")
        safe = match[1][:120].encode("ascii", "backslashreplace").decode()
        print(f"  line={safe}")
    else:
        # show unchecked lines if clause mentioned
        for line in text.splitlines():
            low = line.lower()
            if "52.219-14" in low or "limitations on subcontracting" in low:
                safe = line.strip()[:120].encode("ascii", "backslashreplace").decode()
                print(f"  clause_line={safe}")
    print()

print("=== Summary ===")
if found_any:
    print("AT LEAST ONE CONTRACT HAS CHECKED 52.219-14")
else:
    print("NO contract has checked/applicable 52.219-14 in stored attachment text.")

session.close()
