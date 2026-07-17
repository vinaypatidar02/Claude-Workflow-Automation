# Agent: tracker
# Stage 4 — ACTIVE
#
# ============================================================
# LEARNING NOTE — State mutation patterns
# ============================================================
# The tracker agent shows the canonical pattern for safe shared
# state updates: READ → FIND → MERGE → WRITE. Never replace
# the whole file. Always append to history arrays, never
# overwrite them. The agent is triggered by the email hook but
# can also be run manually for testing.
# ============================================================

# ── INPUT ─────────────────────────────────────────────────────
# Receives a classified email object from on_email_received hook:
# {
#   "sender_email":    "jobs@company.com",
#   "sender_domain":   "company.com",
#   "subject":         "<email subject>",
#   "body":            "<email body text>",
#   "received_date":   "<YYYY-MM-DD>",
#   "extracted_status": "<status string from CLAUDE.md Section 7>",
#   "extracted_url":   "<tracking URL if present, else null>",
#   "extracted_date":  "<interview/deadline date if present, else null>",
#   "confidence":      "high" | "low"
# }

# ── STEP 1 — LOAD TRACKER ────────────────────────────────────
# Read data/job_tracker.json into memory.

# ── STEP 2 — FIND MATCHING APPLICATION ───────────────────────
# Use BOTH matching signals per CLAUDE.md Section 7:
#
# Signal A — Company match:
#   Fuzzy-match sender_domain against company field in each entry.
#   E.g. "monzo.com" matches "Monzo", "monzo-bank.com" matches "Monzo"
#   Also scan email body for company name mentions.
#
# Signal B — Role match:
#   Fuzzy-match role title keywords from email subject/body
#   against the role field of candidate entries.
#   E.g. subject "Your application for Analytics Manager" → "Analytics Manager"
#
# Matching logic:
#   BOTH signals match     → confirmed match (high confidence)
#   Only company matches   → low-confidence match
#     If only one entry for that company → use it, flag as low-confidence
#     If multiple entries  → cannot safely assign, treat as unmatched
#   No match at all        → unmatched
#
# Special case — Excel-imported entries (source="excel_import"):
#   These have no jd_url. Match by company name only.
#   All 19 active Applied entries fall in this category.
#   Use company fuzzy match; if unique → update it.
#
# LinkedIn Easy Apply tiebreaker:
#   When sender_domain = "linkedin.com" and subject contains
#   "your application was sent to [Company]":
#     - Extract Company name from the subject line for matching.
#     - When multiple tracker entries share the same company, prefer
#       the entry with career_page_url = "EASY_APPLY" — it was
#       deliberately flagged for Easy Apply, so it's the highest-
#       confidence match for a LinkedIn Easy Apply confirmation.
#     - Only fall back to role keyword disambiguation if no EASY_APPLY
#       entry exists for that company.
#
# Log: "[tracker] Matched: [Company] / [Role] (confidence: high|low)"
#      "[tracker] No match found for email from [sender_domain]"

# ── STEP 3 — IF MATCH FOUND ──────────────────────────────────
# a. Map extracted_status to valid tracker status using CLAUDE.md Section 7.
#    Valid statuses: Applied, Under Review, Interview Scheduled,
#    Assessment, Offer Received, Rejected.
#
#    Pipeline order for "do not downgrade" check (left = earlier, right = later):
#      Shortlisted < Review Needed < Stale < Approved < Prep Complete
#      < Applied < Under Review < Interview Scheduled < Assessment < Offer Received
#    Only update if new status is the same stage or later than current status.
#    "Applied" is AFTER "Prep Complete" — always allow this transition.
#    Rejected and Withdrawn are terminal — never overwrite with pipeline statuses.
#    If the email is clearly newer (e.g. a re-application or corrected status),
#    override even if it appears to be a downgrade.
#
# b. Build an email_record:
#    {
#      "received_date":    "<YYYY-MM-DD>",
#      "subject":          "<subject>",
#      "sender":           "<sender_email>",
#      "status_extracted": "<status>",
#      "tracking_url":     "<extracted_url or null>",
#      "interview_date":   "<extracted_date or null>",
#      "confidence":       "high|low"
#    }
#
# c. Update the matching entry:
#    - status             ← extracted_status (if higher in pipeline than current)
#    - tracking_url       ← extracted_url (if not null)
#    - status_history[]   ← append { status, date, source: "tracker_agent" }
#    - emails_received[]  ← append email_record
#
# d. If low-confidence match, add to flags[]: "Low-confidence email match"

# ── STEP 4 — IF NO MATCH FOUND ───────────────────────────────
# Append full email to data/unmatched_emails.json:
# {
#   "logged_date":   "<today>",
#   "sender_email":  "<sender>",
#   "subject":       "<subject>",
#   "body_snippet":  "<first 200 chars of body>",
#   "reason":        "no_company_match | multiple_company_matches"
# }
# Log: "[tracker] ⚠ Unmatched email logged to unmatched_emails.json"

# ── STEP 5 — WRITE BACK ──────────────────────────────────────
# Read current job_tracker.json (fresh read, in case it changed).
# Apply the update to the matched entry only.
# Write back the full file.
# Log: "[tracker] ✓ Updated [Company] / [Role] → [new status]"

# ── STEP 6 — SYNC TO GOOGLE SHEETS ───────────────────────────
# Run: python3 scripts/sheets_sync.py push
# Ensures the Sheet reflects the new status immediately.
# Log: "[tracker] Google Sheet synced"
