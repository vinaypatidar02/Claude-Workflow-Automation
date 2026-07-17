#!/usr/bin/env python3
"""
eval_track.py — Quality evaluator for email tracking run output (zero API cost).

Runs after gmail_backfill.py, before sheets_sync.py push.
Surfaces unmatched emails, low-confidence matches, and status anomalies.

Checks (all deterministic, zero Claude API calls):
  T1 — Unmatched email audit: emails in unmatched_emails.json, with fuzzy re-match attempt
  T2 — Low-confidence match quality: company-only matches where multiple entries exist
  T3 — Status anomaly detection: regressions or suspicious status transitions
  T4 — Classifier spot-check: show recent classifications for human confirmation

Usage:
  python3 scripts/eval_track.py
  python3 scripts/eval_track.py --days 2    (restrict to emails from last N days)
"""

import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT          = Path(__file__).parent.parent
UNMATCHED     = ROOT / "data" / "unmatched_emails.json"
TRACKER_PATH  = ROOT / "data" / "job_tracker.json"

sys.path.insert(0, str(Path(__file__).parent))
from eval_base import (
    Issue, company_fuzzy_match, print_report, role_word_overlap,
)

# Pipeline order — used for regression detection (T3)
STATUS_ORDER = {
    "Shortlisted": 0, "Review Needed": 1, "Approved": 2, "Prep Complete": 3,
    "Applied": 4, "Referral": 4, "Under Review": 5, "Interview Scheduled": 6,
    "Assessment": 6, "Offer Received": 7, "Rejected": 8, "Withdrawn": 8,
}
TERMINAL = {"Rejected", "Withdrawn", "Duplicate"}


# ─────────────────────────────────────────────────────────────────────────────
# DATA LOADERS
# ─────────────────────────────────────────────────────────────────────────────

def _load_unmatched() -> list[dict]:
    if not UNMATCHED.exists():
        return []
    raw = json.loads(UNMATCHED.read_text())
    return raw.get("unmatched_emails", []) if isinstance(raw, dict) else raw


def _load_tracker() -> list[dict]:
    if not TRACKER_PATH.exists():
        return []
    raw = json.loads(TRACKER_PATH.read_text())
    return raw.get("applications", []) if isinstance(raw, dict) else raw


def _since(days: int | None) -> str | None:
    if days is None:
        return None
    return (date.today() - timedelta(days=days)).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# T1 — UNMATCHED EMAIL AUDIT
# ─────────────────────────────────────────────────────────────────────────────

def _extract_company_tokens(text: str) -> list[str]:
    """Best-effort company name extraction from email subject/body."""
    # Common patterns: "at Acme", "from Acme", "Acme Hiring", Capitalized words
    patterns = [
        r"(?:at|from|by|with)\s+([A-Z][A-Za-z0-9& ]{2,30}?)(?:\s|[,.])",
        r"([A-Z][A-Za-z0-9]{3,20})\s+(?:Hiring|Recruiting|Careers?|HR|Team)",
    ]
    tokens = []
    for pat in patterns:
        for m in re.finditer(pat, text):
            tokens.append(m.group(1).strip())
    return tokens


def check_t1_unmatched(unmatched: list[dict], tracker: list[dict], since: str | None) -> list[Issue]:
    issues = []
    for email in unmatched:
        logged = email.get("logged_date", "")
        if since and logged < since:
            continue

        subject = email.get("subject", "")
        body    = email.get("body_snippet", "")
        sender  = email.get("sender_email", "")
        reason  = email.get("reason", "")

        # Try a looser fuzzy re-match
        candidates = []
        search_text = subject + " " + body
        tokens = _extract_company_tokens(search_text)

        for app in tracker:
            app_company = app.get("company", "")
            # Direct fuzzy match
            if company_fuzzy_match(app_company, sender.split("@")[-1]):
                candidates.append(app)
                continue
            for tok in tokens:
                if company_fuzzy_match(app_company, tok):
                    candidates.append(app)
                    break

        suggestion = ""
        if candidates:
            best = candidates[0]
            suggestion = (
                f"Possible match: {best.get('id')} — "
                f"{best.get('company')} / {best.get('role')} [{best.get('status')}]"
            )

        issues.append(Issue(
            check_id="T1",
            severity="medium" if not suggestion else "low",
            title="Unmatched email — manual triage needed",
            evidence=(
                f"Logged: {logged}  From: {sender}\n"
                f"Subject: \"{subject}\"\n"
                f"Reason: {reason}\n"
                + (f"Suggestion: {suggestion}" if suggestion else "No tracker match found — may be a new company.")
            ),
            ephemeral_fix={
                "description": suggestion or "No automatic match — check manually in Sheet",
            } if suggestion else None,
            systemic_fix={
                "file": "scripts/gmail_backfill.py",
                "description": (
                    f"Add '{sender.split('@')[-1]}' to ATS_SENDER_DOMAINS list "
                    f"(search for 'ATS_SENDER_DOMAINS = [' near the top of gmail_backfill.py). "
                    f"If the subject pattern is the real gap, add a keyword to SUBJECT_KEYWORDS instead."
                ),
            } if not suggestion else None,
        ))

    return issues


# ─────────────────────────────────────────────────────────────────────────────
# T2 — LOW-CONFIDENCE MATCH QUALITY
# ─────────────────────────────────────────────────────────────────────────────

def check_t2_low_confidence(tracker: list[dict], since: str | None) -> list[Issue]:
    """Surface emails classified via company-only match (confidence='low') where
    multiple tracker entries share the same company — disambiguation needed."""
    issues = []

    # Build company → count map for active entries
    company_entries: dict[str, list[dict]] = {}
    for app in tracker:
        c = (app.get("company") or "").lower().strip()
        if c:
            company_entries.setdefault(c, []).append(app)

    # Scan emails_received for low-confidence matches
    today_str = date.today().isoformat()
    for app in tracker:
        for email in app.get("emails_received", []):
            recv_date = email.get("received_date", "")
            if since and recv_date < since:
                continue
            if email.get("confidence", "high") != "low":
                continue

            company = (app.get("company") or "").lower().strip()
            siblings = company_entries.get(company, [])
            if len(siblings) <= 1:
                continue  # Only one entry for this company — low confidence is fine

            issues.append(Issue(
                check_id="T2",
                severity="medium",
                title=f"Low-confidence email match — {app.get('company')} has {len(siblings)} entries",
                evidence=(
                    f"Email subject: \"{email.get('subject', '')}\"\n"
                    f"Matched to: {app.get('id')} — {app.get('role')} [{app.get('status')}]\n"
                    f"Company has {len(siblings)} tracker entries — role disambiguation may be needed.\n"
                    f"Other entries: " +
                    ", ".join(
                        f"{s.get('id')}/{s.get('role')}" for s in siblings if s.get('id') != app.get('id')
                    )[:120]
                ),
                ephemeral_fix={
                    "description": (
                        f"Verify this email belongs to {app.get('id')} "
                        f"({app.get('role')}) — check email body for role title"
                    ),
                },
                systemic_fix=None,
            ))

    return issues


# ─────────────────────────────────────────────────────────────────────────────
# T3 — STATUS ANOMALY DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def check_t3_status_anomalies(tracker: list[dict], since: str | None) -> list[Issue]:
    """Flag suspicious status transitions in status_history."""
    issues = []

    for app in tracker:
        history = app.get("status_history", [])
        if len(history) < 2:
            continue

        recent = [
            h for h in history
            if not since or h.get("date", "") >= since
        ]

        prev_status = None
        for h in history:
            curr = h.get("status", "")
            if prev_status and curr:
                prev_ord = STATUS_ORDER.get(prev_status, -1)
                curr_ord = STATUS_ORDER.get(curr, -1)

                # Regression: going backward in the pipeline (not terminal)
                if (curr_ord >= 0 and prev_ord >= 0 and curr_ord < prev_ord
                        and curr not in TERMINAL and prev_status not in TERMINAL
                        and h.get("source", "").startswith("email")):
                    issues.append(Issue(
                        check_id="T3",
                        severity="low",
                        title="Status regression from email classifier",
                        evidence=(
                            f"{app.get('id')} — {app.get('company')} / {app.get('role')}\n"
                            f"Transition: {prev_status!r} → {curr!r} (backward in pipeline)\n"
                            f"Source: {h.get('source')} on {h.get('date')}"
                        ),
                        ephemeral_fix=None,
                        systemic_fix={
                            "file": "scripts/gmail_backfill.py",
                            "description": (
                                f"In the status update block (search for 'pipeline_rank' in "
                                f"gmail_backfill.py): verify the guard "
                                f"`pipeline_rank(new_status) > pipeline_rank(current_status)` "
                                f"is applied BEFORE writing. The {prev_status!r} → {curr!r} "
                                f"regression slipped through."
                            ),
                        },
                    ))

                # Suspicious: Rejected arriving when status was Offer Received
                if prev_status == "Offer Received" and curr == "Rejected":
                    issues.append(Issue(
                        check_id="T3",
                        severity="medium",
                        title="Suspicious: Rejected after Offer Received",
                        evidence=(
                            f"{app.get('id')} — {app.get('company')} / {app.get('role')}\n"
                            f"Status: Offer Received → Rejected on {h.get('date')}\n"
                            f"This may be a mismatched email (different role at same company)."
                        ),
                        ephemeral_fix=None,
                        systemic_fix=None,
                    ))

            prev_status = curr

    return issues


# ─────────────────────────────────────────────────────────────────────────────
# T4 — CLASSIFIER ACCURACY SPOT-CHECK  (zero API — reads existing results)
# ─────────────────────────────────────────────────────────────────────────────

def check_t4_spot_check(tracker: list[dict], since: str | None, limit: int = 5) -> list[Issue]:
    """Collect the N most recent email classifications for human review.
    Returns one informational issue per email — not bugs, just confirmation prompts."""
    recent_emails = []

    for app in tracker:
        for email in app.get("emails_received", []):
            recv_date = email.get("received_date", "")
            if since and recv_date < since:
                continue
            recent_emails.append({
                "date":            recv_date,
                "subject":         email.get("subject", ""),
                "sender":          email.get("sender", ""),
                "status_extracted": email.get("status_extracted", ""),
                "company":         app.get("company", ""),
                "role":            app.get("role", ""),
                "job_id":          app.get("id", ""),
            })

    if not recent_emails:
        return []

    recent_emails.sort(key=lambda x: x["date"], reverse=True)
    shown = recent_emails[:limit]

    if not shown:
        return []

    lines = ["Recent email classifications (spot-check — no API cost):"]
    for em in shown:
        lines.append(
            f"  [{em['date']}] {em['status_extracted']!r:<20} "
            f"Subject: \"{em['subject'][:55]}\""
        )
        lines.append(
            f"               Matched: {em['company']} / {em['role']} [{em['job_id']}]"
        )

    return [Issue(
        check_id="T4",
        severity="low",
        title=f"Classifier spot-check — {len(shown)} recent classifications",
        evidence="\n".join(lines),
        ephemeral_fix=None,
        systemic_fix={
            "file": "scripts/gmail_backfill.py",
            "description": (
                "If any classification above looks wrong: search for 'CLASSIFIER_PROMPT' "
                "in gmail_backfill.py and add a corrective few-shot example showing "
                "the correct status for that email type."
            ),
        },
    )]


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Email tracking quality evaluator (zero API cost)")
    parser.add_argument("--days", type=int, default=None,
                        help="Restrict checks to emails received in the last N days")
    args = parser.parse_args()

    since_str = _since(args.days)
    if since_str:
        print(f"[eval_track] Checking emails since {since_str}")

    unmatched = _load_unmatched()
    tracker   = _load_tracker()

    issues: list[Issue] = []
    issues += check_t1_unmatched(unmatched, tracker, since_str)
    issues += check_t2_low_confidence(tracker, since_str)
    issues += check_t3_status_anomalies(tracker, since_str)
    issues += check_t4_spot_check(tracker, since_str)

    print_report(
        issues,
        header=f"Email Tracking{' (last ' + str(args.days) + 'd)' if args.days else ''}",
        checks_desc="T1 (unmatched) · T2 (low-confidence) · T3 (status anomalies) · T4 (spot-check)",
    )

    # T4 is always informational — don't fail on it alone
    non_info = [i for i in issues if i.check_id != "T4"]
    if any(i.severity == "high" for i in non_info):
        sys.exit(2)
    elif non_info:
        sys.exit(1)
    else:
        print("[eval_track] ✓ Track eval passed")
        sys.exit(0)


if __name__ == "__main__":
    main()
