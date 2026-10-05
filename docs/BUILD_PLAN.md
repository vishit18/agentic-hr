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
- [ ] Folder poller (CVS_DIR + INBOX_DIR) -> files -> jobs
- [ ] Safety checks, type detection, PDF/DOCX extraction, OCR fallback
- [ ] Extractor agent + deterministic fallback, normalisation, years computed in code
- [ ] Identity resolution rules + Adjudicator + review items
- [ ] Embeddings + tsvector
- [ ] Time 20 CVs; tune WORKER_CONCURRENCY or the model; record the numbers
- [ ] Candidates list, filters, profile page, upload

## Phase 2: Hiring request loop (5h)
- [ ] Public tokenised e-form + recruiter paste/upload route
- [ ] Requirement Parser, completeness check, owner assignment
- [ ] Filters -> retrieval -> scoring -> Matcher with verified quotes
- [ ] Request page: shortlist, reorder/remove/add, notes, files, next steps
- [ ] Approve and send (preview) -> Mailpit; decision log

## Phase 3: Control and trust (3h)
- [ ] "Needs you" inbox as the home page
- [ ] Merge + undo, unlink auto-linked version
- [ ] Flag a problem -> ticket with logs; engineer view
- [ ] Activity timeline, agent activity panel with fallback banner
- [ ] Reports + Excel export, export-all (7.2), retention setting (7.7), pg_dump backup script (7.6)

## Phase 4: Evaluation (2h)
- [ ] Hand-label ~40 CVs into eval/labels.jsonl (field values + duplicate groups; no raw text)
- [ ] eval/run.py: field accuracy, dedup precision/recall, 3 sample requests with expected top candidates
- [ ] Write up failure cases

## Phase 5: Hardening and UI (3h)
- [ ] Break-it pass: malformed/huge files, prompt-injection CV, XSS in fields, role checks, CSRF
- [ ] Stop Ollama mid-run; confirm fallback and recovery
- [ ] Phone pass over local Wi-Fi; empty, loading and error states; undo toasts
- [ ] Stranger test: someone new uses it with no instructions

## Phase 6: Defence prep (2h)
- [ ] Workflow diagrams (trigger, steps, outcome, actor per step)
- [ ] Requirements table fully filled
- [ ] AWS diagram + monthly cost working
- [ ] Copilot Studio for and against
- [ ] Cleanup script deleting all data after the meeting and the confirmation email to isa@usecadence.tech

## Before the meeting
- [ ] Full ingest of cvs/ done and timed, so the live demo shows new CVs and requests arriving
- [ ] Ollama open, models pulled, laptop plugged in