"""Evaluation harness (Phase 4). Scores what the hub extracted, merged and shortlisted against hand labels.

Run inside the api container, after the labelled CVs have been ingested:
    docker compose run --rm api python /srv/eval/run.py --sample 40   # write a blank labels skeleton (filenames only)
    docker compose run --rm api python /srv/eval/run.py               # score against eval/labels.jsonl

labels.jsonl holds one JSON object per line and never any raw CV text. Blank or missing fields are not scored.
  {"kind": "cv", "file": "Jane Doe CV.pdf", "full_name": "Jane Doe", "email": "jane@x.com", "current_title": "Analyst",
   "years_experience": 4, "location": "Doha", "dup_group": "g1"}        # same dup_group = same person; omit if unique
  {"kind": "request", "id": "r1", "company": "Acme", "job_title": "...", "location": "...", "description": "...",
   "must_text": "...", "nice_text": "...", "expected_top": ["Jane Doe CV.pdf", "..."]}
Requests are run inside a transaction that is rolled back, so the live data is never touched.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, "/srv/backend")

from rapidfuzz import fuzz  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.core.db import SessionLocal  # noqa: E402
from app.core.models import Candidate, CandidateIdentifier, CandidateVersion, File, ShortlistItem  # noqa: E402
from app.dedup.identity import norm_name  # noqa: E402

LABELS = Path(__file__).with_name("labels.jsonl")
TOP_K = 10


def load_labels() -> list[dict]:
    if not LABELS.exists():
        sys.exit(f"{LABELS} not found. Create it with --sample first, then fill it in by hand.")
    return [json.loads(line) for line in LABELS.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_skeleton(n: int) -> None:
    with SessionLocal() as s:
        names = sorted({f.original_name for f in s.scalars(select(File).where(File.origin == "cvs_dir"))})
    random.Random(42).shuffle(names)
    rows = [{"kind": "cv", "file": name, "full_name": "", "email": "", "current_title": "",
             "years_experience": None, "location": "", "dup_group": None} for name in sorted(names[:n])]
    target = LABELS.with_name("labels.skeleton.jsonl")
    target.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    print(f"Wrote {len(rows)} filenames to {target}. Open each CV yourself, fill the fields, save as labels.jsonl.")


def final_candidate(s, cid: int) -> int:
    seen = set()
    while cid not in seen:
        seen.add(cid)
        merged = s.scalar(select(Candidate.merged_into_id).where(Candidate.id == cid))
        if merged is None:
            return cid
        cid = merged
    return cid


def version_for_file(s, name: str) -> CandidateVersion | None:
    return s.scalars(select(CandidateVersion).join(File, File.id == CandidateVersion.file_id)
                     .where(File.original_name == name).order_by(CandidateVersion.id.desc())).first()


def field_ok(s, field: str, want, v: CandidateVersion) -> bool:
    if field == "full_name":
        return norm_name(v.full_name) == norm_name(want)
    if field == "email":
        emails = set(s.scalars(select(CandidateIdentifier.value).where(
            CandidateIdentifier.candidate_id == v.candidate_id, CandidateIdentifier.kind == "email")))
        return str(want).strip().lower() in emails
    if field == "current_title":
        return fuzz.token_set_ratio(str(want).lower(), (v.current_title or "").lower()) >= 80
    if field == "years_experience":
        return v.years_experience is not None and abs(v.years_experience - float(want)) <= 1
    if field == "location":
        return fuzz.partial_ratio(str(want).lower(), (v.location or "").lower()) >= 80
    raise ValueError(field)


def score_fields(s, cvs: list[dict]) -> list[str]:
    fields = ["full_name", "email", "current_title", "years_experience", "location"]
    hit = {f: 0 for f in fields}
    total = {f: 0 for f in fields}
    missing, wrong = [], []
    for row in cvs:
        v = version_for_file(s, row["file"])
        if v is None:
            missing.append(row["file"])
            continue
        for f in fields:
            want = row.get(f)
            if want in (None, ""):
                continue
            total[f] += 1
            if field_ok(s, f, want, v):
                hit[f] += 1
            else:
                wrong.append(f"{row['file']}: {f}")
    out = ["## Field accuracy", "| field | correct | labelled | accuracy |", "|---|---|---|---|"]
    for f in fields:
        pct = f"{100 * hit[f] / total[f]:.0f}%" if total[f] else "n/a"
        out.append(f"| {f} | {hit[f]} | {total[f]} | {pct} |")
    if missing:
        out.append(f"\nNot ingested (no profile): {len(missing)} -> {', '.join(missing)}")
    if wrong:
        out.append("\nWrong fields (failure cases to write up):\n" + "\n".join(f"- {w}" for w in wrong))
    return out


def score_dedup(s, cvs: list[dict]) -> list[str]:
    cand: dict[str, int] = {}
    for row in cvs:
        v = version_for_file(s, row["file"])
        if v is not None:
            cand[row["file"]] = final_candidate(s, v.candidate_id)
    files = sorted(cand)
    group = {r["file"]: r.get("dup_group") for r in cvs}
    tp = fp = fn = 0
    false_merge, missed = [], []
    for i, a in enumerate(files):
        for b in files[i + 1:]:
            truth = group[a] is not None and group[a] == group[b]
            pred = cand[a] == cand[b]
            if truth and pred:
                tp += 1
            elif pred:
                fp += 1
                false_merge.append(f"{a} + {b}")
            elif truth:
                fn += 1
                missed.append(f"{a} + {b}")
    prec = f"{100 * tp / (tp + fp):.0f}%" if tp + fp else "n/a"
    rec = f"{100 * tp / (tp + fn):.0f}%" if tp + fn else "n/a"
    out = ["## Deduplication (pairs of labelled CVs)", f"precision {prec} (tp {tp}, fp {fp}), recall {rec} (fn {fn})"]
    if false_merge:
        out.append("False merges (worst kind, one profile for two people):\n" + "\n".join(f"- {x}" for x in false_merge))
    if missed:
        out.append("Missed duplicates (may be waiting in the review queue):\n" + "\n".join(f"- {x}" for x in missed))
    return out


def score_requests(reqs: list[dict]) -> list[str]:
    from app.workflows.hiring_request import RequestInput, create_request, finish_matching, parse_requirements, source_text

    out = ["## Sample requests (expected candidates in the top 10)"]
    for row in reqs:
        with SessionLocal() as s:
            inp = RequestInput(company=row.get("company") or "Eval Co", job_title=row.get("job_title", ""),
                               location=row.get("location", ""), description=row.get("description", ""),
                               must_text=row.get("must_text", ""), nice_text=row.get("nice_text", ""), via="recruiter")
            req = create_request(s, inp)
            text, _n, _notes = source_text(s, req)
            ai = parse_requirements(s, req, text, None)
            finish_matching(s, req, None)
            ranked = [r[0] for r in s.execute(
                select(File.original_name, ShortlistItem.rank).join(CandidateVersion, CandidateVersion.file_id == File.id)
                .join(ShortlistItem, ShortlistItem.candidate_id == CandidateVersion.candidate_id)
                .where(ShortlistItem.request_id == req.id).order_by(ShortlistItem.rank))]
            s.rollback()  # nothing from the evaluation is kept
        want = row.get("expected_top", [])
        top = ranked[:TOP_K]
        found = [w for w in want if w in top]
        pos = {w: (top.index(w) + 1 if w in top else None) for w in want}
        out.append(f"- {row.get('id')}: {len(found)}/{len(want)} expected in top {TOP_K}; positions {pos}; "
                   f"model used: {ai}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, metavar="N", help="write a blank labels skeleton of N filenames")
    args = ap.parse_args()
    if args.sample:
        return write_skeleton(args.sample)
    rows = load_labels()
    cvs = [r for r in rows if r.get("kind") == "cv"]
    reqs = [r for r in rows if r.get("kind") == "request"]
    lines = [f"# Evaluation: {len(cvs)} labelled CVs, {len(reqs)} sample requests", ""]
    with SessionLocal() as s:
        lines += score_fields(s, cvs) + [""] + score_dedup(s, cvs)
    if reqs:
        lines += [""] + score_requests(reqs)
    report = "\n".join(lines)
    print(report)
    Path(__file__).with_name("report.md").write_text(report + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
