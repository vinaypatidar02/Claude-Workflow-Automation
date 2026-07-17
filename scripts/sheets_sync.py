#!/usr/bin/env python3
"""
sheets_sync.py — Bidirectional sync: job_tracker.json ↔ Google Sheet
======================================================================
LEARNING NOTE — Why a sync layer rather than replacing job_tracker.json?

Agents need fast, offline, atomic reads/writes → local JSON.
You need a human-readable, clickable, editable view → Google Sheets.
A sync layer gives both. Agents write to JSON; this script pushes
to Sheets so you can see and edit. When you edit Sheets (status,
career_page_url, notes), this script pulls those changes back to JSON.

Two modes:
  push  — JSON → Sheets  (run after scrape/score/enrich)
  pull  — Sheets → JSON  (run before application prep, to pick up
                           your edits: status=Approved, career_page_url)

Usage:
  python3 scripts/sheets_sync.py push
  python3 scripts/sheets_sync.py pull

Setup (one-time, see README in this file):
  1. Create Google Cloud project, enable Sheets API + Drive API
  2. Create Service Account, download JSON key → save as
     data/google_service_account.json
  3. Create a new Google Sheet, share it with the service account email
  4. Copy the Sheet ID from the URL into .env as GOOGLE_SHEET_ID

Sheet structure (one row per application):
  Col A: id               (internal — for workflow matching, not for sharing)
  Col B: reference        (Company — Role, shareable with referral contacts)
  Col C: location         (read-only)
  Col D: posted_date      (read-only)
  Col E: fit_score        (read-only, populated after Stage 3)
  Col F: salary_stated    (read-only)
  Col G: experience_req   (read-only)
  Col H: status           ← YOU EDIT THIS — change to "Approved" when ready
                            NOTE: only change to Approved AFTER filling Col J
  Col I: jd_url           (read-only — hyperlink to LinkedIn/Adzuna JD)
  Col J: career_page_url  ← PASTE ATS URL HERE FIRST (before setting Approved)
  Col K: notes            ← YOU CAN ADD NOTES / referral contact name here
  Col L: apply_recommendation   (read-only)
  Col M: visa_sponsorship_status (read-only)
  Col N: actual_hiring_company   (read-only)
  Col O: agency_name      (read-only)
  Col P: company_sponsor_kb (read-only)
  Col Q–AD: secondary columns (job_id, company, role, work_mode, applied_date,
            ats_type, is_contract, tracking_url, source, match_exists,
            matched_entry_id, score_exists, latest_scoring_date, adzuna_salary_stated)

  IMPORTANT EDIT ORDER:
    1. Paste career_page_url in Col J
    2. THEN change status to "Approved" in Col H
    3. THEN run: python3 scripts/sheets_sync.py pull
    application_prep agent will only fire when BOTH are present.
"""

import json, re, sys, os
from pathlib import Path
from datetime import datetime

ROOT          = Path(__file__).parent.parent
TRACKER       = ROOT / "data" / "job_tracker.json"
AUTO_REJ_FILE = ROOT / "data" / "auto_rejected.json"
SA_FILE       = ROOT / "data" / "google_service_account.json"
ENV_FILE      = ROOT / ".env"

# ── Load env vars ─────────────────────────────────────────────────────────────
env = {}
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()

SHEET_ID = env.get("GOOGLE_SHEET_ID", "")

# ── Validate setup ────────────────────────────────────────────────────────────
def check_setup():
    errors = []
    if not SA_FILE.exists():
        errors.append(
            f"Service account file not found: {SA_FILE}\n"
            "  → Follow setup steps in this file's docstring"
        )
    if not SHEET_ID:
        errors.append(
            "GOOGLE_SHEET_ID not set in .env\n"
            "  → Create a Google Sheet, copy its ID from the URL\n"
            "    (the long string between /d/ and /edit)\n"
            "  → Add to .env: GOOGLE_SHEET_ID=your_sheet_id_here"
        )
    if errors:
        print("\n[sheets_sync] Setup incomplete:")
        for e in errors: print(f"  ✗ {e}")
        sys.exit(1)

# ── Connect to Google Sheets ──────────────────────────────────────────────────
def get_sheet():
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        print("\n[sheets_sync] Missing dependencies. Install with:")
        print("  pip install gspread google-auth")
        sys.exit(1)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds  = Credentials.from_service_account_file(str(SA_FILE), scopes=scopes)
    gc     = gspread.authorize(creds)
    return gc.open_by_key(SHEET_ID)

# ── Column definitions ────────────────────────────────────────────────────────
HEADERS = [
    # ── Priority columns (always visible) ──────────────────────────────────────
    "id",                    # Col A — internal workflow ID (app_001 etc.)
    "reference",             # Col B — Company — Role (shareable with referral contacts)
    "location",              # Col C
    "posted_date",           # Col D
    "fit_score",             # Col E — populated after Stage 3
    "salary_stated",         # Col F
    "experience_req",        # Col G
    "status",                # Col H ← USER EDITABLE — set to "Approved" AFTER filling Col J
    "jd_url",                # Col I — hyperlink to LinkedIn/Adzuna JD
    "career_page_url",       # Col J ← USER EDITABLE — paste ATS URL here first (before Approved)
    "notes",                 # Col K ← USER EDITABLE
    "apply_recommendation",  # Col L — Apply / Maybe / Skip (Claude synthesis)
    "visa_sponsorship_status",  # Col M
    "actual_hiring_company", # Col N — real employer for agency posts
    "agency_name",           # Col O — LinkedIn poster (recruiter/agency)
    "company_sponsor_kb",    # Col P — Known sponsor / Not a known sponsor / Uncertain
    # ── Secondary columns ──────────────────────────────────────────────────────
    "job_id",                # Col Q — LinkedIn/Adzuna job ID from scrape
    "company",               # Col R
    "role",                  # Col S
    "work_mode",             # Col T
    "applied_date",          # Col U
    "ats_type",              # Col V
    "is_contract",           # Col W
    "tracking_url",          # Col X — from application confirmation emails
    "source",                # Col Y — apify/adzuna/excel_import/manual_inject
    "match_exists",          # Col Z — was a matching entry found on last scout run?
    "matched_entry_id",      # Col AA — id of related entry (new_entry decisions only)
    "score_exists",          # Col AB — is fit_score populated?
    "latest_scoring_date",   # Col AC — ISO date of last Pass 2 score
    "adzuna_salary_stated",  # Col AD — raw Adzuna API salary (Adzuna-sourced jobs only)
    "market",                # Col AE — uk | nl | se
]

# Columns the user is allowed to edit — pulled back during pull
USER_EDITABLE = {"status", "career_page_url", "notes"}

# Statuses that belong in the Archive tab, not the Applications tab
ARCHIVED_STATUSES = {"Rejected", "Auto-Rejected", "Withdrawn", "Stale", "Duplicate"}

ARCHIVE_HEADERS = [
    # ── Priority columns ───────────────────────────────────────────────────────
    "id",                    # Col A
    "reference",             # Col B — Company — Role (NEW)
    "location",              # Col C
    "posted_date",           # Col D
    "fit_score",             # Col E
    "salary_stated",         # Col F
    "experience_req",        # Col G — (NEW)
    "status",                # Col H
    "rejection_stage",       # Col I
    "rejection_reason",      # Col J
    "jd_url",                # Col K
    "career_page_url",       # Col L
    "notes",                 # Col M
    "apply_recommendation",  # Col N — Apply / Maybe / Skip
    "visa_sponsorship_status",  # Col O
    "actual_hiring_company", # Col P
    "agency_name",           # Col Q
    "company_sponsor_kb",    # Col R — Known sponsor / Not a known sponsor / Uncertain
    # ── Secondary columns ──────────────────────────────────────────────────────
    "company",               # Col S
    "role",                  # Col T
    "source",                # Col U
    "matched_entry_id",      # Col V
    "adzuna_salary_stated",  # Col W — raw Adzuna salary (Adzuna-sourced jobs only)
    "market",                # Col X — uk | nl | se
]

def _salary_cell(app: dict) -> str:
    """Build Col F display value: stated salary, or compact estimate when absent."""
    stated     = app.get("salary_stated", "") or ""
    estimate   = app.get("salary_estimate", "") or ""
    confidence = app.get("salary_estimate_confidence", "") or ""
    if stated.startswith("Not stated (est."):
        return stated
    missing = not stated or stated == "Not stated"
    if not missing:
        return stated
    if estimate:
        market = app.get("market", "uk")
        if market == "nl":
            compact = re.sub(r'€(\d+),000', lambda m: f'€{m.group(1)}k', estimate)
        elif market == "se":
            compact = re.sub(r'(SEK\s+)(\d+),000', lambda m: f'{m.group(1)}{m.group(2)}k', estimate)
        else:
            compact = re.sub(r'£(\d+),000', lambda m: f'£{m.group(1)}k', estimate)
        suffix  = f" (est. · {confidence})" if confidence else " (est.)"
        return compact + suffix
    return stated or ""


def app_to_row(app: dict) -> list:
    """Convert a job_tracker.json application entry to a sheet row."""
    company = app.get("company", "")
    role    = app.get("role", "")
    ref     = f"{company} — {role}"
    jd  = app.get("jd_url", "") or ""
    cp  = app.get("career_page_url", "") or ""
    # Embed HYPERLINK formulas so batch update (USER_ENTERED) renders them clickable
    # — eliminates the need for per-cell update_cell() calls that hit write quotas.
    jd_cell = f'=HYPERLINK("{jd}","View JD")' if (jd and jd.startswith("http")) else (jd or "")
    cp_cell = f'=HYPERLINK("{cp}","Apply")'   if (cp and cp.startswith("http")) else (cp or "")
    return [
        # ── Priority columns ─────────────────────────────────────────────────
        app.get("id", ""),                          # 0  id
        ref,                                         # 1  reference
        app.get("location", ""),                     # 2  location
        app.get("posted_date", ""),                  # 3  posted_date
        app.get("fit_score", ""),                    # 4  fit_score
        _salary_cell(app),                           # 5  salary_stated
        app.get("experience_req", "") or "",         # 6  experience_req
        app.get("status", ""),                       # 7  status
        jd_cell,                                     # 8  jd_url
        cp_cell,                                     # 9  career_page_url
        app.get("notes", "") or "",                  # 10 notes
        app.get("apply_recommendation", "") or "",   # 11 apply_recommendation
        app.get("visa_sponsorship_status", ""),      # 12 visa_sponsorship_status
        app.get("actual_hiring_company") or "",      # 13 actual_hiring_company
        app.get("agency_name") or "",                # 14 agency_name
        app.get("company_sponsor_kb", "") or "",     # 15 company_sponsor_kb
        # ── Secondary columns ─────────────────────────────────────────────────
        app.get("job_id", ""),                       # 16 job_id
        app.get("company", ""),                      # 17 company
        app.get("role", ""),                         # 18 role
        app.get("work_mode", ""),                    # 19 work_mode
        app.get("applied_date", "") or "",           # 20 applied_date
        app.get("ats_type", "") or "",               # 21 ats_type
        str(app.get("is_contract") or ""),           # 22 is_contract
        (f'=HYPERLINK("{app["tracking_url"]}","Track")'
         if app.get("tracking_url", "") and str(app.get("tracking_url","")).startswith("http")
         else ""),                                    # 23 tracking_url
        app.get("source", "") or "",                 # 24 source
        str(app.get("match_exists", "")) if app.get("match_exists") is not None else "",  # 25 match_exists
        app.get("matched_entry_id", "") or "",       # 26 matched_entry_id
        str(app.get("score_exists", "")) if app.get("score_exists") is not None else "",  # 27 score_exists
        app.get("latest_scoring_date", "") or "",    # 28 latest_scoring_date
        app.get("adzuna_salary_stated", "") or "",   # 29 adzuna_salary_stated
        app.get("market", "uk") or "uk",             # 30 market
    ]

# ─────────────────────────────────────────────────────────────────────────────
# PUSH helpers
# ─────────────────────────────────────────────────────────────────────────────

def _read_sheet_statuses(wb) -> dict:
    """Return {id: status} from the Applications tab (fast single read).
    Used by push() to detect entries the user manually archived in the Sheet
    before running pull — so we don't overwrite their edits.
    """
    out = {}
    try:
        ws = wb.worksheet("Applications")
        rows = ws.get_all_values()
        id_col     = 0
        status_col = HEADERS.index("status")  # 7
        for row in rows[1:]:
            if row and len(row) > status_col and row[id_col]:
                out[row[id_col]] = row[status_col]
    except Exception:
        pass
    return out


def _compute_rejection_stage(entry: dict, is_auto_rej_file: bool = False) -> str:
    """Classify why/how an entry ended up in the Archive tab."""
    if is_auto_rej_file or entry.get("status") == "Auto-Rejected":
        return "auto_rejected"
    status = entry.get("status", "")
    if status == "Withdrawn":
        return "withdrawn"
    if status == "Stale":
        return "stale"
    if status == "Duplicate":
        return "duplicate"
    return "post_application"  # Rejected after human review at any pipeline stage


def _archive_row_from_tracker(app: dict) -> list:
    rejection_reason = ""
    for h in reversed(app.get("status_history", [])):
        reason = h.get("note") or h.get("reason")
        if reason:
            rejection_reason = reason
            break
    jd = app.get("jd_url") or ""
    jd_cell = f'=HYPERLINK("{jd}","View JD")' if jd.startswith("http") else jd
    cp = app.get("career_page_url") or ""
    cp_cell = f'=HYPERLINK("{cp}","Apply")' if cp.startswith("http") else cp
    return [
        # ── Priority columns ─────────────────────────────────────────────────
        app.get("id", ""),                                              # 0  id
        f"{app.get('company', '')} — {app.get('role', '')}",           # 1  reference (NEW)
        app.get("location", ""),                                        # 2  location
        app.get("posted_date", ""),                                     # 3  posted_date
        app.get("fit_score", ""),                                       # 4  fit_score
        _salary_cell(app),                                              # 5  salary_stated
        app.get("experience_req", "") or "",                            # 6  experience_req (NEW)
        app.get("status", ""),                                          # 7  status
        _compute_rejection_stage(app),                                  # 8  rejection_stage
        rejection_reason,                                               # 9  rejection_reason
        jd_cell,                                                        # 10 jd_url
        cp_cell,                                                        # 11 career_page_url
        app.get("notes", "") or "",                                     # 12 notes
        app.get("apply_recommendation", "") or "",                      # 13 apply_recommendation
        app.get("visa_sponsorship_status", ""),                         # 14 visa_sponsorship_status
        app.get("actual_hiring_company", "") or "",                     # 15 actual_hiring_company
        app.get("agency_name", "") or "",                               # 16 agency_name
        app.get("company_sponsor_kb", "") or "",                        # 17 company_sponsor_kb
        # ── Secondary columns ─────────────────────────────────────────────────
        app.get("company", ""),                                         # 18 company
        app.get("role", ""),                                            # 19 role
        app.get("source", ""),                                          # 20 source
        app.get("matched_entry_id", "") or "",                          # 21 matched_entry_id
        app.get("adzuna_salary_stated", "") or "",                      # 22 adzuna_salary_stated
        app.get("market", "uk") or "uk",                               # 23 market
    ]


def _archive_row_from_auto_rejected(e: dict) -> list:
    jd = e.get("jd_url") or ""
    jd_cell = f'=HYPERLINK("{jd}","View JD")' if jd.startswith("http") else jd
    return [
        # ── Priority columns ─────────────────────────────────────────────────
        e.get("id", ""),                                                # 0  id
        f"{e.get('company', '')} — {e.get('role', '')}",               # 1  reference (NEW)
        e.get("location", ""),                                          # 2  location
        e.get("posted_date", ""),                                       # 3  posted_date
        e.get("fit_score", ""),                                         # 4  fit_score
        e.get("salary_stated", ""),                                     # 5  salary_stated
        "",                                                             # 6  experience_req (NEW — not tracked)
        "Auto-Rejected",                                                # 7  status
        "auto_rejected",                                                # 8  rejection_stage
        e.get("rejection_reason", ""),                                  # 9  rejection_reason
        jd_cell,                                                        # 10 jd_url
        "",                                                             # 11 career_page_url (never set)
        e.get("visa_hint", ""),                                         # 12 notes (visa_hint as note)
        e.get("apply_recommendation", "") or "Skip",                    # 13 apply_recommendation
        "",                                                             # 14 visa_sponsorship_status (not tracked)
        "",                                                             # 15 actual_hiring_company (not tracked)
        "",                                                             # 16 agency_name (not tracked)
        e.get("company_sponsor_kb", "") or "",                          # 17 company_sponsor_kb
        # ── Secondary columns ─────────────────────────────────────────────────
        e.get("company", ""),                                           # 18 company
        e.get("role", ""),                                              # 19 role
        "",                                                             # 20 source (pass1/pass2 = scoring pass, not scraper)
        "",                                                             # 21 matched_entry_id (not tracked)
        "",                                                             # 22 adzuna_salary_stated (not tracked)
        e.get("market", "uk") or "uk",                                 # 23 market
    ]


# ─────────────────────────────────────────────────────────────────────────────
# PUSH helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_archive_row_count(wb) -> int:
    """Return number of data rows currently in the Archive tab (header excluded).
    Returns 0 if the tab doesn't exist yet (first push is always safe)."""
    try:
        ws = wb.worksheet("Archive")
        # row_count is the grid capacity; count actual non-empty rows instead
        vals = ws.col_values(1)          # column A (id)
        data_rows = sum(1 for v in vals[1:] if v)   # skip header
        return data_rows
    except Exception:
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# PUSH: job_tracker.json → Google Sheets
# ─────────────────────────────────────────────────────────────────────────────
def push():
    print("\n[sheets_sync] PUSH: job_tracker.json → Google Sheets")
    check_setup()

    tracker = json.loads(TRACKER.read_text())
    all_apps = sorted(
        tracker["applications"],
        key=lambda a: a.get("posted_date") or "",
        reverse=True,  # most-recently-posted at the top
    )
    print(f"  {len(all_apps)} applications to sync")

    wb = get_sheet()

    # ── Guard: respect status edits the user made in the Sheet before pulling ──
    # If the Sheet shows an archived status (Withdrawn/Rejected/Stale) for an
    # entry that the JSON still shows as active, treat it as archived so we
    # don't overwrite the user's edit.
    sheet_statuses = _read_sheet_statuses(wb)
    apps = []
    for a in all_apps:
        sheet_st = sheet_statuses.get(a.get("id", ""))
        if sheet_st in ARCHIVED_STATUSES and a.get("status") not in ARCHIVED_STATUSES:
            a = dict(a)  # shallow copy — don't mutate the tracker list
            a["status"] = sheet_st
        apps.append(a)

    # ── Split: active vs archived ─────────────────────────────────────────────
    active_apps   = [a for a in apps if a.get("status") not in ARCHIVED_STATUSES]
    archived_apps = [a for a in apps if a.get("status") in ARCHIVED_STATUSES]

    # ── Applications sheet (active entries only) ──────────────────────────────
    try:
        ws = wb.worksheet("Applications")
    except Exception:
        ws = wb.add_worksheet("Applications", rows=500, cols=len(HEADERS))
        print("  Created 'Applications' worksheet")

    # Clear first so stale rows from a previous (larger) push can't survive.
    rows = [app_to_row(a) for a in active_apps]
    ws.clear()
    ws.update([HEADERS] + rows, "A1", value_input_option="USER_ENTERED")

    print(f"  ✓ Pushed {len(rows)} active rows to 'Applications' sheet "
          f"({len(archived_apps)} archived → Archive tab)")

    # ── Add dropdowns and formatting ──────────────────────────────────────────
    if len(rows) > 0:
        try:
            _apply_dropdowns(wb, ws, len(rows))
            print(f"  ✓ Dropdowns applied to status and career_page_url columns")
        except Exception as e:
            print(f"  ⚠ Dropdown setup failed (non-critical): {e}")

        try:
            _apply_formatting(wb, ws, len(rows))
            print(f"  ✓ Formatting applied (freeze, widths, colours, conditional)")
        except Exception as e:
            print(f"  ⚠ Formatting failed (non-critical): {e}")

    # ── Shrink guard — abort if Archive would lose >20% of its current rows ─────
    auto_rej_entries = []
    if AUTO_REJ_FILE.exists():
        ar_data = json.loads(AUTO_REJ_FILE.read_text())
        auto_rej_entries = ar_data.get("auto_rejected", [])

    new_archive_count = len(archived_apps) + len(auto_rej_entries)
    current_archive_count = _get_archive_row_count(wb)
    force = "--force" in sys.argv

    if not force and current_archive_count > 10 and new_archive_count < current_archive_count * 0.8:
        print(f"\n  ⚠ SHRINK GUARD: Archive would drop {current_archive_count} → {new_archive_count} rows")
        print(f"    Breakdown: {len(archived_apps)} from tracker + {len(auto_rej_entries)} from auto_rejected.json")
        print(f"    Re-run with --force to override, or investigate the discrepancy first.")
        sys.exit(1)

    # ── Archive sheet (rejected + withdrawn + stale) ──────────────────────────
    try:
        _push_archive(wb, archived_apps)
    except Exception as e:
        print(f"  ⚠ Archive sheet failed (non-critical): {e}")

    # ── Record push snapshot in _meta so `status` can detect unpushed changes ──
    tracker["_meta"]["last_push_snapshot"] = {
        "timestamp":          datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "applications_count": len(active_apps),
        "archive_count":      new_archive_count,
    }
    TRACKER.write_text(json.dumps(tracker, indent=2, ensure_ascii=False))

    # Reorganise output folders to match current statuses (ready/ vs done/).
    try:
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location("organize_outputs", Path(__file__).parent / "organize_outputs.py")
        _mod  = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _mod.organize_outputs()
    except Exception as e:
        print(f"  ⚠ organize_outputs skipped: {e}")

    print(f"  Sheet URL: https://docs.google.com/spreadsheets/d/{SHEET_ID}")


def _apply_dropdowns(wb, ws, num_rows: int):
    """
    Apply data validation dropdowns to user-editable columns.
    Uses raw Sheets API batchUpdate — no extra dependencies needed.

    Dropdowns:
      Col L (status, index 11)      — valid pipeline statuses
      Col M (career_page_url, 13)   — hint values: EASY_APPLY or paste URL
        (can't enforce URL format via dropdown, so we add a note dropdown
         that reminds you of the two valid entry types)
    """
    STATUS_COL    = HEADERS.index("status")           # 0-indexed = 11
    CAREER_COL    = HEADERS.index("career_page_url")  # 0-indexed = 13

    STATUS_VALUES = [
        "Shortlisted", "Review Needed", "Approved", "Prep Complete",
        "Applied", "Referral", "Under Review", "Interview Scheduled", "Assessment",
        "Offer Received", "Rejected", "Withdrawn", "Duplicate",
    ]

    CAREER_HINTS = [
        "EASY_APPLY",
        "Paste ATS URL here",
    ]

    spreadsheet_id = wb.id
    creds          = wb.client.auth

    # Build Sheets API request using gspread's underlying service
    # gspread exposes the raw service via client.auth._default_http
    # We use requests directly with the service account token
    import json as _json
    from google.auth.transport.requests import Request as GARequest

    # Refresh credentials if needed
    if not creds.valid:
        creds.refresh(GARequest())

    token = creds.token

    def col_range(col_0idx):
        """Build GridRange dict for a full column (data rows only)."""
        return {
            "sheetId":          ws.id,
            "startRowIndex":    1,               # row 2 (0-indexed)
            "endRowIndex":      num_rows + 1,
            "startColumnIndex": col_0idx,
            "endColumnIndex":   col_0idx + 1,
        }

    def dropdown_rule(values, col_0idx, strict=True):
        return {
            "setDataValidation": {
                "range": col_range(col_0idx),
                "rule": {
                    "condition": {
                        "type": "ONE_OF_LIST",
                        "values": [{"userEnteredValue": v} for v in values],
                    },
                    "showCustomUi":  True,
                    "strict":        strict,
                }
            }
        }

    requests = [
        dropdown_rule(STATUS_VALUES, STATUS_COL, strict=True),
        # career_page_url: show hint dropdown but not strict
        # (user needs to paste a real URL too, so we can't enforce a fixed list)
        dropdown_rule(CAREER_HINTS, CAREER_COL, strict=False),
    ]

    import urllib.request as _ureq
    url     = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}:batchUpdate"
    payload = _json.dumps({"requests": requests}).encode()
    req     = _ureq.Request(url, data=payload, method="POST")
    req.add_header("Authorization",  f"Bearer {token}")
    req.add_header("Content-Type",   "application/json")

    with _ureq.urlopen(req, timeout=20) as resp:
        resp.read()   # consume response


def _apply_formatting(wb, ws, num_rows: int):
    """
    Apply visual formatting to the Applications sheet:
      - Freeze header row
      - Column widths
      - Bold dark header
      - Amber highlight on user-editable columns (status, career_page_url, notes)
      - Conditional formatting on status column (one colour per status value)
    Safe to call on every push — conditional rules are cleared and re-added.
    """
    import urllib.request as _ureq, json as _json
    from google.auth.transport.requests import Request as GARequest

    creds = wb.client.auth
    if not creds.valid:
        creds.refresh(GARequest())
    token          = creds.token
    spreadsheet_id = wb.id
    sheet_id       = ws.id

    STATUS_COL  = HEADERS.index("status")           # 0-indexed = 7
    CAREER_COL  = HEADERS.index("career_page_url")  # 0-indexed = 9
    NOTES_COL   = HEADERS.index("notes")            # 0-indexed = 10

    # Colour map: status value → (bg_r, bg_g, bg_b, fg_r, fg_g, fg_b)
    STATUS_COLORS = {
        "Shortlisted":         (1.000, 0.949, 0.800, 0, 0, 0),
        "Review Needed":       (0.988, 0.898, 0.804, 0, 0, 0),
        "Stale":               (0.878, 0.878, 0.878, 0.4, 0.4, 0.4),
        "Approved":            (0.851, 0.918, 0.827, 0, 0, 0),
        "Prep Complete":       (0.714, 0.843, 0.659, 0, 0, 0),
        "Applied":             (0.812, 0.886, 0.953, 0, 0, 0),
        "Referral":            (0.984, 0.871, 0.678, 0, 0, 0),
        "Under Review":        (0.788, 0.855, 0.973, 0, 0, 0),
        "Interview Scheduled": (0.851, 0.824, 0.914, 0, 0, 0),
        "Assessment":          (0.918, 0.820, 0.863, 0, 0, 0),
        "Offer Received":      (0.416, 0.659, 0.310, 1, 1, 1),
        "Rejected":            (0.957, 0.800, 0.800, 0, 0, 0),
        "Auto-Rejected":       (0.918, 0.600, 0.600, 0, 0, 0),
        "Withdrawn":           (0.937, 0.937, 0.937, 0.4, 0.4, 0.4),
        "Duplicate":           (0.906, 0.835, 0.953, 0.3, 0.3, 0.3),
    }

    def col_range(col_0idx, start_row=1, end_row=None):
        return {
            "sheetId":          sheet_id,
            "startRowIndex":    start_row,
            "endRowIndex":      end_row if end_row else num_rows + 1,
            "startColumnIndex": col_0idx,
            "endColumnIndex":   col_0idx + 1,
        }

    requests = []

    # 1. Freeze header row
    requests.append({
        "updateSheetProperties": {
            "properties": {
                "sheetId": sheet_id,
                "gridProperties": {"frozenRowCount": 1},
            },
            "fields": "gridProperties.frozenRowCount",
        }
    })

    # 2. Column widths (pixels) — keyed by field name, resolved to index at runtime
    FIELD_WIDTHS = {
        "id": 90, "reference": 260, "location": 110, "posted_date": 100,
        "fit_score": 75, "salary_stated": 200, "experience_req": 120,
        "status": 140, "jd_url": 55, "career_page_url": 55, "notes": 200,
        "apply_recommendation": 90, "visa_sponsorship_status": 160,
        "actual_hiring_company": 180, "agency_name": 180, "company_sponsor_kb": 150,
        "job_id": 110, "company": 200, "role": 260, "work_mode": 100,
        "applied_date": 100, "ats_type": 120, "is_contract": 80,
        "tracking_url": 55, "source": 90, "match_exists": 85,
        "matched_entry_id": 110, "score_exists": 80, "latest_scoring_date": 130,
        "adzuna_salary_stated": 180,
    }
    col_widths = {HEADERS.index(f): w for f, w in FIELD_WIDTHS.items() if f in HEADERS}
    for col_idx, px in col_widths.items():
        requests.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId":    sheet_id,
                    "dimension":  "COLUMNS",
                    "startIndex": col_idx,
                    "endIndex":   col_idx + 1,
                },
                "properties": {"pixelSize": px},
                "fields": "pixelSize",
            }
        })

    # 3. Header row: bold, dark background, white text, centred
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId":       sheet_id,
                "startRowIndex": 0,
                "endRowIndex":   1,
            },
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": {"red": 0.216, "green": 0.278, "blue": 0.310},
                    "textFormat": {
                        "bold": True,
                        "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                    },
                    "horizontalAlignment": "CENTER",
                }
            },
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
        }
    })

    # 4. Editable column highlights (light amber background for data rows)
    AMBER = {"red": 1.0, "green": 0.988, "blue": 0.878}
    for col_idx in (STATUS_COL, CAREER_COL, NOTES_COL):
        requests.append({
            "repeatCell": {
                "range": col_range(col_idx, start_row=1),
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": AMBER,
                    }
                },
                "fields": "userEnteredFormat.backgroundColor",
            }
        })

    # 5. Delete existing conditional format rules for this sheet, then re-add
    #    (avoids rule accumulation on repeated pushes)
    requests.append({
        "deleteConditionalFormatRule": {
            "sheetId": sheet_id,
            "index":   0,
        }
    })

    # 6. Add conditional formatting for each status value
    status_range = {
        "sheetId":          sheet_id,
        "startRowIndex":    1,
        "endRowIndex":      1000,
        "startColumnIndex": STATUS_COL,
        "endColumnIndex":   STATUS_COL + 1,
    }
    for i, (status_val, (br, bg, bb, fr, fg, fb)) in enumerate(STATUS_COLORS.items()):
        requests.append({
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [status_range],
                    "booleanRule": {
                        "condition": {
                            "type":   "TEXT_EQ",
                            "values": [{"userEnteredValue": status_val}],
                        },
                        "format": {
                            "backgroundColor": {"red": br, "green": bg, "blue": bb},
                            "textFormat": {
                                "foregroundColor": {"red": fr, "green": fg, "blue": fb}
                            },
                        },
                    },
                },
                "index": i,
            }
        })

    # Send batchUpdate — split into two calls:
    #   First: all formatting except the deleteConditionalFormatRule
    #          (which fails if no rules exist yet on first push)
    #   Then:  conditional rules

    def _batch(reqs):
        url     = (f"https://sheets.googleapis.com/v4/spreadsheets/"
                   f"{spreadsheet_id}:batchUpdate")
        payload = _json.dumps({"requests": reqs}).encode()
        req     = _ureq.Request(url, data=payload, method="POST")
        req.add_header("Authorization",  f"Bearer {token}")
        req.add_header("Content-Type",   "application/json")
        with _ureq.urlopen(req, timeout=20) as resp:
            resp.read()

    # Non-conditional requests (freeze, widths, header, editable cols)
    non_cond = [r for r in requests if "deleteConditionalFormatRule" not in r
                                    and "addConditionalFormatRule" not in r]
    _batch(non_cond)

    # Try to delete existing rules (silently ignore if none exist)
    try:
        _batch([{"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": 0}}])
    except Exception:
        pass

    # Add conditional rules
    cond_rules = [r for r in requests if "addConditionalFormatRule" in r]
    if cond_rules:
        _batch(cond_rules)


def _push_archive(wb, archived_from_tracker: list):
    """Push all archived entries (Rejected/Auto-Rejected/Withdrawn/Stale from
    job_tracker.json plus auto_rejected.json) into a single 'Archive' sheet tab.
    Also deletes the legacy 'Auto-Rejected' tab if it exists.
    """
    import urllib.request as _ureq, json as _json
    from google.auth.transport.requests import Request as GARequest

    # ── Combine sources ───────────────────────────────────────────────────────
    tracker_rows = [_archive_row_from_tracker(a) for a in archived_from_tracker]

    auto_rej_rows = []
    if AUTO_REJ_FILE.exists():
        ar_data = json.loads(AUTO_REJ_FILE.read_text())
        auto_rej_rows = [
            _archive_row_from_auto_rejected(e)
            for e in ar_data.get("auto_rejected", [])
        ]

    all_rows = tracker_rows + auto_rej_rows
    if not all_rows:
        print("  [archive] No archived entries to push")
        return

    # ── Get or create Archive worksheet ──────────────────────────────────────
    try:
        ws = wb.worksheet("Archive")
    except Exception:
        ws = wb.add_worksheet("Archive", rows=2000, cols=len(ARCHIVE_HEADERS))
        print("  Created 'Archive' worksheet")

    ws.clear()
    ws.update([ARCHIVE_HEADERS] + all_rows, "A1", value_input_option="USER_ENTERED")

    # ── Delete legacy 'Auto-Rejected' tab if it exists ────────────────────────
    try:
        old_ws = wb.worksheet("Auto-Rejected")
        wb.del_worksheet(old_ws)
        print("  Deleted legacy 'Auto-Rejected' worksheet")
    except Exception:
        pass

    # ── Formatting: freeze header + dark header + status colour coding ────────
    creds = wb.client.auth
    if not creds.valid:
        creds.refresh(GARequest())
    token          = creds.token
    spreadsheet_id = wb.id
    sheet_id       = ws.id

    STATUS_COL_IDX    = ARCHIVE_HEADERS.index("status")          # 5
    REJ_STAGE_COL_IDX = ARCHIVE_HEADERS.index("rejection_stage") # 6

    ARCHIVE_STATUS_COLORS = {
        "Rejected":      (0.957, 0.800, 0.800, 0, 0, 0),
        "Auto-Rejected": (0.918, 0.600, 0.600, 0, 0, 0),
        "Withdrawn":     (0.937, 0.937, 0.937, 0.4, 0.4, 0.4),
        "Stale":         (0.878, 0.878, 0.878, 0.4, 0.4, 0.4),
        "Duplicate":     (0.906, 0.835, 0.953, 0.3, 0.3, 0.3),
    }
    STAGE_COLORS = {
        "post_application": (0.988, 0.878, 0.878, 0, 0, 0),
        "auto_rejected":    (0.976, 0.796, 0.796, 0, 0, 0),
        "withdrawn":        (0.937, 0.937, 0.937, 0.4, 0.4, 0.4),
        "stale":            (0.878, 0.878, 0.878, 0.4, 0.4, 0.4),
        "duplicate":        (0.906, 0.835, 0.953, 0.3, 0.3, 0.3),
    }

    def _batch(reqs):
        url     = (f"https://sheets.googleapis.com/v4/spreadsheets/"
                   f"{spreadsheet_id}:batchUpdate")
        payload = _json.dumps({"requests": reqs}).encode()
        req     = _ureq.Request(url, data=payload, method="POST")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type",  "application/json")
        with _ureq.urlopen(req, timeout=20) as resp:
            resp.read()

    base_requests = [
        # Freeze header
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": 1},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        },
        # Dark header
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 0.216, "green": 0.278, "blue": 0.310},
                        "textFormat": {
                            "bold": True,
                            "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                        },
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat)",
            }
        },
    ]
    _batch(base_requests)

    # Conditional formatting — clear existing first, then add
    try:
        _batch([{"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": 0}}])
    except Exception:
        pass

    cond_rules = []
    for col_idx, color_map in (
        (STATUS_COL_IDX, ARCHIVE_STATUS_COLORS),
        (REJ_STAGE_COL_IDX, STAGE_COLORS),
    ):
        col_range = {
            "sheetId": sheet_id,
            "startRowIndex": 1, "endRowIndex": 2000,
            "startColumnIndex": col_idx, "endColumnIndex": col_idx + 1,
        }
        for i, (val, (br, bg, bb, fr, fg, fb)) in enumerate(color_map.items()):
            cond_rules.append({
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [col_range],
                        "booleanRule": {
                            "condition": {
                                "type":   "TEXT_EQ",
                                "values": [{"userEnteredValue": val}],
                            },
                            "format": {
                                "backgroundColor": {"red": br, "green": bg, "blue": bb},
                                "textFormat": {
                                    "foregroundColor": {"red": fr, "green": fg, "blue": fb}
                                },
                            },
                        },
                    },
                    "index": i,
                }
            })
    if cond_rules:
        _batch(cond_rules)

    print(f"  ✓ Pushed {len(all_rows)} rows to 'Archive' sheet "
          f"({len(tracker_rows)} from tracker, {len(auto_rej_rows)} from auto_rejected.json)")


# ─────────────────────────────────────────────────────────────────────────────
# PULL: Google Sheets → job_tracker.json (user-editable columns only)
# ─────────────────────────────────────────────────────────────────────────────
def pull():
    print("\n[sheets_sync] PULL: Google Sheets → job_tracker.json")
    print("  Reading user-editable columns: status, career_page_url, notes")
    check_setup()

    tracker = json.loads(TRACKER.read_text())
    apps    = {a["id"]: a for a in tracker["applications"]}

    wb = get_sheet()
    try:
        ws = wb.worksheet("Applications")
    except Exception:
        print("  ✗ 'Applications' worksheet not found — run push first")
        sys.exit(1)

    rows = ws.get_all_records(value_render_option='FORMULA')   # FORMULA mode so =HYPERLINK(...) is returned as text, not display value
    updated = 0

    for row in rows:
        app_id = str(row.get("id", "")).strip()
        if not app_id or app_id not in apps:
            continue

        app     = apps[app_id]
        changed = []

        for col in USER_EDITABLE:
            sheet_val = str(row.get(col, "") or "").strip()
            local_val = str(app.get(col, "") or "").strip()

            # Skip HYPERLINK formula values — extract raw URL
            if sheet_val.startswith("=HYPERLINK"):
                import re
                m = re.search(r'HYPERLINK\("([^"]+)"', sheet_val)
                sheet_val = m.group(1) if m else ""

            if sheet_val and sheet_val != local_val:
                # Status change — record in history
                if col == "status" and sheet_val != local_val:
                    if "status_history" not in app:
                        app["status_history"] = []
                    app["status_history"].append({
                        "status": sheet_val,
                        "date":   datetime.now().strftime("%Y-%m-%d"),
                        "source": "sheets_sync_pull",
                    })
                app[col] = sheet_val
                changed.append(f"{col}: '{local_val}' → '{sheet_val}'")

        if changed:
            updated += 1
            print(f"  ✓ {app.get('company')} / {app.get('role')}")
            for c in changed: print(f"      {c}")

    # ── Archive tab: detect re-activations (user changed Stale → Shortlisted etc.) ──
    try:
        ws_archive = wb.worksheet("Archive")
        archive_rows = ws_archive.get_all_records(value_render_option="FORMULA")
        for row in archive_rows:
            app_id = str(row.get("id", "")).strip()
            if not app_id or app_id not in apps:
                continue  # auto_rejected.json entries or unknown IDs — skip
            sheet_status = str(row.get("status", "") or "").strip()
            if not sheet_status or sheet_status in ARCHIVED_STATUSES:
                continue  # still archived — no action
            # Status was manually changed to an active value — re-activate
            app = apps[app_id]
            old_status = app.get("status", "")
            app["status"] = sheet_status
            app.setdefault("status_history", []).append({
                "status": sheet_status,
                "date":   datetime.now().strftime("%Y-%m-%d"),
                "source": "sheets_sync_pull_archive_reactivation",
                "reason": f"Manually re-activated from Archive (was {old_status})",
            })
            # Also restore career_page_url if the user filled it in Archive
            sheet_url = str(row.get("career_page_url", "") or "").strip()
            if sheet_url.startswith("=HYPERLINK"):
                m = re.search(r'HYPERLINK\("([^"]+)"', sheet_url)
                sheet_url = m.group(1) if m else ""
            if sheet_url:
                app["career_page_url"] = sheet_url
            updated += 1
            print(f"  ✓ RE-ACTIVATED {app.get('company')} / {app.get('role')}")
            print(f"      status: '{old_status}' → '{sheet_status}' (from Archive tab)")
    except Exception as e:
        print(f"  [archive pull] Skipped Archive tab — {e}")

    # Write back
    tracker["applications"] = list(apps.values())
    TRACKER.write_text(json.dumps(tracker, indent=2, ensure_ascii=False))

    # Reorganise output folders to match updated statuses (ready/ vs done/).
    try:
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location("organize_outputs", Path(__file__).parent / "organize_outputs.py")
        _mod  = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _mod.organize_outputs()
    except Exception as e:
        print(f"  ⚠ organize_outputs skipped: {e}")

    print(f"\n  {updated} applications updated in job_tracker.json")
    if updated == 0:
        print("  (No changes detected in user-editable columns)")


# ─────────────────────────────────────────────────────────────────────────────
# SETUP INSTRUCTIONS (printed when run without args)
# ─────────────────────────────────────────────────────────────────────────────
SETUP_GUIDE = """
sheets_sync.py — One-time Setup
═══════════════════════════════

Step 1 — Install dependencies (in your venv):
  pip install gspread google-auth

Step 2 — Create Google Cloud credentials:
  a. Go to https://console.cloud.google.com
  b. Select your existing 'job-automation' project
     (or create a new one)
  c. APIs & Services → Enable APIs:
       - Google Sheets API
       - Google Drive API
  d. APIs & Services → Credentials →
     + Create Credentials → Service Account
       Name: job-automation-sheets
       Click Create, skip optional steps
  e. Click the service account email → Keys →
     Add Key → Create new key → JSON → Download
  f. Rename the downloaded file to:
       google_service_account.json
     Move it to your project's data/ folder

Step 3 — Create your Google Sheet:
  a. Go to https://sheets.google.com → New spreadsheet
  b. Name it: "Job Application Tracker"
  c. Share it with the service account email
     (found in google_service_account.json as "client_email")
     → Give it Editor access
  d. Copy the Sheet ID from the URL:
     https://docs.google.com/spreadsheets/d/SHEET_ID_HERE/edit
  e. Add to .env:
     GOOGLE_SHEET_ID=SHEET_ID_HERE

Step 4 — Test:
  python3 scripts/sheets_sync.py push
  (Then open your Sheet — you should see all applications)

Step 5 — Workflow:
  After every scrape+enrich:   python3 scripts/sheets_sync.py push
  Before approving jobs:       python3 scripts/sheets_sync.py pull
  After editing sheet:         python3 scripts/sheets_sync.py pull
"""

# ─────────────────────────────────────────────────────────────────────────────
# STATUS: compare local files vs live Sheet — read-only health check
# ─────────────────────────────────────────────────────────────────────────────
def status():
    """Read-only sync health check.
    Compares what the Sheet *should* contain (from local files) vs what it
    actually contains (live Sheet read). Shows a clear ✓ / ⚠ verdict.
    """
    print("\n[sheets_sync] STATUS")
    check_setup()

    # ── Local file counts ─────────────────────────────────────────────────────
    tracker = json.loads(TRACKER.read_text())
    apps    = tracker["applications"]
    meta    = tracker.get("_meta", {})

    active_count   = sum(1 for a in apps if a.get("status") not in ARCHIVED_STATUSES)
    archived_count = sum(1 for a in apps if a.get("status") in ARCHIVED_STATUSES)

    auto_rej_count = 0
    if AUTO_REJ_FILE.exists():
        ar_data = json.loads(AUTO_REJ_FILE.read_text())
        auto_rej_count = len(ar_data.get("auto_rejected", []))

    expected_apps    = active_count
    expected_archive = archived_count + auto_rej_count

    print(f"\n  Local files:")
    print(f"    job_tracker.json   — {active_count} active, {archived_count} archived")
    print(f"    auto_rejected.json — {auto_rej_count} entries")
    print(f"    {'─'*46}")
    print(f"    Expected Sheet:  Applications = {expected_apps}")
    print(f"                     Archive      = {expected_archive}  ({archived_count} tracker + {auto_rej_count} auto_rejected)")

    # ── Last push snapshot ────────────────────────────────────────────────────
    snap = meta.get("last_push_snapshot")
    if snap:
        print(f"\n  Last push snapshot: {snap['timestamp']}")
        print(f"    pushed Applications={snap['applications_count']}, Archive={snap['archive_count']}")
    else:
        print(f"\n  Last push snapshot: (none — push has never been recorded)")

    # ── Live Sheet counts ─────────────────────────────────────────────────────
    problems = []
    try:
        wb = get_sheet()

        # Applications tab
        try:
            ws_apps = wb.worksheet("Applications")
            app_rows = sum(1 for v in ws_apps.col_values(1)[1:] if v)
        except Exception:
            app_rows = None

        # Archive tab
        try:
            ws_arch = wb.worksheet("Archive")
            arch_rows = sum(1 for v in ws_arch.col_values(1)[1:] if v)
        except Exception:
            arch_rows = None

        print(f"\n  Google Sheet (live):")
        if app_rows is not None:
            match = "✓" if app_rows == expected_apps else "⚠"
            print(f"    Applications tab: {app_rows} rows {match}")
            if app_rows != expected_apps:
                problems.append(f"Applications tab has {app_rows} rows, expected {expected_apps}")
        else:
            print(f"    Applications tab: (not found)")
            problems.append("Applications tab missing — run push first")

        if arch_rows is not None:
            match = "✓" if arch_rows == expected_archive else "⚠"
            print(f"    Archive tab:      {arch_rows} rows {match}")
            if arch_rows != expected_archive:
                problems.append(f"Archive tab has {arch_rows} rows, expected {expected_archive}")
        else:
            print(f"    Archive tab:      (not found)")
            problems.append("Archive tab missing — run push first")

    except Exception as e:
        print(f"\n  Google Sheet (live): unreachable — {e}")
        print(f"\n  Sync verdict: ? UNKNOWN (Sheet unreachable)")
        return

    # ── Unpushed-changes check ────────────────────────────────────────────────
    if snap:
        if (snap["applications_count"] != active_count or
                snap["archive_count"] != expected_archive):
            problems.append(
                f"JSON changed since last push "
                f"(snapshot had apps={snap['applications_count']}, archive={snap['archive_count']}; "
                f"now apps={active_count}, archive={expected_archive}) — run push"
            )

    # ── Pull-needed check (Sheet edits not yet in JSON) ───────────────────────
    # Detect if Sheet Applications tab has status/career_page_url values that
    # differ from JSON — a lightweight signal that pull is needed.
    if app_rows and app_rows > 0:
        try:
            sheet_rows = wb.worksheet("Applications").get_all_records(value_render_option="FORMULA")
            apps_by_id = {a["id"]: a for a in apps}
            pull_needed = False
            for row in sheet_rows:
                app_id  = str(row.get("id", "")).strip()
                app     = apps_by_id.get(app_id)
                if not app:
                    continue
                for col in USER_EDITABLE:
                    sv = str(row.get(col, "") or "").strip()
                    if sv.startswith("=HYPERLINK"):
                        m = re.search(r'HYPERLINK\("([^"]+)"', sv)
                        sv = m.group(1) if m else ""
                    lv = str(app.get(col, "") or "").strip()
                    if sv and sv != lv:
                        pull_needed = True
                        break
                if pull_needed:
                    break
            if pull_needed:
                problems.append("Sheet has edits not yet in JSON (status/career_page_url/notes) — run pull")
        except Exception:
            pass

    # ── Verdict ───────────────────────────────────────────────────────────────
    print()
    if not problems:
        print("  Sync verdict: ✓ IN SYNC")
    else:
        for p in problems:
            print(f"  ⚠ {p}")
        print(f"\n  Sync verdict: ⚠ ACTION NEEDED  (see warnings above)")


# ─────────────────────────────────────────────────────────────────────────────
# RECOVER_ARCHIVE: import Archive tab entries missing from job_tracker.json
# ─────────────────────────────────────────────────────────────────────────────
def recover_archive():
    """Read the Archive tab and import any entries whose ID is not already in
    job_tracker.json. Useful after a JSON rebuild that lost historical entries.
    Recovered entries are minimal (only the fields the Archive tab stores).
    """
    print("\n[sheets_sync] RECOVER_ARCHIVE: Sheet Archive tab → job_tracker.json")
    check_setup()

    tracker = json.loads(TRACKER.read_text())
    existing_ids = {a["id"] for a in tracker["applications"]}

    # Also exclude IDs already in auto_rejected.json — those appear in Archive
    # but are NOT part of job_tracker.json by design.
    if AUTO_REJ_FILE.exists():
        ar_data = json.loads(AUTO_REJ_FILE.read_text())
        for e in ar_data.get("auto_rejected", []):
            if e.get("id"):
                existing_ids.add(e["id"])

    wb = get_sheet()
    try:
        ws = wb.worksheet("Archive")
    except Exception:
        print("  ✗ 'Archive' worksheet not found — nothing to recover")
        return

    rows = ws.get_all_records(value_render_option="FORMULA")
    today = datetime.now().strftime("%Y-%m-%d")

    recovered, skipped = 0, 0
    for row in rows:
        app_id = str(row.get("id", "")).strip()
        if not app_id:
            continue
        if app_id in existing_ids:
            skipped += 1
            continue

        # Extract raw URL from =HYPERLINK("url","...") formula if present
        raw_jd = str(row.get("jd_url", "") or "")
        if raw_jd.startswith("=HYPERLINK"):
            m = re.search(r'HYPERLINK\("([^"]+)"', raw_jd)
            raw_jd = m.group(1) if m else ""

        status = str(row.get("status", "") or "").strip() or "Rejected"
        entry = {
            "id":                    app_id,
            "company":               str(row.get("company", "") or "").strip(),
            "role":                  str(row.get("role", "") or "").strip(),
            "location":              str(row.get("location", "") or "").strip(),
            "fit_score":             row.get("fit_score", ""),
            "status":                status,
            "salary_stated":         str(row.get("salary_stated", "") or "").strip(),
            "jd_url":                raw_jd,
            "posted_date":           str(row.get("posted_date", "") or "").strip(),
            "visa_sponsorship_status": str(row.get("visa_sponsorship_status", "") or "").strip(),
            "notes":                 str(row.get("notes", "") or "").strip(),
            "source":                "sheet_recovery",
            "status_history": [{"status": status, "date": today, "source": "sheet_recovery"}],
        }
        tracker["applications"].append(entry)
        existing_ids.add(app_id)
        recovered += 1
        print(f"  + {entry['company']} / {entry['role']} ({status})")

    if recovered:
        TRACKER.write_text(json.dumps(tracker, indent=2, ensure_ascii=False))
        print(f"\n  ✓ Recovered {recovered} entries into job_tracker.json ({skipped} already existed)")
    else:
        print(f"  No new entries to recover ({skipped} already in JSON)")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "push":
        push()
    elif mode == "pull":
        pull()
    elif mode == "status":
        status()
    elif mode == "recover_archive":
        recover_archive()
    else:
        print(SETUP_GUIDE)
