"""Plain-English labels for audit actions, used by every timeline in the UI."""
from __future__ import annotations

ACTION_LABELS: dict[str, str] = {
    # requests
    "request.received": "Request received", "request.form_submitted": "Submitted through the company form",
    "agent.parse_request": "Requirements read", "request.owner_assigned": "Owner assigned automatically",
    "request.owner_changed": "Owner changed", "request.needs_info": "More information needed",
    "request.shortlist_ready": "Shortlist ready for review", "request.shared": "Shortlist approved and sent",
    "request.requirement_added": "Requirement added", "request.requirement_edited": "Requirement edited",
    "request.requirement_removed": "Requirement removed", "request.rematch": "Shortlist update requested",
    "request.candidate_removed": "Candidate removed from shortlist", "request.candidate_restored": "Candidate restored",
    "request.candidate_added": "Candidate added by hand", "request.candidate_moved": "Shortlist reordered",
    "request.closed": "Request closed", "request.reopened": "Request reopened", "request.note_added": "Note added",
    "agent.matcher": "Candidate explanation written",
    # candidates and files
    "file.registered": "CV file received", "file.exact_duplicate": "Exact copy of a file already held (ignored)",
    "file.safety": "File checked", "file.text": "Text read from the file", "agent.extract": "Details read from the CV",
    "identity.resolved": "Checked against existing profiles", "version.written": "Profile updated",
    "agent.embed": "Search index updated", "agent.adjudicate": "AI suggested whether two profiles match",
    "file.rejected": "File could not be accepted", "file.unreadable": "File could not be read",
    "file.uploaded": "File uploaded", "file.downloaded": "Original file downloaded",
    "candidate.added_manually": "Candidate added by hand", "candidate.merged": "Profiles merged",
    "candidate.merge_undone": "Merge undone", "candidate.version_unlinked": "CV unlinked into its own profile",
    "candidate.unlink_undone": "Unlink undone", "review.resolved": "Review item handled",
    # system
    "job.done": "Background task finished", "job.dead": "Background task failed",
    "login.ok": "Signed in", "login.failed": "Failed sign-in", "logout": "Signed out",
    "ticket.created": "Problem reported", "ticket.updated": "Ticket updated", "agent.triage": "Problem summarised",
    "export.all": "Everything exported", "export.report": "Report exported", "retention.purge": "Old records purged",
    "retention.changed": "Retention period changed", "user.created": "User created", "user.updated": "User changed",
    "company_link.created": "Company link created", "agent_input.viewed": "Agent input viewed by engineer",
}


def action_label(action: str | None) -> str:
    return ACTION_LABELS.get(action or "", (action or "").replace(".", " ").replace("_", " ").capitalize())
