# Hook: on_job_approved
# Stage 5 — ACTIVE
#
# ============================================================
# LEARNING NOTE — PostToolUse hooks in Claude Code
# ============================================================
# Claude Code hooks fire in response to events within a session.
# PostToolUse fires AFTER Claude successfully uses a tool — in
# this case, after sheets_sync.py pull writes job_tracker.json.
#
# The hook inspects the written file and conditionally triggers
# application_prep if qualifying entries exist.
#
# Hook type:   PostToolUse
# Watches:     Write tool on data/job_tracker.json
# Wired via:   .claude/settings.json (see Implementation section)
# ============================================================

# ── TRIGGER CONDITION ─────────────────────────────────────────
# Fires after any write to data/job_tracker.json.
# Most commonly triggered by: python3 scripts/sheets_sync.py pull
# (which you run after editing the Google Sheet)

# ── STEP 1 — INSPECT WRITTEN FILE ────────────────────────────
# Read data/job_tracker.json.
# Find all entries where ALL THREE conditions are true:
#   a. status = "Approved"
#   b. career_page_url is not null and not empty string
#   c. resume_path is null  (prep not already done)
#
# TERMINAL STATUS EXCLUSION — skip any entry where status is:
#   "Withdrawn", "Rejected", "Applied", "Under Review",
#   "Interview Scheduled", "Assessment", "Offer Received"
# These are terminal or post-approval states. Even if career_page_url
# is set and resume_path is null (e.g. a manually-closed application),
# NEVER trigger prep for them. Only "Approved" is valid here.
#
# WHY ALL THREE:
#   (a) alone → could fire before career_page_url is filled
#   (b) alone → career_page_url might exist on a non-approved job
#   (c) prevents re-running prep for already-prepared jobs
#       and makes repeated pull runs completely safe
#
# If zero qualifying entries → exit silently (no logging needed)
# Log only when action is taken.

# ── STEP 2 — CONFIRM BEFORE RUNNING ──────────────────────────
# For each qualifying entry, print a confirmation prompt:
#   "Ready to prepare application for:
#    Company:        <company>
#    Role:           <role>
#    Career page:    <career_page_url>
#    ATS type:       <ats_type>
#    Fit score:      <fit_score>
#    Work mode:      <work_mode>
#    Proceed? [Y/n]"
#
# This is intentional — application prep generates PDFs and
# consumes API tokens. One human confirmation per application
# is a sensible gate, especially early in the workflow.
# (Can be set to auto-confirm once you trust the pipeline)

# ── STEP 3 — INVOKE APPLICATION_PREP ────────────────────────
# If confirmed (or auto-confirm is set):
#   Invoke agents/application_prep.md for each qualifying entry.
#   Pass the full entry object as context.
#
# The application_prep agent handles:
#   - Fetching JD text from jd_url
#   - Selecting resume variant
#   - Invoking tailor_resume skill
#   - Invoking draft_cover_letter skill
#   - Calling pdf_renderer.py for both PDFs
#   - Writing output files to outputs/applications/
#   - Updating job_tracker.json (status → "Prep Complete")
#   - Running sheets_sync.py push to update Google Sheet
#
# Process entries sequentially, not in parallel.
# Log progress per entry.

# ── STEP 4 — COMPLETION SUMMARY ───────────────────────────────
# After all qualifying entries are processed:
#   ═══════════════════════════════════════════════
#    Application Prep Complete — <datetime>
#   ═══════════════════════════════════════════════
#    Prepared:  X applications
#    Location:  outputs/applications/
#    Next step: Review PDFs, then submit via career page URLs
#               (career_page_url in job_tracker.json or Sheet)
#   ═══════════════════════════════════════════════

# ── IMPLEMENTATION — .claude/settings.json ────────────────────
# Add this to your project's .claude/settings.json file:
# (create the file at job-automation/.claude/settings.json)
#
# {
#   "hooks": {
#     "PostToolUse": [
#       {
#         "matcher": "Write",
#         "hooks": [
#           {
#             "type": "command",
#             "command": "Check job_tracker.json for newly approved jobs and run application prep if found"
#           }
#         ]
#       }
#     ]
#   }
# }
#
# LEARNING NOTE — Hook matchers:
# "Write" matches when Claude uses the Write file tool.
# The command string becomes a new prompt to Claude in the
# same session. Claude then reads this hook file, inspects
# the tracker, and decides whether to invoke application_prep.
#
# Alternative manual trigger (without hooks config):
#   claude "Check for approved jobs and run application prep"

# ── IDEMPOTENCY GUARANTEE ─────────────────────────────────────
# Running this hook multiple times is always safe because:
#   - resume_path = null check prevents double-prep
#   - The hook exits silently if no qualifying entries exist
#   - sheets_sync.py push is idempotent (re-push = same result)
# You can run pull → hook fires → nothing qualifies → no harm done.
