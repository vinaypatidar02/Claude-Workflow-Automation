#!/usr/bin/env python3
"""
gmail_backfill.py — Email backfill via Yahoo IMAP (imaplib, no external deps).
Searches inbox for job-related emails, classifies via Claude API, updates tracker.

Usage:
    python3 scripts/gmail_backfill.py              # last 2 days (default, normal mode)
    python3 scripts/gmail_backfill.py --days 35    # full backfill (run once to catch history)
    python3 scripts/gmail_backfill.py --dry        # classify only, no writes
    python3 scripts/gmail_backfill.py --retry-unmatched  # re-process previously unmatched emails
"""

import imaplib, email as email_lib, json, sys, re, subprocess
from email.header import decode_header
from pathlib import Path
from datetime import datetime, date, timedelta

ROOT      = Path(__file__).parent.parent
TRACKER   = ROOT / "data" / "job_tracker.json"
PROCESSED = ROOT / "data" / "processed_email_ids.json"

IMAP_HOST = "imap.mail.yahoo.com"
IMAP_PORT = 993

# Subject keywords — broad net; Claude filters out non-job emails downstream
SUBJECT_KEYWORDS = [
    "application", "interview", "offer", "assessment", "thank you for applying",
    "your application", "next steps", "technical test", "unfortunately",
    "not been successful", "not successful", "pleased to offer",
    "on this occasion", "regret to inform", "hiring", "vacancy", "role",
    "position", "job", "recruitment", "recruiter",
    "first step",       # Oracle HCM confirmation: "You've taken the first step"
]

# ATS platform sender domains — fetch ALL emails from these regardless of subject
# Catches Greenhouse/Workday/Lever emails with generic subjects like "Workday Recruiting: Lead Data Analyst"
ATS_SENDER_DOMAINS = [
    "greenhouse.io", "myworkday.com", "myworkdayjobs.com", "lever.co",
    "ashbyhq.com", "smartrecruiters.com", "icims.com", "taleo.net",
    "jobvite.com", "bamboohr.com", "workable.com", "teamtailor.com",
    "successfactors.com", "pinpointhq.com", "recruitee.com",
    "screenloop.io",
    "brassring.com",        # IBM Kenexa BrassRing (Jet2)
    "teamtailor-mail.com",  # Teamtailor client emails (e.g. LEGO Digital Play, Awaze)
    "legodigitalplay.com",  # LEGO Digital Play direct emails (fallback)
    "oraclecloud.com",      # Oracle HCM (large enterprise ATS)
    "oracle.com",           # Oracle HCM fallback sender domain
    "sainsburys.co.uk",     # Sainsbury's Oracle HCM branded sender (myhr.sainsburys.co.uk)
]

# ── Load .env ─────────────────────────────────────────────────────────────────
def load_env() -> dict:
    env = {}
    env_file = ROOT / ".env"
    if not env_file.exists():
        return env
    for line in env_file.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env

# ── Load / save processed message ID log ──────────────────────────────────────
def load_processed() -> set:
    if PROCESSED.exists():
        return set(json.loads(PROCESSED.read_text()).get("ids", []))
    return set()

def save_processed(ids: set):
    PROCESSED.write_text(json.dumps({"ids": sorted(ids)}, indent=2))

# ── Decode MIME header (handles encoded-word =?utf-8?...?=) ──────────────────
def decode_mime_header(value: str) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    result = []
    for part, charset in parts:
        if isinstance(part, bytes):
            result.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(part)
    return " ".join(result)

# ── Extract plain text from email message ────────────────────────────────────
def extract_body(msg) -> str:
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    body = payload.decode(charset, errors="replace")
                    break
        if not body:
            for part in msg.walk():
                ct = part.get_content_type()
                if ct == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        import html as _html
                        charset = part.get_content_charset() or "utf-8"
                        htm = payload.decode(charset, errors="replace")
                        htm = re.sub(r"<style[^>]*>.*?</style>", " ", htm, flags=re.DOTALL | re.IGNORECASE)
                        htm = re.sub(r"<script[^>]*>.*?</script>", " ", htm, flags=re.DOTALL | re.IGNORECASE)
                        htm = re.sub(r"<(?:br|p|div|tr|li|h[1-6])[^>]*>", "\n", htm, flags=re.IGNORECASE)
                        body = _html.unescape(re.sub(r"<[^>]+>", " ", htm))
                        body = re.sub(r"[ \t]+", " ", body)
                        body = re.sub(r"\n\s*\n+", "\n\n", body).strip()
                        break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            body = payload.decode(charset, errors="replace")
    return body.strip()

# ── Build company-name subject searches from active tracker entries ───────────
def _company_subject_terms(since_str: str) -> list[str]:
    """
    Build SUBJECT search terms for company names of active Applied/Under Review/
    Interview Scheduled entries. Catches emails where the company name appears in
    the subject line but no standard keyword (e.g. "Senior DA - Marketing at 9fin").
    """
    try:
        raw = json.loads(TRACKER.read_text())
        entries = raw if isinstance(raw, list) else raw.get("applications", [])
        active_statuses = {"Prep Complete", "Applied", "Referral", "Under Review", "Interview Scheduled", "Assessment"}
        companies = set()
        for e in entries:
            if e.get("status") in active_statuses:
                company = (e.get("company") or "").strip()
                # Use first meaningful token: "9fin" → "9fin", "Simply Business" → "Simply Business"
                if company and len(company) >= 3:
                    companies.add(company)
        return [f'SINCE {since_str} SUBJECT "{c}"' for c in sorted(companies)]
    except Exception:
        return []


# ── Search Yahoo IMAP for emails since a given date ───────────────────────────
def fetch_candidate_emails(imap, since_date: date) -> list:
    """
    Search inbox for emails since since_date. Returns list of parsed email dicts.
    Uses multiple SUBJECT searches to cast a wide net, deduplicates by message ID.
    """
    since_str = since_date.strftime("%d-%b-%Y")
    imap.select("INBOX", readonly=True)

    seen_uids  = set()
    candidates = []

    # Run several targeted searches to stay within IMAP query limits
    search_terms = [
        f'SINCE {since_str} SUBJECT "application"',
        f'SINCE {since_str} SUBJECT "interview"',
        f'SINCE {since_str} SUBJECT "offer"',
        f'SINCE {since_str} SUBJECT "assessment"',
        f'SINCE {since_str} SUBJECT "unfortunately"',
        f'SINCE {since_str} SUBJECT "thank you for applying"',
        f'SINCE {since_str} SUBJECT "your application"',
        f'SINCE {since_str} SUBJECT "not successful"',
        f'SINCE {since_str} SUBJECT "next steps"',
        f'SINCE {since_str} SUBJECT "recruiter"',
        f'SINCE {since_str} SUBJECT "hiring"',
        f'SINCE {since_str} SUBJECT "vacancy"',
        f'SINCE {since_str} SUBJECT "role"',
        f'SINCE {since_str} SUBJECT "position"',
    ]

    def _parse_uid(uid):
        """Fetch and parse one IMAP UID into a candidate dict. Returns None on failure."""
        if uid in seen_uids:
            return None
        seen_uids.add(uid)
        try:
            _, msg_data = imap.fetch(uid, "(RFC822)")
            raw = msg_data[0][1]
            msg = email_lib.message_from_bytes(raw)
            subject  = decode_mime_header(msg.get("Subject", ""))
            sender   = decode_mime_header(msg.get("From", ""))
            date_str = msg.get("Date", "")
            msg_id   = msg.get("Message-ID", uid.decode())
            body     = extract_body(msg)
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(date_str)
                received_date = dt.strftime("%Y-%m-%d")
            except Exception:
                received_date = date.today().isoformat()
            match = re.search(r"<(.+?)>", sender)
            sender_email  = match.group(1).strip() if match else sender.strip()
            sender_domain = sender_email.split("@")[-1] if "@" in sender_email else sender_email
            return {
                "uid":           uid.decode(),
                "msg_id":        msg_id.strip(),
                "subject":       subject,
                "sender_email":  sender_email,
                "sender_domain": sender_domain,
                "received_date": received_date,
                "body":          body,
            }
        except Exception as e:
            print(f"  ⚠ Could not parse message uid={uid}: {e}")
            return None

    for term in search_terms:
        try:
            status, data = imap.search(None, term)
        except Exception:
            status = "NO"
        if status != "OK" or not data[0]:
            continue
        for uid in data[0].split():
            parsed = _parse_uid(uid)
            if parsed:
                candidates.append(parsed)

    # Also search by ATS sender domains — catches emails with generic subjects
    # e.g. "Workday Recruiting: Lead Data Analyst" or "PMG Hiring Team Update"
    print(f"[hook] Searching {len(ATS_SENDER_DOMAINS)} ATS sender domains...")
    ats_found = 0
    for ats_domain in ATS_SENDER_DOMAINS:
        term = f'SINCE {since_str} FROM "{ats_domain}"'
        try:
            status, data = imap.search(None, term)
        except Exception:
            status = "NO"
        if status != "OK" or not data[0]:
            continue
        for uid in data[0].split():
            parsed = _parse_uid(uid)
            if parsed:
                candidates.append(parsed)
                ats_found += 1
    if ats_found:
        print(f"[hook] ATS sender search added {ats_found} additional email(s)")

    # Company-name subject search — catches emails with role-title subjects that
    # contain the company name (e.g. "Senior DA - Marketing at 9fin").
    company_terms = _company_subject_terms(since_str)
    company_found = 0
    for term in company_terms:
        try:
            status, data = imap.search(None, term)
        except Exception:
            status = "NO"
        if status != "OK" or not data[0]:
            continue
        for uid in data[0].split():
            parsed = _parse_uid(uid)
            if parsed:
                candidates.append(parsed)
                company_found += 1
    if company_found:
        print(f"[hook] Company-name subject search added {company_found} additional email(s)")

    return candidates

# ── Reuse classifier and matching logic from test_email_tracker.py ─────────────
sys.path.insert(0, str(ROOT / "scripts"))
from test_email_tracker import (
    classify_email_claude, find_match, update_tracker, pipeline_rank
)


# ── Retry previously unmatched emails ─────────────────────────────────────────
def retry_unmatched(env: dict, dry_run: bool):
    """Re-fetch and re-classify emails from unmatched_emails.json that have a stored uid.
    Uses the improved HTML-stripping pipeline — resolves matches missed on first pass."""
    unmatched_path = ROOT / "data" / "unmatched_emails.json"
    if not unmatched_path.exists():
        print("[retry] No unmatched_emails.json found — nothing to retry.")
        return

    unmatched_log = json.loads(unmatched_path.read_text())
    entries       = unmatched_log.get("unmatched_emails", [])
    retriable     = [e for e in entries if e.get("uid")]
    skipped       = [e for e in entries if not e.get("uid")]

    if not retriable:
        print(f"[retry] {len(entries)} unmatched entries found but none have a uid field.")
        print("[retry] Run a fresh backfill to capture UIDs for future retries.")
        return

    print(f"[retry] Retrying {len(retriable)} unmatched email(s) with improved HTML pipeline...")
    if skipped:
        print(f"[retry] Skipping {len(skipped)} older entries without uid (run fresh backfill to re-capture).")

    yahoo_email = env.get("YAHOO_EMAIL", "")
    yahoo_pass  = env.get("YAHOO_APP_PASSWORD", "")
    if not yahoo_email or not yahoo_pass:
        print("ERROR: YAHOO_EMAIL and YAHOO_APP_PASSWORD must be set in .env")
        return

    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        imap.login(yahoo_email, yahoo_pass)
        imap.select("INBOX", readonly=True)
    except Exception as e:
        print(f"ERROR: IMAP login failed — {e}")
        return

    tracker  = json.loads(TRACKER.read_text())
    APPLICATIONS_TAB_STATUSES = {
        "Shortlisted", "Review Needed", "Approved", "Prep Complete",
        "Applied", "Referral", "Under Review", "Interview Scheduled", "Assessment", "Offer Received",
    }
    all_apps = [a for a in tracker["applications"]
                if a.get("status") in APPLICATIONS_TAB_STATUSES]
    TERMINAL_STATUSES = {"Rejected", "Withdrawn"}
    processed_ids = load_processed()
    newly_processed = set()

    resolved      = []
    still_unmatched = list(skipped)  # preserve entries without uid

    for entry in retriable:
        uid    = entry["uid"]
        msg_id = entry.get("msg_id", "")
        print(f"\n[retry] uid={uid} | {entry.get('subject','')[:65]}")

        try:
            _, msg_data = imap.fetch(uid.encode(), "(RFC822)")
            raw = msg_data[0][1]
            msg = email_lib.message_from_bytes(raw)
            subject  = decode_mime_header(msg.get("Subject", ""))
            sender_h = decode_mime_header(msg.get("From", ""))
            body     = extract_body(msg)
            m        = re.search(r"<(.+?)>", sender_h)
            sender_email  = m.group(1).strip() if m else sender_h.strip()
            sender_domain = sender_email.split("@")[-1] if "@" in sender_email else sender_email
        except Exception as e:
            print(f"  ⚠ Could not re-fetch: {e}")
            still_unmatched.append(entry)
            continue

        result = classify_email_claude(subject, body)
        if not result.get("is_job_related") or result.get("status") == "Not Relevant":
            print(f"  → Still not job-related after retry")
            updated = dict(entry)
            updated["reason"] = "retry_not_job_related"
            updated["last_retry"] = date.today().isoformat()
            still_unmatched.append(updated)
            continue

        status = result["status"]
        matched_app, confidence = find_match(
            all_apps, sender_email, sender_domain, subject, body,
            company_name_from_claude=result.get("company_name"),
            role_title_from_claude=result.get("role_title"),
            job_reference_id_from_claude=result.get("job_reference_id"),
            tracking_url_from_claude=result.get("tracking_url"),
        )

        if not matched_app:
            print(f"  ✗ Still unmatched ({confidence})")
            updated = dict(entry)
            updated["reason"] = f"retry_failed:{confidence}"
            updated["last_retry"] = date.today().isoformat()
            still_unmatched.append(updated)
            continue

        company = matched_app.get("company", "?")
        role    = matched_app.get("role", "?")
        current = matched_app.get("status", "?")
        print(f"  ✓ Matched: {company} / {role}  (conf={confidence})  {current} → {status}")

        email_record = {
            "received_date":    entry.get("logged_date", date.today().isoformat()),
            "subject":          subject,
            "sender":           sender_email,
            "status_extracted": status,
            "tracking_url":     result.get("tracking_url"),
            "confidence":       confidence,
            "source":           "retry_unmatched",
        }

        if not dry_run:
            fresh = json.loads(TRACKER.read_text())
            if current not in TERMINAL_STATUSES and pipeline_rank(status) > pipeline_rank(current):
                update_tracker(matched_app["id"], status, email_record, fresh)
            else:
                for app in fresh["applications"]:
                    if app["id"] == matched_app["id"]:
                        app.setdefault("emails_received", []).append(email_record)
                        break
                fresh["applications"] = fresh["applications"]
                TRACKER.write_text(json.dumps(fresh, indent=2, ensure_ascii=False))
            tracker  = json.loads(TRACKER.read_text())
            all_apps = tracker["applications"]

        resolved.append(entry)
        if msg_id:
            newly_processed.add(msg_id)

    imap.logout()

    if not dry_run:
        unmatched_log["unmatched_emails"] = still_unmatched
        existing_resolved = unmatched_log.get("resolved_emails", [])
        unmatched_log["resolved_emails"] = existing_resolved + resolved
        unmatched_path.write_text(json.dumps(unmatched_log, indent=2))
        if newly_processed:
            save_processed(processed_ids | newly_processed)

    print(f"\n[retry] Resolved: {len(resolved)}  |  Still unmatched: {len(still_unmatched)}")

    if not dry_run and resolved:
        result = subprocess.run(
            ["python3", "scripts/sheets_sync.py", "push"],
            capture_output=True, text=True, cwd=ROOT
        )
        if result.returncode == 0:
            print("[retry] Google Sheet synced")
        else:
            print(f"[retry] ⚠ Sheet sync failed: {result.stderr[:100]}")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    args    = sys.argv[1:]
    dry_run = "--dry" in args
    days    = 2   # default: 48 hours (use --days 35 for full backfill)
    if "--days" in args:
        days = int(args[args.index("--days") + 1])

    env = load_env()

    # Retry mode — re-process previously unmatched emails that have a stored uid
    if "--retry-unmatched" in args:
        print(f"\n{'='*60}")
        print(f"  Email Backfill — Retry Unmatched  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        print(f"  Mode: {'DRY RUN — ' if dry_run else ''}retry-unmatched  |  Yahoo IMAP")
        print(f"{'='*60}\n")
        retry_unmatched(env, dry_run)
        return

    print(f"\n{'='*60}")
    print(f"  Email Backfill — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Mode: {'DRY RUN — ' if dry_run else ''}last {days} days  |  Yahoo IMAP")
    print(f"{'='*60}\n")
    yahoo_email = env.get("YAHOO_EMAIL", "")
    yahoo_pass  = env.get("YAHOO_APP_PASSWORD", "")
    if not yahoo_email or not yahoo_pass:
        print("ERROR: YAHOO_EMAIL and YAHOO_APP_PASSWORD must be set in .env")
        sys.exit(1)

    processed_ids = load_processed()

    # Connect to Yahoo IMAP
    print(f"[hook] Connecting to Yahoo IMAP ({IMAP_HOST})...")
    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        imap.login(yahoo_email, yahoo_pass)
    except Exception as e:
        print(f"ERROR: IMAP login failed — {e}")
        print("Check YAHOO_APP_PASSWORD in .env (generate at security.yahoo.com → App passwords)")
        sys.exit(1)
    print("[hook] Connected OK")

    since_date = date.today() - timedelta(days=days)
    print(f"[hook] Searching inbox since {since_date} ...")
    candidates = fetch_candidate_emails(imap, since_date)
    imap.logout()
    print(f"[hook] Found {len(candidates)} candidate email(s)")

    if not candidates:
        print("\n  No job-related emails found in the search window.")
        print(f"{'='*60}\n")
        return

    tracker     = json.loads(TRACKER.read_text())
    APPLICATIONS_TAB_STATUSES = {
        "Shortlisted", "Review Needed", "Approved", "Prep Complete",
        "Applied", "Referral", "Under Review", "Interview Scheduled", "Assessment", "Offer Received",
    }
    # Only match against active Applications-tab entries — mirrors Sheet behaviour.
    # Stale, Rejected, Withdrawn, Duplicate are excluded; email check never updates them.
    all_apps    = [a for a in tracker["applications"]
                   if a.get("status") in APPLICATIONS_TAB_STATUSES]
    TERMINAL_STATUSES = {"Rejected", "Withdrawn"}

    stats = {"scanned": 0, "skipped_processed": 0, "job_related": 0,
             "matched": 0, "updated": 0, "already_current": 0, "unmatched": 0}

    unmatched_path = ROOT / "data" / "unmatched_emails.json"
    unmatched_log  = (json.loads(unmatched_path.read_text())
                      if unmatched_path.exists() else {"unmatched_emails": []})

    newly_processed = set()

    for item in candidates:
        msg_id   = item["msg_id"]
        subject  = item["subject"]
        body     = item["body"]
        sender   = item["sender_email"]
        domain   = item["sender_domain"]
        rcvd     = item["received_date"]

        # Skip already-processed (normal mode; backfill re-reads all)
        if msg_id in processed_ids and "--backfill" not in args:
            stats["skipped_processed"] += 1
            continue

        stats["scanned"] += 1
        print(f"\n[{stats['scanned']}] {rcvd} — {sender[:45]}")
        print(f"  Subject: {subject[:70]}")

        # Classify
        result = classify_email_claude(subject, body)
        if not result.get("is_job_related") or result.get("status") == "Not Relevant":
            print(f"  → Not job-related ({result.get('notes','')[:60]})")
            newly_processed.add(msg_id)
            continue

        status = result["status"]
        url    = result.get("tracking_url")
        print(f"  → Classified: {status}  |  {result.get('notes','')[:60]}")
        stats["job_related"] += 1

        # Match to tracker — pass full pool + Claude-extracted signals
        matched_app, confidence = find_match(
            all_apps, sender, domain, subject, body,
            company_name_from_claude=result.get("company_name"),
            role_title_from_claude=result.get("role_title"),
            job_reference_id_from_claude=result.get("job_reference_id"),
            tracking_url_from_claude=result.get("tracking_url"),
        )

        if not matched_app:
            print(f"  ✗ No match ({confidence})")
            stats["unmatched"] += 1
            if not dry_run:
                unmatched_log["unmatched_emails"].append({
                    "uid":          item["uid"],
                    "msg_id":       msg_id,
                    "logged_date":  date.today().isoformat(),
                    "sender_email": sender,
                    "subject":      subject,
                    "body_snippet": body[:200],
                    "reason":       confidence or "no_company_match"
                })
            newly_processed.add(msg_id)
            continue

        company = matched_app.get("company", "?")
        role    = matched_app.get("role", "?")
        current = matched_app.get("status", "?")
        print(f"  ✓ Matched: {company} / {role}  (conf={confidence})")
        stats["matched"] += 1

        email_record = {
            "received_date":    rcvd,
            "subject":          subject,
            "sender":           sender,
            "status_extracted": status,
            "tracking_url":     url,
            "confidence":       confidence
        }

        # Safety guard — terminal statuses are not in the active pool but guard against edge cases
        if current in TERMINAL_STATUSES:
            print(f"    ⚠ Matched terminal app ({current}) — email logged, status unchanged. "
                  f"If you re-applied to this role, update {matched_app.get('id')} manually.")
            stats["already_current"] += 1
            if not dry_run:
                for app in tracker["applications"]:
                    if app["id"] == matched_app["id"]:
                        app.setdefault("emails_received", []).append(email_record)
                        break
                TRACKER.write_text(json.dumps(tracker, indent=2, ensure_ascii=False))
            newly_processed.add(msg_id)
            continue

        print(f"    {current} → {status}")
        if pipeline_rank(status) <= pipeline_rank(current):
            print(f"    ℹ No upgrade ('{status}' ≤ '{current}') — email recorded")
            stats["already_current"] += 1
            if not dry_run:
                for app in tracker["applications"]:
                    if app["id"] == matched_app["id"]:
                        app.setdefault("emails_received", []).append(email_record)
                        break
                TRACKER.write_text(json.dumps(tracker, indent=2, ensure_ascii=False))
        else:
            if not dry_run:
                tracker = json.loads(TRACKER.read_text())
                changed = update_tracker(matched_app["id"], status, email_record, tracker)
                tracker  = json.loads(TRACKER.read_text())
                all_apps = [a for a in tracker["applications"]
                            if a.get("status") in APPLICATIONS_TAB_STATUSES]
                if changed:
                    print(f"    ✓ Tracker updated")
                    stats["updated"] += 1
            else:
                print(f"    [dry] Would update → {status}")
                stats["updated"] += 1

        newly_processed.add(msg_id)

    # Persist processed IDs and unmatched log
    if not dry_run:
        save_processed(processed_ids | newly_processed)
        if unmatched_log["unmatched_emails"]:
            unmatched_path.write_text(json.dumps(unmatched_log, indent=2))

    # Sync to Google Sheets if anything changed
    if not dry_run and stats["updated"] > 0:
        print(f"\n[hook] Syncing to Google Sheet...")
        result = subprocess.run(
            ["python3", "scripts/sheets_sync.py", "push"],
            capture_output=True, text=True, cwd=ROOT
        )
        if result.returncode == 0:
            print("[hook] Google Sheet synced")
        else:
            print(f"[hook] ⚠ Sheet sync failed: {result.stderr[:100]}")

    print(f"\n{'='*60}")
    print(f"  Email Backfill — {date.today()}  |  Yahoo IMAP")
    print(f"{'='*60}")
    print(f"  Emails scanned:      {stats['scanned']}")
    print(f"  Job-related:         {stats['job_related']}")
    print(f"  Matched:             {stats['matched']}")
    print(f"  Status updated:      {stats['updated']}")
    print(f"  Already up-to-date:  {stats['already_current']}")
    print(f"  Unmatched:           {stats['unmatched']}  (see data/unmatched_emails.json)")
    print(f"{'='*60}\n")
    if stats["unmatched"] > 0:
        print("  Next: check data/unmatched_emails.json for emails that didn't match.")
    if stats["updated"] > 0:
        print("  Next: open Google Sheet to review updated statuses.")

if __name__ == "__main__":
    main()
