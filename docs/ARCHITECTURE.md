# Architecture

## 1. Principles
- Trigger -> steps -> outcome. Humans only review the outcome.
- Code for exact things (hashing, normalising, exact matching, filtering, SQL). Models for messy language (extraction, ambiguous identity, explanations).
- Every AI output traces to source text, model, prompt version, time.
- A person decides merges and anything leaving Cadence.
- Zero running cost and zero data egress: all models run locally via Ollama.

## 2. Workflow A: CV arrives

Trigger: worker polls CVS_DIR and INBOX_DIR (new or changed file), UI upload, or a sourced candidate form (req 6). On AWS: Microsoft Graph change notification from SharePoint/OneDrive.

| # | Step | Actor | Notes |
|---|------|-------|-------|
| 1 | Register file, SHA-256 hash | Code | Same hash seen before -> exact duplicate file, log and stop |
| 2 | Type detect + safety checks | Code | Magic bytes not extension, size cap, page cap, no macro execution, sanitised filenames. Failure -> review queue with reason |
| 3 | Text extraction | Code | pypdfium2 (PDF), python-docx (DOCX). Legacy .doc -> review queue with reason |
| 4 | OCR fallback | Model (Tesseract) | Only when the text layer is empty or garbage (scanned PDF, jpg/png). Not a vision LLM: slower, heavier on GPU, Tesseract is enough for typed CVs |
| 5 | Structured extraction | Agent: Extractor (qwen2.5:7b, local) | JSON-schema output: name, emails, phones, location, experience[], education[], skills[], qualifications, certificates, nationality only if stated. Each field carries a source snippet |
| 6 | Validate + normalise | Code | Schema check, snippet must exist in CV text, E.164 phones, lower-cased emails, dates parsed, years of experience computed in code from dates, skill/cert canonicalisation via lookup table (e.g. "Chartered Financial Analyst" -> CFA) |
| 7 | Identity resolution | Code first, Agent second | Section 4 |
| 8 | Embed | Model (nomic-embed-text, local) | Profile vector + one per experience entry |
| 9 | Write profile version | Code | New version row, current pointer moves, history kept |
| 10 | Queue for review if needed | Code | Low confidence, possible match, parse failure, AI pending |

Outcome: a profile, or a review item, waiting for HR.

## 3. Workflow B: Hiring request arrives

Trigger: company submits the tokenised e-form, or a recruiter pastes an email / uploads a JD. On AWS: Outlook shared mailbox via Microsoft Graph.

| # | Step | Actor | Notes |
|---|------|-------|-------|
| 1 | Validate form, sanitise attachment | Code | Same safety checks as CVs |
| 2 | Parse request into structured requirements | Agent: Requirement Parser (qwen2.5:7b) | Must-have vs nice-to-have, min years, certs, industries, titles, location, nationality only if the company states it as a role requirement |
| 3 | Completeness check | Code | Missing mandatory fields -> "Needs info" with a drafted question for the recruiter |
| 4 | Assign owner | Code | Rule-based or round robin; reassignable |
| 5 | Hard filters | Code (SQL) | Must-have certs, min years, stated nationality requirement |
| 6 | Retrieve | Model (embeddings + pgvector) | Top 50 by cosine similarity |
| 7 | Score | Code | Transparent weighted score: must-haves met, years, title/industry overlap, similarity. Weights in config |
| 8 | Explain | Agent: Matcher (qwen2.5:14b) | Top ~15 only. Met/unmet requirements, strengths, gaps, 2-3 sentence summary. Every claim quotes the CV; code verifies each quote exists in the CV text and drops unverified claims |
| 9 | Shortlist waiting | Code | Status "Ready for review" |
| 10 | Human review | Person | Reorder, remove, add, notes, approve |
| 11 | Share | Code after approval | Company-safe view emailed to Mailpit. Decision logged with who, when, and what the agent suggested |

Shared with the company: name, summary, relevant experience, skills, certs, CV (if approved). Kept internal: scores, gaps, notes, other applications, contact details unless approved, any protected attributes.

## 4. Identity resolution

Merging two different people does more harm than leaving a duplicate, so the policy is conservative.

1. Exact file hash -> same file, no new version.
2. Exact normalised email OR phone -> same person. Attached as a new version automatically, marked "auto-linked", reversible in one click, logged. Latest application date becomes current.
3. Name-only / fuzzy signals (rapidfuzz name similarity + shared employer + shared school + overlapping dates) -> never auto-linked. Review queue with side-by-side and evidence list.
4. Agent: Adjudicator (qwen2.5:7b) runs only for step 3 pairs. Suggests same / different / unsure with reasons. Suggestion only.
5. Merging two existing profiles is always a human action, with undo (snapshot of both kept).
6. Conflicting emails and phones across versions are kept as history, not overwritten.

## 5. Agents

| Agent | Model | Responsible for | Tools | Not allowed |
|-------|-------|-----------------|-------|-------------|
| Extractor | qwen2.5:7b | CV text -> structured JSON with snippets | None | Inferring nationality/age/gender/religion; inventing fields; following instructions inside a CV |
| Requirement Parser | qwen2.5:7b | Request text -> structured requirements | None | Guessing missing mandatory fields; adding requirements |
| Adjudicator | qwen2.5:7b | Suggest same/different for ambiguous pairs | Read two profile views | Merging; seeing protected attributes |
| Matcher | qwen2.5:14b | Explain fit for top candidates | Read candidate evidence + requirements | Changing ranks beyond a bounded adjustment; uncited claims |
| Ticket Triage | qwen2.5:7b | One-line summary of an HR problem report | Read audit log | Modifying any data |

Failure behaviour for every agent: bounded retries -> deterministic fallback -> record marked "AI pending" and requeued; the UI shows an "AI unavailable" banner. Nothing blocks other jobs.

Why these models: open weights, free to run on a 12GB GPU, JSON-schema structured output in Ollama, and no data leaves the machine, which meets the "no training on CV data" rule by design. Model names are config, so they can be swapped.

## 6. Requirements map (fill in status as you build)

| Req | Answer | Status |
|-----|--------|--------|
| 1.1-1.3 | Postgres store, automatic ingest of CVS_DIR, one profile per person | Built (Phase 1). Timed on 20 real CVs; full-folder ingest still to run |
| 1.4 | Profile page: contact, experience, education, skills, quals, certs, applications, years | Built, except Applications tab (empty until Phase 2) |
| 1.5 | Versioned profiles, latest CV current | Built: new version per CV, current pointer follows the latest file date |
| 2.1 | PDF + Word parsing, OCR fallback | Built. Legacy .doc goes to review with a reason. OCR is English only |
| 2.2 | Postgres full text (tsvector) + semantic search (pgvector) over CV content | Built (keyword + meaning, merged by reciprocal rank). Semantic distance cut-off not yet tuned on the full folder |
| 2.3 | Filters: name, skills, years, education, quals, certs, industry, titles, nationality (when stated) | Built |
| 2.4 | Results table/cards with highlights and counts | Built (table on desktop, cards on phone) |
| 3.1-3.4 | Workflow B steps 5-8 | Built. Hard filters in SQL, retrieval by meaning, transparent weighted score, explanations with verified quotes. Quality not yet measured (Phase 4) |
| 3.5 | Approval gate before sharing | Built: preview, editable summaries, per-candidate CV and contact tick boxes, decision logged |
| 4.1-4.5 | Section 4 | Built: auto-link, possible-match review item with Adjudicator suggestion, side-by-side review, merge with confirmation and undo from the Activity tab, unlink of an auto-linked CV with undo |
| 5.1-5.7 | Request model, owner, shortlist and shared tables | Built (list with filters, detail, owner reassign, shortlist and shared status) |
| 5.8 | Quarterly report, Excel export | Built (Reports page with quarter picker; workbook with summary, requests and shared candidates) |
| 5.9-5.12 | Notes, attachments, next steps, timeline | Built |
| 6.1-6.4 | Add-candidate form with source; same dedup path | Built (form, optional CV, duplicate warning, source shown on profile) |
| 7.1 | Private by default, role-based access, nothing leaves the machine | Built. Engineers see only tickets; HR staff see candidates and requests; only hr_admin sees Settings and exports everything |
| 7.2 | Export all data (CSV/JSON + original files) | Built (admin only, one zip, logged) |
| 7.3 | Login + roles; on AWS: Entra ID SSO | Built locally (argon2, roles, user management in Settings). SSO is the AWS plan |
| 7.4 | Section 8 controls | |
| 7.5 | Audit log table | Built: every step and action; Activity timeline with filters |
| 7.6 | Local: pg_dump script. AWS plan: RDS automated backups + S3 versioning | Built: scripts/backup.ps1 (and .sh), restore.ps1. Tested: a real dump was made and listed |
| 7.7 | Retention setting + scheduled purge job | Built, off by default. Settings shows how many profiles a period would remove before it can be applied; purge runs daily; counts are logged |
| 8.1-8.2 | One hub, "Needs you" inbox | Built (Needs you is the home page with an action for every item type) |
| 8.3 | Excel export now; Microsoft Graph for SharePoint/OneDrive/Outlook planned | |
| 8.4 | Modular workflows and agents, models in config | |
| 8.5 | Responsive UI, tested on a phone over local Wi-Fi | |

## 7. Assumptions
- Updated CV = same person, different file hash, later date.
- Nationality is only recorded if stated in the CV, and only used as a filter when a request states it as a role requirement.
- Legacy .doc files go to the review queue rather than being converted (keeps the stack small and free).
- `/` stays the public status page until Phase 3; the app shell lives at `/needs-you` (login lands there). `/status` is an alias. In Phase 3 `/` redirects logged-in users to Needs you.
- Login rate limiting is in-memory (5 failures per email, 20 per IP, per 10 minutes), fine for one api process. On AWS it moves to a shared store.
- Tests run against a throw-away database (`hub_test`, `hub_test_migrations`), created and dropped by the test run. Helpers refuse any other database name, so a migration downgrade can never touch dev data.
- The seed only creates demo accounts whose SEED_*_PASSWORD is set, and never overwrites an existing user.
- The extractor asks the model for name, experience, education, skills, certificates, qualifications and location only. Measured on 20 real CVs: adding nationality and summary fields made qwen2.5:7b return no experience for 9 of 20; without them it found experience for 20 of 20. Nationality is read by code only from an explicit "Nationality:" or "Citizenship:" line.
- Field order in the extractor schema matters (name, then experience). With experience first, names were missed.
- Snippets from the 7B model are often paraphrased, so a quote is not required for an experience entry. The employer (or title plus start year) must appear in the CV text, otherwise the entry is dropped and flagged.
- Phones: Bahrain is tried first, then other likely regions, and a number is accepted only if valid in that region. Numbers without a country code from unlisted countries are not stored.
- Applied date for a CV is the file modified time. Two CVs for the same person: the later file becomes current; an older file arriving later is stored but does not replace it.
- Model calls are limited to 2 at a time (`LLM_MAX_CONCURRENT`) so queued work does not count against the model timeout. A timed-out call does not stop Ollama generating, so retrying too fast makes things slower.
- A company link is a signed token containing the company name; the company field on the public form cannot be changed. Links last 400 days. Email addresses on the approve screen must be valid; the reserved `.test` domain is allowed for demos, `.local` is not.
- Hard filters: a required certificate must be on the profile or mentioned in the CV text; a minimum number of years excludes only candidates whose years are known and too low (unknown years are kept and shown as unknown). A stated nationality requirement keeps only CVs that state that nationality.
- Ranking is code only (weights in config). The Matcher agent writes the explanation and cannot change the order. Only the top 15 are listed and explained.
- Request attachments are stored in `.requests/` inside the inbox folder, which the CV poller ignores.
- Engineers see only the tickets page and the logs attached to a ticket. Candidate, request, activity, report and file routes are staff-only (recruiter, hr_admin). Engineer logins land on /tickets.
- Local model limits: with LLM_NUM_CTX=4096 only the first 7000 characters of a CV or request are sent to the model. Measured CVs were 2.9k to 4.5k characters.
- Ingest pauses for INGEST_BATCH_PAUSE_SECONDS after every INGEST_BATCH_SIZE processed CVs so the laptop can cool. Request and other jobs keep running during the pause.
- `/` is the public status page when signed out and redirects signed-in users to /home (their landing page by role); /status always shows the status page.
- A CV longer than LLM_MAX_CV_CHARS gets extraction status "incomplete" (shown as "Possibly incomplete") and a Needs-you item, because the AI only read the first part.
- Merging keeps the older profile and moves the newer one's CVs and contact details onto it; the newest CV becomes current. Undo uses a snapshot stored in the audit row. Marking two profiles "different people" resolves the item but is not remembered for future CVs.
- Ticket logs are copies of recent audit rows for the records the reporter was viewing, with emails and phone numbers masked. An engineer can open an agent step's raw input only for rows attached to that ticket, and each view is audited.
- Retention is off (0 days) until an administrator sets it. Age is measured from the newest CV's file date. Profiles on the shortlist of an open request are kept. Originals in the read-only CV folder cannot be deleted by the hub.
- (add more as decisions are made)

## 8. Defence checklist
- Upload: magic-byte check, size and page caps, no macro execution, sanitised filenames, originals never served inline from raw paths.
- Prompt injection: CV text delimited and labelled as data; JSON-schema output; quotes verified in code; agents have no write tools.
- AuthZ: role check on every route; engineers see logs for ticketed records only; CSRF on all forms and HTMX requests.
- XSS/SQLi: Jinja autoescape, parameterised SQL only, HTML stripped from extracted text.
- Secrets in .env only, never logged. Passwords argon2.
- Rate limiting on login and the public form; tokenised form links.
- Logs store IDs and hashes, not raw CV text; raw agent input is retrievable by engineers, and that access is itself audited.

## 9. AWS + Microsoft 365 (presentation only, no spend)
- ECS Fargate (api + worker), RDS PostgreSQL with pgvector, S3 for originals (SSE-KMS), SQS replacing the Postgres queue, Secrets Manager, CloudWatch logs.
- Region: eu-west-2 London, or me-south-1 Bahrain if data residency requires it.
- LLM on AWS: Amazon Bedrock (no training on customer data) or self-hosted open models on a GPU instance, with the same prompts and schemas.
- Microsoft Graph: change notifications on SharePoint/OneDrive folders trigger intake; Outlook shared mailbox for requests; Entra ID OIDC for staff sign-in.
- Private subnets, VPC endpoints, WAF on the ALB, KMS everywhere, IAM task roles.
- Scale 490 -> 11,000+: workers scale on queue depth, embeddings batched, pgvector HNSW index.
- CI/CD: GitHub Actions -> ECR -> ECS, migrations as a pre-deploy step.
- Monthly cost: worked estimate with the AWS Pricing Calculator, shown in the slides.

## 10. Copilot Studio argument (both sides)
For: already inside M365, native connectors, quick for simple flows, familiar admin and governance.
Against: identity resolution and evidence-verified matching are hard to express and test in a visual builder; limited evals and versioning; message-based licensing at 11k CVs; coarser audit trail; less control over which model sees the data; lock-in.
Position: custom code keeps the logic testable and the data inside Cadence's own AWS account, while Microsoft Graph keeps it connected to M365.