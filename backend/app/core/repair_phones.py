"""One-off repair: remove "phone numbers" that are really date ranges, and list auto-links that relied only on them.

    docker compose exec api python -m app.core.repair_phones           # report only
    docker compose exec api python -m app.core.repair_phones --apply   # delete the bogus identifiers, one audit row

It never unlinks or merges anything: a person decides that (profile page > unlink). Prints ids and counts only.
"""
from __future__ import annotations

import sys

import phonenumbers
from sqlalchemy import select

from app.core.audit import audit
from app.core.db import SessionLocal
from app.core.models import AuditLog, CandidateIdentifier
from app.parsing.normalise import _DATE_DIGITS


def bogus_value(value: str) -> bool:
    try:
        return bool(_DATE_DIGITS.fullmatch(str(phonenumbers.parse(value).national_number)))
    except phonenumbers.NumberParseException:
        return False


def main(apply: bool) -> None:
    with SessionLocal() as s:
        phones = list(s.scalars(select(CandidateIdentifier).where(CandidateIdentifier.kind == "phone")))
        bad = [i for i in phones if bogus_value(i.value)]
        bad_values = {i.value for i in bad}
        only_bogus = []
        for row in s.scalars(select(AuditLog).where(AuditLog.action == "identity.resolved")):
            out = row.output or {}
            evidence = out.get("evidence") or []
            if out.get("action") == "auto_link" and evidence and all(
                    e.startswith("Same phone: ") and e.removeprefix("Same phone: ") in bad_values for e in evidence):
                only_bogus.append((row.entity_id, out.get("candidate_id")))
        print(f"phone identifiers: {len(phones)}; date-like: {len(bad)} on {len({i.candidate_id for i in bad})} profiles")
        print(f"auto-links that relied ONLY on date-like phones: {len(only_bogus)} (version id, profile id): {only_bogus}")
        if apply:
            for i in bad:
                s.delete(i)
            audit(s, actor_type="system", actor_name="repair-phones", action="repair.phones",
                  output={"removed": len(bad), "auto_links_to_check": [list(x) for x in only_bogus]})
            s.commit()
            print("removed the date-like identifiers")
        else:
            print("report only, nothing changed (use --apply)")


if __name__ == "__main__":
    main("--apply" in sys.argv)
