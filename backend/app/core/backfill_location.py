"""One-off: fill a missing location on existing profiles using the code-only guess (no AI, no re-extraction).

    docker compose run --rm api python -m app.core.backfill_location          # counts only, changes nothing
    docker compose run --rm api python -m app.core.backfill_location --apply  # writes, and logs one audit row

Prints counts only, never CV text or the locations found.
"""
from __future__ import annotations

import sys

from sqlalchemy import select

from app.core.audit import audit
from app.core.db import SessionLocal
from app.core.models import CandidateVersion
from app.parsing.location import guess_location


def main(apply: bool) -> None:
    with SessionLocal() as s:
        versions = list(s.scalars(select(CandidateVersion).where(
            (CandidateVersion.location.is_(None)) | (CandidateVersion.location == ""))))
        found = 0
        for v in versions:
            loc = guess_location(v.search_text or "")
            if loc:
                found += 1
                if apply:
                    v.location = loc
                    v.data = {**(v.data or {}), "location": loc}
        print(f"versions without a location: {len(versions)}; code found one for: {found}")
        if apply:
            audit(s, actor_type="system", actor_name="backfill-location", action="backfill.location",
                  output={"checked": len(versions), "filled": found})
            s.commit()
            print("applied")
        else:
            print("dry run, nothing changed (use --apply)")


if __name__ == "__main__":
    main("--apply" in sys.argv)
