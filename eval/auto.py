"""Label-free evaluation: checks the hub's output against the CV text it already holds, using code only.

    docker compose run --rm api python /srv/eval/auto.py

Prints counts and file names only, never CV text or personal values. This measures grounding and consistency, not
true accuracy: a field can be consistent and still wrong. Use eval/run.py with hand labels for real accuracy.
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, "/srv/backend")

from sqlalchemy import func, select  # noqa: E402

from app.core.db import SessionLocal  # noqa: E402
from app.core.models import Candidate, CandidateIdentifier, CandidateVersion, File, ReviewItem  # noqa: E402
from app.dedup.identity import norm_name  # noqa: E402


def words(text: str | None) -> set[str]:
    return set(re.findall(r"[a-z]{2,}", (text or "").lower()))


def main() -> None:
    out: list[str] = ["# Label-free evaluation", ""]
    with SessionLocal() as s:
        versions = list(s.scalars(select(CandidateVersion)))
        total = len(versions)
        out.append(f"Versions: {total}; extraction status: " + ", ".join(
            f"{k}={v}" for k, v in s.execute(select(CandidateVersion.extraction_status, func.count())
                                              .group_by(CandidateVersion.extraction_status)).all()))

        # 1. Grounding: every extracted value should be traceable to the CV text
        checks = defaultdict(lambda: [0, 0])  # name -> [ok, checked]
        ungrounded: dict[str, list[str]] = defaultdict(list)
        fname = {f.id: f.original_name for f in s.scalars(select(File))}
        for v in versions:
            text = (v.search_text or "").lower()
            tw = words(v.search_text)
            if v.full_name:
                ok = all(w in tw for w in norm_name(v.full_name).split())
                checks["name appears in CV text"][1] += 1
                checks["name appears in CV text"][0] += ok
                if not ok:
                    ungrounded["name"].append(fname.get(v.file_id, "?"))
            if v.current_title:
                tt = words(v.current_title)
                ok = bool(tt) and len(tt & tw) / len(tt) >= 0.6
                checks["title appears in CV text"][1] += 1
                checks["title appears in CV text"][0] += ok
                if not ok:
                    ungrounded["title"].append(fname.get(v.file_id, "?"))
            for sk in v.skills or []:
                checks["skill appears in CV text"][1] += 1
                checks["skill appears in CV text"][0] += sk.lower() in text
            if v.years_experience is not None:
                checks["years in 0-50"][1] += 1
                checks["years in 0-50"][0] += 0 <= v.years_experience <= 50
        out += ["", "## Grounding", "| check | ok | checked | rate |", "|---|---|---|---|"]
        for k, (ok, n) in checks.items():
            out.append(f"| {k} | {ok} | {n} | {100 * ok / n:.0f}% |" if n else f"| {k} | 0 | 0 | n/a |")
        for k, files in ungrounded.items():
            out.append(f"\nNot grounded ({k}), {len(files)} files, first 10: " + "; ".join(files[:10]))

        # 2. Completeness: how many profiles lack the fields recruiters filter on
        out += ["", "## Missing fields (share of versions)"]
        for label, col in [("full_name", CandidateVersion.full_name), ("current_title", CandidateVersion.current_title),
                           ("location", CandidateVersion.location), ("years_experience", CandidateVersion.years_experience)]:
            n = s.scalar(select(func.count()).select_from(CandidateVersion).where(col.is_(None)))
            out.append(f"- {label}: {n}/{total} missing")
        no_id = s.scalar(select(func.count()).select_from(Candidate).where(
            Candidate.merged_into_id.is_(None),
            ~Candidate.id.in_(select(CandidateIdentifier.candidate_id))))
        out.append(f"- candidates with no email or phone: {no_id}")

        # 3. Dedup sanity
        live = list(s.scalars(select(Candidate).where(Candidate.merged_into_id.is_(None))))
        cur = {c.id: s.get(CandidateVersion, c.current_version_id) for c in live if c.current_version_id}
        by_name: dict[str, list[int]] = defaultdict(list)
        for cid, v in cur.items():
            if v and v.full_name:
                by_name[norm_name(v.full_name)].append(cid)
        same_name = {n: ids for n, ids in by_name.items() if n and len(ids) > 1}
        queued = s.scalar(select(func.count()).select_from(ReviewItem).where(
            ReviewItem.type.like("%match%"), ReviewItem.status == "open"))
        out += ["", "## Dedup sanity",
                f"- live profiles: {len(live)}; merged away: {s.scalar(select(func.count()).select_from(Candidate).where(Candidate.merged_into_id.is_not(None)))}",
                f"- names shared by more than one live profile (possible missed duplicates): {len(same_name)}",
                f"- open possible-match review items: {queued}"]
        multi = [(c, s.scalar(select(func.count()).select_from(CandidateVersion).where(CandidateVersion.candidate_id == c.id)))
                 for c in live]
        out.append(f"- profiles with more than one version (updated CVs caught): {sum(1 for _, n in multi if n > 1)}")
        # merged or auto-linked profiles whose versions disagree on the name = possible false merge
        suspicious = 0
        for c, n in multi:
            if n < 2:
                continue
            names = {norm_name(v.full_name) for v in s.scalars(select(CandidateVersion).where(CandidateVersion.candidate_id == c.id)) if v.full_name}
            if len(names) > 1 and not any(a in b or b in a for a in names for b in names if a != b):
                suspicious += 1
        out.append(f"- multi-version profiles whose names look unrelated (possible false merge): {suspicious}")

    report = "\n".join(out)
    print(report)
    Path(__file__).with_name("auto_report.md").write_text(report + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
