# Hook: on_email_received
# Stage 5 — ACTIVE
#
# ============================================================
# EMAIL TRANSPORT: Yahoo IMAP via scripts/gmail_backfill.py
# ============================================================
# Gmail MCP (mcp__claude_ai_Gmail__*) is NOT used for email checking.
# It requires local OAuth that must be re-established each session.
# Instead, all email checking goes through Yahoo IMAP using
# YAHOO_EMAIL and YAHOO_APP_PASSWORD from .env — always available,
# no session auth required.
#
# NEVER use Gmail MCP tools for email reading. Always delegate to
# scripts/gmail_backfill.py.
# ============================================================

# ── GIT SYNC — run FIRST before anything else ─────────────────
# Pull latest tracker state committed by GitHub Actions cron or other sessions:
#   git pull
# If pull has conflicts (unlikely — only json files change), resolve by
# accepting remote changes: git checkout --theirs data/job_tracker.json
# then re-stage and continue.
# ──────────────────────────────────────────────────────────────

# ── TRIGGER ───────────────────────────────────────────────────
# Type: On-demand (say "check email" in Claude Code).
#
# Translate user prompt → script args, then run:
#
#   "check email"                 → python3 scripts/gmail_backfill.py --days 2
#   "check email last 3 days"     → python3 scripts/gmail_backfill.py --days 3
#   "check email last 30 days"    → python3 scripts/gmail_backfill.py --days 30
#   "check email backfill"        → python3 scripts/gmail_backfill.py --days 35
#   "check email last N days"     → python3 scripts/gmail_backfill.py --days N
#   "retry unmatched emails"      → python3 scripts/gmail_backfill.py --retry-unmatched
#
# The script handles everything: IMAP connection, search, classify,
# tracker update, sheets sync, and processed-ID dedup.
# Claude's only job is to parse N from the prompt and run the command.

# ── WHAT THE SCRIPT DOES (for reference) ──────────────────────
# 1. Connects to Yahoo IMAP (imap.mail.yahoo.com:993)
# 2. Searches inbox by subject keywords AND ATS sender domains since N days ago
# 3. Classifies each email via Claude API (not keyword matching)
# 4. Fuzzy-matches each email to job_tracker.json entries
# 5. Updates status (never downgrades), appends to emails_received[]
# 6. Saves processed message IDs to data/processed_email_ids.json
# 7. Logs unmatched to data/unmatched_emails.json
# 8. Syncs to Google Sheet on any status change
# 9. Prints a summary table
#
# Query A — subject keyword search (both modes, only time window differs):
#   newer_than:{days}d -label:job-processed
#   subject:(application OR interview OR offer OR assessment OR
#            "thank you for applying" OR "your application" OR
#            "next steps" OR "technical test" OR unfortunately OR
#            hiring OR vacancy OR role OR position OR recruitment OR recruiter)
# ── EVAL STEP — EMAIL TRACKING QUALITY CHECK ─────────────────
# After gmail_backfill.py completes, run:
#   python3 scripts/eval_track.py --days <N>
#   (use the same --days value as the backfill run, e.g. --days 2)
#
# Checks: T1 (unmatched emails), T2 (low-confidence matches),
#         T3 (status anomalies), T4 (classifier spot-check)
# Cost: zero Claude API calls.
#
# IF exit code 0: log "✓ Track eval passed" and proceed to sheets_sync.py push.
#
# IF exit code 1 or 2 (issues found):
#
#   PART A — Fix this run (no approval needed):
#     T1 (unmatched): if a plausible match is suggested in the report,
#       manually link the email to that tracker entry by updating
#       emails_received[] and status in job_tracker.json using the Edit tool.
#     T2 (low-confidence): if the suggested correct entry differs from the
#       matched one, correct the tracker entry directly with the Edit tool.
#     T3 (status regression): revert the incorrect status change in the
#       tracker using the Edit tool. Remove the bad emails_received entry.
#
#   PART B — Fix the root cause (one issue at a time, with approval):
#     For each issue that carries a systemic_fix in the report:
#       1. Show: "Root cause fix — <file>: <what changes>"
#       2. Ask: "Apply this fix to prevent recurrence? (Y/n)"
#       3. If Y: use Edit tool to make the exact change.
#          Common fixes: add ATS sender domain to ATS_SENDER_DOMAINS,
#          add subject keyword to SUBJECT_KEYWORDS, add company alias
#          to matching logic — all in scripts/gmail_backfill.py.
#       4. After applying: run python3 scripts/check_workflow.py --quick
#
#   Proceed to sheets_sync.py push after both parts are done.
#
# T4 spot-check is informational only — never blocks the run.
# If a classification looks wrong in T4, treat it as a systemic fix:
# add a corrective example to CLASSIFIER_PROMPT in gmail_backfill.py.

# ── GIT COMMIT & PUSH — run AFTER sheets sync ─────────────────
# Commit tracker state changes so CCR cron picks up latest dedup history:
#   git add data/job_tracker.json data/processed_email_ids.json data/unmatched_emails.json
#   git diff --cached --quiet || git commit -m "local: email check $(date +%Y-%m-%d)"
#   git push
# If push is rejected (CCR pushed since your pull), run:
#   git pull --rebase && git push
# ──────────────────────────────────────────────────────────────

# ── IMPLEMENTATION — cron for automatic polling ───────────────
# Add to crontab (crontab -e) for automatic polling every 2 hours:
#   0 */2 * * * cd ~/Projects/job-automation && source .venv/bin/activate && python3 scripts/gmail_backfill.py --days 2 >> logs/email_hook.log 2>&1
#
# First-time full backfill (run once):
#   python3 scripts/gmail_backfill.py --days 35
# Estimated cost: < $0.10 (Haiku 4.5, ~50-80 job emails × $0.001 each)
