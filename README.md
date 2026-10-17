# Agentic HR

An internal tool for an HR team that handles hiring for subsidiary companies. It is a workflow system, not a chatbot: work starts from a trigger and ends as something a person reviews.

1. **A CV arrives** (folder, upload or form): it is read, structured, checked against existing people (new, exact duplicate, updated CV, possible match) and filed as one profile per person with history kept.
2. **A hiring request arrives** (company form, pasted email or uploaded job description): it is turned into requirements, candidates are filtered, ranked and explained, and a shortlist waits for a recruiter. Nothing is shared with a company until a named person approves it.

Everything runs on your own machine at no cost: local AI through Ollama, PostgreSQL, a local mail catcher. No cloud service and no API key is needed, and no CV text leaves the computer.

## What you need
- Docker Desktop
- [Ollama](https://ollama.com) running, with these models pulled:
  ```
  ollama pull qwen2.5:7b
  ollama pull nomic-embed-text
  ```
- A folder of CVs (PDF, DOCX, images). A 8 GB GPU is enough. Without a GPU it works but is slow.

## First run
1. Copy `.env.example` to `.env` and set `POSTGRES_PASSWORD`, `DATABASE_URL` (same password), `APP_SECRET_KEY` and the three `SEED_*_PASSWORD` values. Set `CVS_DIR` to your CV folder.
2. If port 8000 is used by another project, set `API_PORT=8001` (or any free port) in `.env`.
3. Start everything:
   ```
   docker compose up --build
   ```
4. Open `http://localhost:8000` (or your `API_PORT`). The status page shows five checks: database, Ollama, mail catcher and the two folders. All should be green. Then sign in with a seeded account.

| Account | Role | Sees |
|---------|------|------|
| `recruiter@agentic-hr.local` | recruiter | candidates, requests, shortlists, activity |
| `admin@agentic-hr.local` | hr_admin | everything above, plus Settings, users, retention, full export |
| `engineer@agentic-hr.local` | engineer | problem tickets and their logs only |

(Addresses come from `SEED_*_EMAIL` in `.env`. The seed never overwrites an existing user.)

Emails the app sends go to Mailpit at `http://localhost:8025`. They never leave your machine.

## Day to day
| Task | Command |
|------|---------|
| Start in the background | `docker compose up -d` |
| Stop, keep data | `docker compose stop` |
| Logs | `docker compose logs -f api worker` |
| Run the tests | `docker compose run --rm api pytest -q tests` |
| Reset all data | `docker compose down -v` |

`Makefile` has the same as shortcuts (`make start`, `make test`, ...).

**Adding CVs.** Drop files into the CV folder or `storage/inbox`, or use Candidates > Upload. The worker checks every `POLL_SECONDS`. A full ingest of about 500 CVs takes roughly 45 minutes with a cool GPU and up to a few hours if the laptop throttles, so plug it in. After every `INGEST_BATCH_SIZE` CVs the worker pauses for `INGEST_BATCH_PAUSE_SECONDS` to let it cool. Progress is on the Activity page.

**If Ollama is down or slow.** Nothing stops. The app falls back to code (regex contacts, keyword filters, keyword ranking), marks the item "AI pending", shows a banner, and redoes the AI step later.

## Backups
- `.\scripts\backup.ps1` writes `backups/hub-<date>.dump` and keeps the newest 14.
- `.\scripts\restore.ps1 backups\hub-<date>.dump -Yes` replaces the current database.
- Administrators can export everything (people, requests, logs, original files) as one zip from Reports.
- Uploaded files live in `storage/inbox`. Include that folder in your normal file backup.
- `.\scripts\cleanup.ps1` shows what it would delete; `-Confirm` deletes the database, uploads, backups and local labels.

## How it is built
Python 3.12, FastAPI, Jinja2 and HTMX (no JavaScript build step), PostgreSQL 16 with pgvector, a Postgres job queue run by a worker container. Parsing uses pypdfium2, python-docx and Tesseract. Models are named only in config.

- Design and workflows: `docs/ARCHITECTURE.md`, diagrams in `docs/DEFENCE.md`
- Screens: `docs/UI.md`
- Working rules for contributors and AI assistants: `CLAUDE.md`

## Quality checks
- `docker compose run --rm api pytest -q tests`: the automated tests, including a security sweep (`tests/test_phase5_break_it.py`).
- `docker compose run --rm api python /srv/eval/auto.py`: label-free check of the extracted data (counts and filenames only).
- `docker compose run --rm api python /srv/eval/run.py`: accuracy against your own hand labels in `eval/labels.jsonl` (see the top of that file).

## Safety rules the code follows
- A person decides every merge of two profiles and everything sent outside the team. Each decision is logged with who, when and what the AI suggested.
- CV and request text is treated as data, never as instructions. AI answers are checked against a schema, and quotes are verified against the CV text.
- Nationality is only used when the CV states it. Age, gender and religion are never inferred.
- Passwords are hashed with argon2. Secrets live in `.env`, which is git-ignored. CVs, extracted text, embeddings, dumps and logs are never committed.
