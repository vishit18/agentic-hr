# UI spec

Audience: HR staff who live in Outlook and SharePoint. No training. Must work on a phone.
Tech: Jinja2 templates in frontend/templates, HTMX for partial updates and polling, one CSS file. No JS framework, no build step.

## Layout
- Desktop: Outlook-style 3 panes. Left nav (folders), middle list, right reading pane.
- Mobile (<768px): left nav becomes a bottom tab bar; list and detail are separate screens with a back button. Tap targets >= 44px.
- Top bar: global search, "Agent activity" indicator, user menu, "Flag a problem" button (always visible).
- Calm neutral palette, one accent colour, status pills (Needs review = amber, Ready = blue, Approved = green, Failed = red). Use the variables in app.css.

## Left nav
1. Needs you (count badge): everything waiting on a human
2. Hiring requests
3. Candidates
4. Activity
5. Reports
6. Tickets (engineer role only)
7. Settings (hr_admin only: users, retention)

## Screens

### Login
Email + password. Rate-limited. Clear error text.

### Needs you (home)
Inbox list of review items, newest first, grouped by type:
- Shortlist ready for approval
- Possible duplicate
- Low-confidence extraction
- File could not be read (with reason)
- Request missing information
Each row: type pill, one-line summary, age, owner. Clicking opens the item in the reading pane with its action buttons.

### Candidates
- Search box (full text + semantic) and filter chips: skills, years of experience (range), education, qualifications, certificates, industry, job title, nationality (only shown if stated), source.
- Result count always visible ("37 candidates with CFA").
- Results: table on desktop, cards on mobile. Columns: name, current title, years, top skills, certs, last updated, source.
- Matching terms highlighted.
- "Add candidate" (sourced from LinkedIn/referral): name, contact, source, source URL/notes, optional CV upload. Runs the same dedup check before saving and shows "possible match" if found.

### Candidate profile
Header: name, contact details, location, years of experience, source, "auto-linked" badge if applicable.
Tabs: Overview (summary, skills, certs, qualifications) | Experience | Education | CV versions (all files, latest marked current, open original) | Applications (requests they were shortlisted or shared for) | Activity (audit timeline).
Every extracted field shows its source snippet on tap/hover. "Edit" for hr_admin. "Unlink this CV" on auto-linked versions (undo).

### Duplicate review
Side by side: existing profile vs incoming CV. Matching fields highlighted, conflicts marked. Evidence list ("Same email", "Name 92% similar", "Same employer 2023-2025"). Agent suggestion with reasons, clearly labelled as a suggestion.
Buttons: Same person (merge) | Different people | Not sure, leave. Merge asks for confirmation and is undoable from the Activity tab.

### Hiring requests
List with status pills: New, Needs info, Shortlisting, Ready for review, Shared, Closed. Filters: company, owner, status, date.
Request detail:
- Header: company, job title, received date, owner (reassign dropdown), status.
- Requirements: parsed must-haves and nice-to-haves, editable. Link to the original text/attachment.
- Shortlist: ranked cards. Each card: name, score, met requirements (green ticks), missing (red), strengths, 2-3 sentence summary, evidence quotes from the CV. Actions: remove, move up/down, add candidate (search), internal note.
- "Approve and send" opens a preview of exactly what the company will receive (company-safe view), then sends to Mailpit and logs the decision.
- Notes, files, next steps, timeline (5.9-5.12).

### Company e-form (public, no login)
Opened through a tokenised link per company. Mobile friendly. Fields: company, contact name/email, job title, location, number of roles, description (free text), must-have requirements, nice-to-haves, nationality requirement (only if the role legally requires it, with a justification field), start date, attachment (JD). Confirmation page with a reference number.

### Agent activity
Live list (HTMX poll every 3s): job type, item, step, status, duration. "AI unavailable, using fallback" banner when Ollama is down. Clicking a job shows input/output (engineers see full detail; recruiters see a plain-English summary).

### Activity
Audit timeline filterable by candidate, request, user, agent.

### Reports
Quarter picker. Shows: requests received, open, closed, by company, by owner, candidates shortlisted, shared, time to shortlist. "Export to Excel" and "Export all data" (7.2).

### Flag a problem
Modal from any page with one text box ("What looks wrong?"). Auto-attaches the current page, record IDs, recent audit rows and agent inputs/outputs. Creates a ticket. Confirmation toast.

### Tickets (engineer)
List and detail with the HR message, linked records and the attached logs. Status: open, in progress, fixed. The fix note is visible back to the reporter.

## Patterns
- Undo toast for every reversible action (8 seconds).
- Empty states explain what will appear and why ("No shortlists yet. When a company submits a request, a shortlist will appear here.").
- Loading states via HTMX indicators; never a blank screen.
- Dates shown relative, with the absolute date on hover.
- Plain English everywhere. No model jargon in recruiter views.