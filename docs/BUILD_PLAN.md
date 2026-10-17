# Build plan (24h)

Rule: cover all seven problems end to end before polishing anything. Vertical slices. Commit after each phase with tests green and the status page green.

## Phase 0: Foundations (1-1.5h)
Already done: compose, Dockerfile, config, status page, worker heartbeat, base template, CSS, smoke tests.
- [x] Confirm `docker compose up --build` is green on the status page
- [x] SQLAlchemy setup + Alembic in backend/ (alembic.ini, migrations/), first migration enables `CREATE EXTENSION IF NOT EXISTS vector`
- [x] Tables: users, files, candidates, candidate_versions, candidate_embeddings, requests, request_requirements, shortlist_items, review_items, audit_log, jobs, tickets, notes, attachments
- [x] Auth (argon2, signed session cookie, CSRF), roles, idempotent seed at app/core/seed.py
- [x] Postgres job queue + worker loop (SKIP LOCKED, WORKER_CONCURRENCY, retries, dead-letter -> review item)
- [x] Ollama client: chat with JSON schema, embed, timeouts, retries, health
- [x] Audit helper used by every step
- [x] App shell: 3-pane desktop, bottom tabs on mobile, login page

## Phase 1: CV intake loop (5h)
- [x] Folder poller (CVS_DIR + INBOX_DIR) -> files -> jobs
- [x] Safety checks, type detection, PDF/DOCX extraction, OCR fallback
- [x] Extractor agent + deterministic fallback, normalisation, years computed in code
- [x] Identity resolution rules + Adjudicator + review items
- [x] Embeddings + tsvector
- [x] Time 20 CVs; tune WORKER_CONCURRENCY or the model; record the numbers
  - 20 real CVs, qwen2.5:7b + nomic-embed-text, 4 worker threads, 2 concurrent model calls (`LLM_MAX_CONCURRENT`).
  - GPU cool: 20 CVs in about 113 s (about 5.7 s per CV, extraction about 26 s each with 4 waiting in the queue). Full folder (488): about 46 min.
  - GPU hot (86 C, throttling): 20 CVs in about 400 s (about 20 s per CV). Full folder: up to about 2.7 h. Start the full ingest well before the meeting, with the laptop plugged in and cool.
  - The GPU reports 8 GB of VRAM, not 12 GB. qwen2.5:14b (about 9 GB) will not fit fully on the GPU: check its speed in Phase 2 and switch LLM_MODEL_RERANK to the 7b if explanations are too slow.
  - Embedding: about 0.1 s per CV when the model is loaded.
  - Findings that changed the design: see ARCHITECTURE.md section 7 (extractor schema, concurrency, phone regions).
- [x] Candidates list, filters, profile page, upload

## Phase 2: Hiring request loop (5h)
- [x] Public tokenised e-form + recruiter paste/upload route
- [x] Requirement Parser, completeness check, owner assignment
- [x] Filters -> retrieval -> scoring -> Matcher with verified quotes
- [x] Request page: shortlist, reorder/remove/add, notes, files, next steps
- [x] Approve and send (preview) -> Mailpit; decision log

## Phase 3: Control and trust (3h)
- [x] "Needs you" inbox as the home page
- [x] Merge + undo, unlink auto-linked version
- [x] Flag a problem -> ticket with logs; engineer view
- [x] Activity timeline, agent activity panel with fallback banner
- [x] Reports + Excel export, export-all (7.2), retention setting (7.7), pg_dump backup script (7.6)

## Phase 4: Evaluation (2h)
- [ ] Hand-label ~40 CVs into eval/labels.jsonl (optional: needs a person to read the CVs; `run.py --sample 40` writes the skeleton)
- [x] eval/run.py: field accuracy, dedup precision/recall, 3 sample requests with expected top candidates (runs once labels exist)
- [x] eval/auto.py: label-free grounding and consistency check, counts and filenames only (see eval/auto_report.md)
- [ ] Write up failure cases

## Phase 5: Hardening and UI (3h)
- [x] Break-it pass: tests/test_phase5_break_it.py (route auth sweep, XSS, NUL bytes, oversized fields, hostile search). Found and fixed: NUL byte 500s, /status and /health leaking internals to signed-out visitors
- [x] Stop Ollama mid-run; confirm fallback and recovery: tested live on real data with the AI unreachable (keyword shortlist of 15, template explanations, flagged AI pending); recovery covered by tests
- [~] Phone pass: layout checked at 390 px (fixed stacked Needs-you rows, tap targets, sideways scroll). Empty and error states are tested for every page; undo exists for merges, unlinks and shortlist removal. Still to do on a real phone over Wi-Fi
- [ ] Stranger test: someone new uses it with no instructions (checklist and scoring sheet in docs/STRANGER_TEST.md; needs a person)

## Phase 6: Defence prep (2h)
- [x] Workflow diagrams (trigger, steps, outcome, actor per step): docs/DEFENCE.md sections 1-2
- [x] Requirements table fully filled: ARCHITECTURE.md section 6 (every row now has an answer; 8.3 and 8.5 are partial)
- [-] AWS diagram and cost: dropped (local only for now)
- [x] Copilot Studio for and against: DEFENCE.md section 3
- [x] Cleanup script (scripts/cleanup.ps1, dry run by default) and confirmation email draft (docs/CLEANUP_EMAIL.md, not sent)

## Before the meeting
- [ ] Full ingest of cvs/ done and timed, so the live demo shows new CVs and requests arriving
- [ ] Ollama open, models pulled, laptop plugged in