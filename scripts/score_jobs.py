#!/usr/bin/env python3
"""
score_jobs.py — Semantic job scorer using Claude API (Haiku)
============================================================
Two-pass architecture:
  Pass 1 (free, ~0ms): Python pre-filter using ONLY native LinkedIn/Apify
    metadata fields (poster-declared, reliable, no JD text interpretation).
    Rejection hierarchy (most objective first):
    1. posted_date      → reject if >30d old; track as Stale if >3d old
    2. work_type        → reject if LinkedIn marks as Remote-only
    3. job_type         → reject if LinkedIn marks as Contract/Part-time
    4. salary (native)  → reject only if LinkedIn-provided AND clearly below £85k
    5. location         → reject if outside Tier 1/2/3
    6. job_title        → hard-reject title list (last: lets other gates fire first)
    - Duplicate check   → jd_url exact match OR fuzzy company+role match
    Note: Visa sponsorship requires JD text reading → Pass 2 only.

  Pass 2 (~$0.027/run): Claude Haiku semantic scoring for jobs that survive
    Pass 1. Receives raw LinkedIn native fields + full JD description.
    Claude is authoritative for: salary extraction, visa sponsorship,
    work mode, contract detection, remote detection, agency post detection,
    ATS type identification. No heuristic pre-processed values are passed.

Usage:
  python3 scripts/score_jobs.py                   # default
  python3 scripts/score_jobs.py --model sonnet    # higher quality (more expensive)
  python3 scripts/score_jobs.py --no-prefilter    # skip Pass 1 (debug)
  python3 scripts/score_jobs.py --max-age 14      # reject postings older than 14 days
  python3 scripts/score_jobs.py --dry-run         # score but don't write to disk

Output:
  data/scored_jobs.json     — shortlisted + review jobs (input to tracker writer)
  data/auto_rejected.json   — updated with rejected jobs (persistent history)
"""

import json, re, sys, time
from pathlib import Path
from datetime import datetime, date
from typing import Optional
from urllib import request as _ureq, error as _uerr

ROOT           = Path(__file__).parent.parent
ENRICHED_PATH  = ROOT / "data" / "pipeline" / "enriched_scrape_output.json"
TRACKER_PATH   = ROOT / "data" / "job_tracker.json"
AUTO_REJ_PATH  = ROOT / "data" / "auto_rejected.json"
SCORED_PATH    = ROOT / "data" / "pipeline" / "scored_jobs.json"
ENV_FILE       = ROOT / ".env"

TODAY = date.today().isoformat()

# ── Env / API key ─────────────────────────────────────────────────────────────
def load_env() -> dict:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env

# ── CLI args ──────────────────────────────────────────────────────────────────
_args = sys.argv[1:]
MODEL        = "claude-haiku-4-5-20251001"
NO_PREFILTER    = "--no-prefilter" in _args
DRY_RUN         = "--dry-run" in _args
MAX_AGE         = 30   # hard-reject threshold (days) — older than this, don't track
STALE_AGE_DAYS  = 3    # stale threshold (days) — last 4 days scored fresh (today + 3)

# NL/SE use 7-day Apify windows on first run — their stale threshold matches
_STALE_DAYS_BY_MARKET = {"uk": 3, "nl": 7, "se": 7}

def _get_stale_threshold(market: str) -> int:
    return _STALE_DAYS_BY_MARKET.get((market or "uk").lower(), STALE_AGE_DAYS)

if "--model" in _args:
    idx = _args.index("--model")
    MODEL = _args[idx+1] if idx+1 < len(_args) else MODEL
if "--max-age" in _args:
    idx = _args.index("--max-age")
    try: MAX_AGE = int(_args[idx+1])
    except: pass

# ── Standalone stale-sweep mode (no enriched data needed) ────────────────────
if "--recategorize-stale" in _args:
    _raw  = json.loads(TRACKER_PATH.read_text()) if TRACKER_PATH.exists() else {"applications": []}
    _apps = _raw.get("applications", [])
    _today = date.today()
    _today_str = _today.isoformat()
    _count = 0
    for _app in _apps:
        if _app.get("status") not in {"Shortlisted", "Review Needed"}:
            continue
        _pd = _app.get("posted_date") or ""
        _m  = re.match(r"(\d{4}-\d{2}-\d{2})", str(_pd))
        if not _m:
            continue
        try:
            _age = (_today - date.fromisoformat(_m.group(1))).days
        except ValueError:
            continue
        _thr2 = _get_stale_threshold(_app.get("market", "uk"))
        if _age <= _thr2:
            continue
        _old = _app["status"]
        _app["status"] = "Stale"
        _app.setdefault("status_history", []).append({
            "status": "Stale", "date": _today_str,
            "source": "score_jobs_stale_sweep",
            "reason": f"Retroactive: posting now {_age}d old (>{_thr2}d threshold)",
        })
        print(f"  [stale-sweep] {_app.get('company')} / {_app.get('role')} "
              f"({_old} → Stale, age={_age}d)")
        _count += 1
    if _count and not DRY_RUN:
        _raw["applications"] = _apps
        TRACKER_PATH.write_text(json.dumps(_raw, indent=2, ensure_ascii=False))
    print(f"\n[score_jobs] Stale sweep complete: {_count} entries updated.")
    sys.exit(0)

# ── Load data ─────────────────────────────────────────────────────────────────
if not ENRICHED_PATH.exists():
    print(f"ERROR: {ENRICHED_PATH} not found. Run python3 scripts/run_scout.py first.")
    sys.exit(1)

enriched    = json.loads(ENRICHED_PATH.read_text())
tracker_raw = json.loads(TRACKER_PATH.read_text()) if TRACKER_PATH.exists() else {"applications": []}
tracker     = tracker_raw.get("applications", [])

auto_rej_raw = {"auto_rejected": []}
if AUTO_REJ_PATH.exists():
    try: auto_rej_raw = json.loads(AUTO_REJ_PATH.read_text())
    except: pass
auto_rejected = auto_rej_raw.get("auto_rejected", [])

print(f"\n[score_jobs] ─────────────────────────────────────────")
print(f"[score_jobs] Input:        {len(enriched)} enriched jobs")
print(f"[score_jobs] Tracker:      {len(tracker)} existing entries (duplicate check)")
print(f"[score_jobs] Auto-rejected:{len(auto_rejected)} historical rejects (duplicate check)")
print(f"[score_jobs] Model:        {MODEL}")
print(f"[score_jobs] Max age:      {MAX_AGE} days")
print(f"[score_jobs] Pre-filter:   {'disabled' if NO_PREFILTER else 'enabled (native fields only)'}")
print(f"[score_jobs] ─────────────────────────────────────────\n")

# ─────────────────────────────────────────────────────────────────────────────
# PASS 1 — Native LinkedIn metadata gates (no API cost, no JD text parsing)
# ─────────────────────────────────────────────────────────────────────────────

TIER1 = {"london"}
TIER2 = {
    "manchester", "birmingham", "leeds", "reading", "milton keynes",
    "cambridge", "oxford", "leicester", "coventry", "nottingham", "northampton"
}
TIER3 = {"bristol", "brighton", "luton", "watford", "slough", "guildford"}
ALL_TIERS = TIER1 | TIER2 | TIER3

# ── NL / SE tier cities ───────────────────────────────────────────────────────
NL_TIER1 = {
    "amsterdam", "rotterdam", "den haag", "the hague", "utrecht",
    "randstad",    # LinkedIn region string covering the full Randstad cluster
    "amstelveen",  # Amsterdam suburb (<20 min)
    "hoofddorp",   # Amsterdam metro / Schiphol area (<25 min)
    "schiphol",    # Schiphol area (Amsterdam metro)
}
SE_TIER1 = {
    "stockholm", "gothenburg", "göteborg", "malmo", "malmö",
    "solna",     # Stockholm County, immediately north of Stockholm
    "mölndal",   # Adjacent to Gothenburg, same labour market
}

# Minimum analytics relevance required before a stale posting is tracked.
# Stale jobs that don't contain any of these substrings are rejected outright —
# there's no value logging "Project Controls Consultant" as Stale.
ANALYTICS_TITLE_KW = {
    "analytic", "analyst", "data", "insight",
    "bi manager", "bi lead", "bi director", "decision scientist",
}

TITLE_REJECT_CONTAINS = [
    # ── Broad keyword blocks (each subsumes several specific patterns) ────────
    "engineer",     # data/software/ML/cloud/platform/BI/AI-enablement engineers — not analytics leadership
                    # tradeoff: "Senior Analytics Engineer" (Tier 2 today) — not in target role list,
                    # below seniority target, Gate 6 catches analytics_engineering role_focus anyway
    "architect",    # solutions/data/cloud/analytics architects — not analytics leadership
    "director",     # Director-level roles — above target seniority per CLAUDE.md (VP+)
                    # tradeoff: UK bank "Associate Director" (sometimes Sr Manager equiv) — acceptable
    "governance",   # data/AI governance roles — always non-analytics
    "devops",       # DevOps Manager/Lead beyond "devops engineer"
    # ── Analyst role types that are never target roles ────────────────────────
    "site reliability",
    "hr analyst", "people analyst", "payroll analyst", "payroll manager",
    "paid social", "seo analyst", "seo manager", "digital marketing analyst",
    "cro specialist", "cro consultant", "financial analyst", "finance analyst",
    "bi developer", "etl developer",
    "data science", "scientist", "graduate analyst",
    # ── Non-analytics operational/strategy titles ─────────────────────────────
    "revenue operations manager", "revenue operations lead",    # RevOps = Salesforce/CRM ops
    "business operations manager", "business operations lead",  # BizOps without analytics
    "talent acquisition", "talent partner",                     # HR/recruiting
    "people operations", "people partner",                      # HR
    "commercial transformation",                                # Strategy consulting type
    # ── Accounting / finance domain ───────────────────────────────────────────
    "statutory reporting", "accounts payable", "accounts receivable",
    "financial accountant", "fp&a", "actuarial",
    # ── Supply chain / infrastructure ops ────────────────────────────────────
    "demand manager", "data center operations", "data centre operations",
    # ── Customer success / services ops ──────────────────────────────────────
    "customer success manager",
]

# ── Language detection gate (NL/SE only) ─────────────────────────────────────
# Phrases that indicate the JD requires native Dutch or Swedish proficiency.
# Gate fires when 2+ distinct phrases are found in the first 2,000 chars.
# Threshold of 2 avoids false positives from incidental single mentions.
_DUTCH_PHRASES = [
    "je beschikt over", "wij zoeken", "functie-eisen", "you must speak dutch",
    "dutch language required", "dutch is required", "native dutch", "dutch is a must",
    "beheers je", "in het nederlands", "nederlandstalig", "vloeiend nederlands",
    "dutch fluency", "je werkt", "je hebt",
]
_SWEDISH_PHRASES = [
    "vi söker", "du har", "arbetsuppgifter", "svenska krävs", "swedish is required",
    "swedish language required", "fluent in swedish", "flytande svenska",
    "kräver svenska", "du behärskar svenska", "svenska är ett krav",
]
# Title-level language words — unambiguous non-English words that only appear in
# Dutch/Swedish job titles. One match in the title is sufficient to reject (titles
# are short; a non-English word in a title is a strong signal the role requires fluency).
_DUTCH_TITLE_WORDS   = ["strategisch", "analist", "hoofd", "medewerker", "adviseur", "bedrijfsanalist"]
_SWEDISH_TITLE_WORDS = ["analytiker", "ansvarig", "ingenjör"]

# Companies that brand analytics-focused roles as "Data Science" —
# their jobs bypass the "data science" substring check and go to Claude Pass 2.
_ANALYTICS_NAMED_DS_COMPANIES = {"deliveroo"}

# SAP-primary title gate — only unambiguous title-level signals.
# Roles where SAP IS the title (SAP Data Analyst, SAP BI Analyst) are clearly
# SAP-tooling-first. Roles that merely mention SAP in the JD are NOT blocked here —
# Claude handles those in Pass 2 via low domain/skills scores.
_SAP_PRIMARY_TITLE_KW = [
    "sap analyst", "sap data analyst", "sap bi analyst", "sap bi developer",
    "sap reporting analyst", "sap functional analyst", "sap hana analyst",
]

# ── classify_title import for Tier-4 Pass 1 gate ─────────────────────────────
# Reuses the deterministic title classifier from classify_title.py.
# Gate fires when pts == 5 (Tier 4 — non-senior, non-lead BA/DA roles).
# Wrapped in try/except so a missing file silently skips the gate.
try:
    import importlib.util as _ilu_ct
    _ct_spec = _ilu_ct.spec_from_file_location(
        "classify_title", Path(__file__).parent / "classify_title.py"
    )
    _ct_mod = _ilu_ct.module_from_spec(_ct_spec)
    _ct_spec.loader.exec_module(_ct_mod)
    _classify_title = _ct_mod.classify_title
except Exception:
    _classify_title = None  # safe fallback — gate skipped if import fails

# Pass 1 keyword gates — high-confidence, low false-positive phrases in JD description.
# These are unambiguous objective signals that don't require semantic interpretation.

_VISA_DENIAL_KW = [
    # ── Explicit sponsorship refusal ─────────────────────────────────────────
    "cannot sponsor",
    "unable to sponsor",
    "no visa sponsorship",
    "visa sponsorship is not available",
    "visa sponsorship is not provided",
    "sponsorship is not available",
    "sponsorship cannot be provided",
    "sponsorship will not be provided",
    "cannot provide visa sponsorship",
    "we cannot provide visa sponsorship",
    "we do not offer sponsorship",
    "we are unable to offer sponsorship",
    "we are unable to provide visa sponsorship",
    "we cannot offer sponsorship",
    "we are not in a position to sponsor",
    "unfortunately we are unable to sponsor",
    "no work visa sponsorship",
    "no sponsorship available",
    "sponsorship not available",
    "no relocation or visa sponsorship",
    "work permit sponsorship is not available",
    # ── Right-to-work denial phrases — UK ───────────────────────────────────
    "must have the right to work in the uk without sponsorship",
    "must already have the right to work in the uk",
    "must already hold the right to work",
    "applicants must have the right to work in the uk",
    "you must have the right to work in the uk",
    "candidates must have the right to work in the uk",
    "right to work in the uk is required",
    "right to work in the united kingdom is required",
    "must be eligible to work in the uk without",
    "existing right to work in the uk",
    "must have legal right to work in the uk",
    "must hold a valid right to work in the uk",
    "must hold the right to work in the uk",
    # ── LinkedIn checkbox-style fields ───────────────────────────────────────
    "authorized to work in the united kingdom",
    "authorised to work in the united kingdom",
    "authorized to work in the uk",
    "authorised to work in the uk",
    "authorization to work in the uk",
    "authorisation to work in the uk",
    "must be authorized to work in",
    "must be authorised to work in",
]

# Netherlands — phrases that unambiguously deny non-EU applicants or refuse sponsorship
_VISA_DENIAL_KW_NL = [
    # ── Right-to-work denial — NL ────────────────────────────────────────────
    "must have the right to work in the netherlands",
    "must already have the right to work in the netherlands",
    "existing right to work in the netherlands",
    "right to work in the netherlands is required",
    "must have legal right to work in the netherlands",
    "must have existing right to work in the netherlands",
    "must be eligible to work in the netherlands without",
    "eligible to work in the netherlands without a work permit",
    # ── Authorization to work — NL ───────────────────────────────────────────
    "must be authorized to work in the netherlands",
    "must be authorised to work in the netherlands",
    "authorized to work in the netherlands",
    "authorised to work in the netherlands",
    "dutch work authorization required",
    "dutch work authorisation required",
    "must have valid dutch work authorization",
    "must have valid dutch work authorisation",
    # ── EU/EEA citizenship requirement ──────────────────────────────────────
    "eu/eea candidates only",
    "eu or eea candidates only",
    "eu/eea citizens only",
    "eu or eea citizens only",
    "must be an eu citizen",
    "must be an eu/eea citizen",
    "must hold eu citizenship",
    "eu citizenship required",
    "eu/eea citizenship required",
    # ── Work permit refusal — NL ─────────────────────────────────────────────
    "no ind sponsorship",
    "cannot sponsor ind applications",
    "no sponsorship for highly skilled migrant",
    "no sponsorship for a highly skilled migrant permit",
    "cannot sponsor a highly skilled migrant permit",
    "cannot sponsor a work permit",
    "no work permit sponsorship",
    "we do not sponsor work permits",
    "we cannot sponsor a work permit",
    "no work permit support",
    "we are unable to support visa applications",
    "we cannot assist with work permit applications",
    "must have valid dutch residency",
    "must have dutch residency",
    # ── EU work authorization ────────────────────────────────────────────────
    "eu work permit required",
    "eu work permit is required",
    "must hold an eu work permit",
    "must have eu work authorization",
    "must have eu work authorisation",
]

# Sweden — phrases that unambiguously deny non-EU applicants or refuse sponsorship
_VISA_DENIAL_KW_SE = [
    # ── Right-to-work denial — SE ────────────────────────────────────────────
    "must have the right to work in sweden",
    "must already have the right to work in sweden",
    "existing right to work in sweden",
    "right to work in sweden is required",
    "must have legal right to work in sweden",
    "must be eligible to work in sweden without",
    "eligible to work in sweden without a work permit",
    # ── Authorization to work — SE ───────────────────────────────────────────
    "must be authorized to work in sweden",
    "must be authorised to work in sweden",
    "authorized to work in sweden",
    "authorised to work in sweden",
    "swedish work authorization required",
    "swedish work authorisation required",
    "must have valid swedish work authorization",
    "must have valid swedish work authorisation",
    # ── EU/EEA citizenship requirement ──────────────────────────────────────
    "eu/eea candidates only",
    "eu or eea candidates only",
    "eu/eea citizens only",
    "eu or eea citizens only",
    "must be an eu citizen",
    "must be an eu/eea citizen",
    "must hold eu citizenship",
    "eu citizenship required",
    "eu/eea citizenship required",
    # ── Work permit refusal — SE ─────────────────────────────────────────────
    "swedish work permit required",
    "must hold a valid swedish work permit",
    "must have a valid swedish work permit",
    "cannot sponsor arbetstillstånd",
    "cannot sponsor a work permit",
    "no work permit sponsorship",
    "we do not sponsor work permits",
    "we cannot sponsor a work permit",
    "no work permit support",
    "we are unable to support visa applications",
    "we cannot assist with work permit applications",
    "must have swedish residency",
    # ── EU work authorization ────────────────────────────────────────────────
    "eu work permit required",
    "eu work permit is required",
    "must hold an eu work permit",
    "must have eu work authorization",
    "must have eu work authorisation",
    "must have valid eu work authorization",
    "must have valid eu work authorisation",
]

_CONTRACT_KW = [
    "day rate",
    "day-rate",
    " ir35",        # leading space avoids matching e.g. "their35"
    "ir35 ",
    "ir35,",
    "ir35.",
    "inside ir35",
    "outside ir35",
    "fixed-term contract",
    "fixed term contract",
    " ftc ",        # fixed term contract abbreviation
    "(ftc)",
    "interim role",
    "interim position",
    "interim contract",
    "6-month contract",
    "12-month contract",
    "6 month contract",
    "12 month contract",
    "6month contract",
    "12month contract",
]

_REMOTE_ONLY_KW = [
    "100% remote",
    "fully remote",
    "remote only",
    "remote-only",
    "permanently remote",
    "entirely remote",
    "all remote",
    "completely remote",
]


def _location_in_tiers(location_str: str, market: str = "uk") -> tuple[bool, str]:
    """Returns (passes, tier_name). Reads native LinkedIn location field."""
    loc = location_str.lower()
    if market == "nl":
        for city in NL_TIER1:
            if city in loc: return True, "nl_tier1"
        return False, "outside"
    if market == "se":
        for city in SE_TIER1:
            if city in loc: return True, "se_tier1"
        return False, "outside"
    # UK (default — unchanged)
    for city in TIER1:
        if city in loc: return True, "tier1"
    for city in TIER2:
        if city in loc: return True, "tier2"
    for city in TIER3:
        if city in loc: return True, "tier3"
    return False, "outside"


def _posting_age_days(posted_date: str) -> Optional[int]:
    """Return how many days ago the job was posted, or None if unparseable."""
    if not posted_date:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})", str(posted_date))
    if not m:
        return None
    try:
        return (date.today() - date.fromisoformat(m.group(1))).days
    except ValueError:
        return None


def _posting_too_old(posted_date: str) -> bool:
    """True if the posting is older than MAX_AGE days (hard-reject, don't track)."""
    age = _posting_age_days(posted_date)
    return age is not None and age > MAX_AGE


def _posting_is_stale(posted_date: str, threshold: int = STALE_AGE_DAYS) -> bool:
    """True if the posting is older than threshold days but within MAX_AGE (track as Stale)."""
    age = _posting_age_days(posted_date)
    return age is not None and threshold < age <= MAX_AGE


def recategorize_stale_entries() -> int:
    """
    Retroactively update existing 'Shortlisted'/'Review Needed' tracker entries to
    'Stale' if their posted_date is now older than STALE_AGE_DAYS. Called at the
    start of every score run so the tracker stays current between scout runs.
    Returns count of entries updated.
    """
    updated = 0
    for app in tracker:
        if app.get("status") not in {"Shortlisted", "Review Needed"}:
            continue
        age = _posting_age_days(app.get("posted_date") or "")
        _thr = _get_stale_threshold(app.get("market", "uk"))
        if age is None or age <= _thr:
            continue
        old_status = app["status"]
        app["status"] = "Stale"
        app.setdefault("status_history", []).append({
            "status": "Stale",
            "date":   TODAY,
            "source": "score_jobs_stale_sweep",
            "reason": f"Retroactive: posting now {age}d old (>{_thr}d threshold)",
        })
        print(f"  [stale-sweep] {app.get('company')} / {app.get('role')} "
              f"({old_status} → Stale, age={age}d)")
        updated += 1
    if updated and not DRY_RUN:
        tracker_raw["applications"] = tracker
        TRACKER_PATH.write_text(json.dumps(tracker_raw, indent=2, ensure_ascii=False))
    return updated


_SALARY_HEADER_RE = re.compile(
    r'^(?:salary|pay|compensation|remuneration|package|annual\s+salary|total\s+compensation|reward)'
    r'(?:\s+(?:range|package|information))?\s*:\s*(.+)',
    re.IGNORECASE | re.MULTILINE
)


def _extract_salary_hint_from_description(desc: str) -> Optional[str]:
    """
    Find an explicitly labelled salary line (e.g. 'Salary: From £53k+') in the
    first 2000 chars of the description where structured headers always appear.
    Returns the extracted value string, or None if not found.
    """
    import html as _html
    clean = _html.unescape(desc[:2000])
    m = _SALARY_HEADER_RE.search(clean)
    if m:
        return m.group(1).strip()
    return None


def _parse_native_salary(native_str: str) -> Optional[tuple[int, int]]:
    """
    Parse LinkedIn's native salary field only.
    Handles: '75K GBP/yr - 90K GBP/yr' and '£75,000 - £90,000/year'.
    Returns (lower, upper) in GBP integers, or None if unparseable.
    Only used for Pass 1 hard-fail gate — Claude is authoritative for salary_stated.
    """
    if not native_str:
        return None
    has_k = bool(re.search(r'\d\s*[kK]\b', native_str))
    mult = 1000 if has_k else 1

    def _parse_num(s: str) -> Optional[int]:
        try:
            return int(float(s.replace(',', '').replace('k', '').replace('K', ''))) * mult
        except (ValueError, OverflowError):
            return None

    # Pattern A: currency-code suffix range "75K GBP/yr - 90K GBP/yr"
    m = re.search(
        r'(\d[\d,\.]+)\s*[kK]?\s*(?:GBP)[^\d\-–]*[\-–]\s*(\d[\d,\.]+)',
        native_str, re.IGNORECASE
    )
    if m:
        lo, hi = _parse_num(m.group(1)), _parse_num(m.group(2))
        if lo and hi:
            return lo, hi

    # Pattern B: symbol-prefix range "£75,000 - £90,000" or "£75k–£90k"
    m = re.search(
        r'(?:£|\$|€)\s*(\d[\d,\.]+)\s*[kK]?\s*(?:/\s*(?:yr|year|annum))?\s*[\-–]\s*(?:£|\$|€)?\s*(\d[\d,\.]+)',
        native_str, re.IGNORECASE
    )
    if m:
        lo, hi = _parse_num(m.group(1)), _parse_num(m.group(2))
        if lo and hi:
            return lo, hi

    return None


# ── Matching & dedup (new deterministic logic) ────────────────────────────────

KNOWN_AGENCIES = {
    "hackajob", "harnham", "robert walters", "michael page", "lorien",
    "burns sheehan", "glocomms", "la fosse", "data idols", "vma group",
    "sf technology", "w talent", "dune advisors", "salt",
}

REPOST_GAP_DAYS   = 21   # days between old and new posting → genuine repost
TERMINAL_STATUSES = {"Rejected", "Withdrawn", "Stale", "Auto-Rejected", "Duplicate"}


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance, case-insensitive. Returns 99 if length diff > 3 (early exit)."""
    a, b = a.lower().strip(), b.lower().strip()
    if a == b:
        return 0
    if abs(len(a) - len(b)) > 3:
        return 99
    m, n = len(a), len(b)
    dp = list(range(n + 1))
    for i in range(1, m + 1):
        prev, dp[0] = dp[0], i
        for j in range(1, n + 1):
            temp = dp[j]
            dp[j] = prev if a[i - 1] == b[j - 1] else 1 + min(dp[j], dp[j - 1], prev)
            prev = temp
    return dp[n]


def _field_matches(a: str, b: str) -> bool:
    """True if two strings match within 3 character edits (case-insensitive)."""
    return bool(a and b and _edit_distance(a, b) <= 3)


def _extract_hiring_company_hint(job: dict) -> Optional[str]:
    """
    If the posting company is a known agency, try to extract the real employer
    from the description. Returns the hiring company name or None.
    Note: scraped descriptions often have no whitespace between words (HTML strip
    artifact), so patterns use optional spaces not required spaces.
    """
    company = (job.get("company_name") or "").lower()
    if not any(re.search(r'\b' + re.escape(ag) + r'\b', company) for ag in KNOWN_AGENCIES):
        return None
    desc = job.get("description") or ""
    m = re.search(r"collaborating\s*with\s*([A-Z][\w\s&\-]+?)\s*to\s*connect", desc, re.IGNORECASE)
    if m: return m.group(1).strip()
    m = re.search(r"on behalf of\s*([A-Z][\w\s&\-,]+?)[\.,]", desc, re.IGNORECASE)
    if m: return m.group(1).strip()
    m = re.search(r"join\s*us\s*at\s*([A-Z][\w\s&\-]+?)\s+as\s+(?:a|an|the)\b", desc, re.IGNORECASE)
    if m: return m.group(1).strip()
    m = re.search(r"partnering\s*with\s*([A-Z][\w\s&\-]+?)\s+to\s+\w+", desc, re.IGNORECASE)
    if m: return m.group(1).strip()
    return None


def _scoring_date_proxy(entry: dict) -> Optional[str]:
    """Compute latest_scoring_date for entries that don't have it stored."""
    if entry.get("latest_scoring_date"):
        return entry["latest_scoring_date"]
    if entry.get("fit_score") is None:
        return None
    if entry.get("source") == "excel_import":
        return "2026-06-18"   # import date as proxy for all excel-imported scored entries
    for h in (entry.get("status_history") or []):
        d = h.get("date") or ""
        if d and len(d) >= 10:
            return d[:10]
    return None


def _build_existing_pool() -> list:
    """Build unified pool of tracker + auto_rejected entries for match checking."""
    pool = []
    for e in tracker:
        pool.append({
            "id":                  e.get("id"),
            "company":             (e.get("company") or "").lower().strip(),
            "role":                (e.get("role") or "").lower().strip(),
            "jd_url":              (e.get("jd_url") or "").strip(),
            "status":              e.get("status", ""),
            "posted_date":         (e.get("posted_date") or "")[:10],
            "fit_score":           e.get("fit_score"),
            "score_exists":        e.get("score_exists", e.get("fit_score") is not None),
            "latest_scoring_date": _scoring_date_proxy(e),
            "market":              e.get("market", "uk") or "uk",
        })
    for e in auto_rejected:
        pool.append({
            "id":                  e.get("id"),
            "company":             (e.get("company") or "").lower().strip(),
            "role":                (e.get("role") or "").lower().strip(),
            "jd_url":              (e.get("jd_url") or "").strip(),
            "status":              "Auto-Rejected",
            "posted_date":         (e.get("posted_date") or "")[:10],
            "fit_score":           e.get("fit_score"),
            "score_exists":        e.get("fit_score") is not None,
            "latest_scoring_date": (e.get("scout_run_date") or "")[:10] or None,
            "market":              e.get("market", "uk") or "uk",
        })
    return pool


# Built once at module load — shared across all jobs in this batch
_EXISTING_POOL: list = _build_existing_pool()


def _find_match(job: dict) -> dict:
    """
    Determines the relationship between a scraped job and existing tracker/auto_rejected.

    Returns:
      decision:       "dedup" | "update_in_place" | "new_entry" | "no_match"
      matched_entry:  the best-matching pool entry dict, or None
      matched_id:     matched entry's id string, or None

    Decision tree:
      1. jd_url exact match → dedup
      2. company/hiring_co + role match (≤3 char edit distance each), gap ≤ 1 day → dedup
      3. match, gap 2–21 days → update_in_place
      4. match, gap > 21 days, terminal status → new_entry (old entry kept)
      5. match, gap > 21 days, active/unknown status → update_in_place
      6. no match → no_match
      Unknown gap (missing dates) → update_in_place (safe default)
    """
    jd_url     = (job.get("job_url") or job.get("url") or "").strip()
    company    = (job.get("company_name") or "").lower().strip()
    title      = (job.get("job_title") or "").lower().strip()
    new_posted = (job.get("posted_date") or job.get("postedDate") or "")[:10]

    is_agency  = any(re.search(r'\b' + re.escape(ag) + r'\b', company) for ag in KNOWN_AGENCIES)
    hiring_co  = _extract_hiring_company_hint(job)

    # Signal 1: jd_url exact match → always dedup
    if jd_url:
        for e in _EXISTING_POOL:
            if e["jd_url"] and jd_url == e["jd_url"]:
                print(f"    [match] jd_url exact → dedup ({e['id']}/{e['status']})")
                return {"decision": "dedup", "matched_entry": e, "matched_id": e["id"]}

    # Signal 2: company/hiring_co + role (≤3 char edit distance each)
    if is_agency and not hiring_co:
        # Known agency with no identifiable client → no company+role fallback
        return {"decision": "no_match", "matched_entry": None, "matched_id": None}

    match_co = hiring_co.lower().strip() if (is_agency and hiring_co) else company

    incoming_market = (job.get("market") or "uk").lower()
    candidates = [
        e for e in _EXISTING_POOL
        if _field_matches(match_co, e["company"]) and _field_matches(title, e["role"])
        # Different markets = different job listings (same company can post in UK and NL/SE)
        and (not incoming_market or not e.get("market") or incoming_market == e.get("market", "uk"))
    ]
    if not candidates:
        return {"decision": "no_match", "matched_entry": None, "matched_id": None}

    # Among multiple matches use the one with latest posted_date
    best        = max(candidates, key=lambda e: e["posted_date"])
    best_posted = best["posted_date"]

    gap_days: Optional[int] = None
    if new_posted and best_posted:
        try:
            gap_days = (date.fromisoformat(new_posted) - date.fromisoformat(best_posted)).days
        except ValueError:
            pass

    if gap_days is not None and abs(gap_days) <= 1:
        print(f"    [match] {match_co}+role, gap={gap_days}d → dedup ({best['id']})")
        return {"decision": "dedup", "matched_entry": best, "matched_id": best["id"]}

    existing_status = best["status"]

    if gap_days is not None and gap_days > REPOST_GAP_DAYS and existing_status in TERMINAL_STATUSES:
        print(f"    [match] {match_co}+role, gap={gap_days}d>{REPOST_GAP_DAYS}, "
              f"terminal → new_entry (supersedes {best['id']})")
        return {"decision": "new_entry", "matched_entry": best, "matched_id": best["id"]}

    reason = (f"gap={gap_days}d" if gap_days is not None else "gap unknown")
    print(f"    [match] {match_co}+role, {reason} → update_in_place ({best['id']}/{existing_status})")
    return {"decision": "update_in_place", "matched_entry": best, "matched_id": best["id"]}


def _reconstruct_result_from_entry(entry: dict) -> dict:
    """Build a minimal score result dict from an existing tracker entry for score reuse."""
    score  = entry.get("fit_score") or 0
    action = "shortlist" if score >= 75 else "review" if score >= 60 else "reject"
    return {
        "action":                     action,
        "fit_score":                  entry.get("fit_score"),
        "fit_score_breakdown":        entry.get("fit_score_breakdown"),
        "visa_sponsorship_status":    entry.get("visa_sponsorship_status", "Unconfirmed"),
        "salary_stated":              entry.get("salary_stated"),
        "salary_estimate":            entry.get("salary_estimate"),
        "salary_estimate_confidence": entry.get("salary_estimate_confidence"),
        "salary_gate":                ("passed" if entry.get("salary_meets_threshold") is True
                                       else "tbc"  if entry.get("salary_meets_threshold") is None
                                       else "failed"),
        "work_mode":                  entry.get("work_mode"),
        "experience_req":             entry.get("experience_req"),
        "ats_type_from_jd":           entry.get("ats_type"),
        "is_agency_post":             entry.get("is_agency_post", False),
        "actual_hiring_company":      entry.get("actual_hiring_company"),
        "is_contract":                entry.get("is_contract", False),
        "is_remote_only":             entry.get("is_remote_only", False),
        "is_investment_domain":       entry.get("is_investment_domain", False),
        "is_sap_primary":             entry.get("is_sap_primary", False),
        "apply_recommendation":       entry.get("apply_recommendation"),
        "company_sponsor_kb":         entry.get("company_sponsor_kb", "Uncertain"),
        "flags":                      entry.get("flags", []),
        "_score_reused_from":         entry.get("id"),
    }


def pass1_filter(job: dict) -> tuple[str, str]:
    """
    Returns (result, reason) where result is "pass", "reject", or "stale".
    Uses ONLY native LinkedIn/Apify metadata fields — no JD description parsing.
      "pass"   → send to Pass 2 (Claude API)
      "reject" → P1-REJECT, add to auto_rejected (hard cutoff)
      "stale"  → add to tracker as status=Stale, skip Claude API

    Rejection hierarchy (most objective gate first):
      1. Posting too old (>30d)  — hardest cut, no JD read needed
      2. Remote-only             — role-type gate, native LinkedIn field
      3. Contract/non-permanent  — role-type gate, native LinkedIn field
      4. Salary below £85k       — only when stated; Pass 2 handles TBC cases
      5. Location outside tiers  — native LinkedIn field
      6. Title hard-reject       — last: lets other gates fire first, Pass 2 can still see edge cases
    Note: "No Visa Sponsorship (stated)" requires JD text reading → Pass 2 only (not in Pass 1).
    """
    market = job.get("market", "uk")
    title  = (job.get("job_title") or "").lower()
    posted = job.get("posted_date") or job.get("postedDate") or ""

    # 1. Posting age (from LinkedIn posted_date — poster-declared)
    #    >MAX_AGE days → hard reject (not worth tracking at all)
    #    >STALE_AGE_DAYS days → Stale ONLY if title is analytics-relevant;
    #                           otherwise reject (no value logging off-topic stale jobs)
    if _posting_too_old(posted):
        return "reject", f"Posting too old (posted {posted}, >{MAX_AGE}d)"
    _stale_thr = _get_stale_threshold(market)
    if _posting_is_stale(posted, _stale_thr):
        if any(kw in title for kw in ANALYTICS_TITLE_KW):
            return "stale", f"Posting is stale (posted {posted}, >{_stale_thr}d old)"
        return "reject", f"Stale + non-analytics title (posted {posted}, >{_stale_thr}d old)"

    # 2. Work type (from LinkedIn structured fields — poster-declared)
    # Check multiple field names — Apify's actor schema varies by version.
    # Also check boolean remote_allowed fields for fast rejection without API cost.
    _WORK_TYPE_FIELDS = ["work_type", "workType", "workplace_type", "workplaceType", "workMode"]
    _REMOTE_BOOL_FIELDS = ["remote_allowed", "remoteAllowed", "is_remote", "isRemote"]
    native_work_type = next(
        (str(job.get(f)) for f in _WORK_TYPE_FIELDS if job.get(f) is not None and job.get(f) != ""),
        ""
    ).lower()
    remote_bool = any(job.get(f) is True for f in _REMOTE_BOOL_FIELDS)
    if remote_bool:
        return "reject", f"Remote-only (structured remote_allowed flag is True)"
    if native_work_type and "remote" in native_work_type and "hybrid" not in native_work_type:
        return "reject", f"Remote-only (LinkedIn native work_type: '{native_work_type}')"
    # Text-based remote hint (set by enrich_jobs.py from JD description).
    # Only fires when native_work_type is empty (Apify always-empty case) to save API cost.
    if job.get("remote_hint") and not native_work_type:
        return "reject", "Remote-only (unambiguous text signal in JD description)"

    # 3. Job type (from LinkedIn job_type field — poster-declared)
    # Only reject when LinkedIn itself marks it non-permanent.
    native_job_type = (job.get("job_type") or job.get("jobType") or "").lower()
    if native_job_type and any(t in native_job_type for t in ["contract", "part-time", "temporary", "freelance"]):
        return "reject", f"Non-permanent role (LinkedIn native job_type: '{native_job_type}')"

    # 4. Native salary hard-fail (from LinkedIn salary card field — poster-declared)
    # Only fires for UK market: NL/SE salaries are in EUR/SEK and can't be compared to £85k.
    native_salary = job.get("salary") or ""
    if market == "uk" and native_salary:
        parsed = _parse_native_salary(native_salary)
        if parsed:
            lo, hi = parsed
            if hi < 85000 and lo < 85000:
                return "reject", f"Salary below £85k threshold (LinkedIn native: '{native_salary}')"

    # 5. Location (from LinkedIn location field — poster-declared)
    location = job.get("location") or job.get("jobLocation") or ""
    in_tier, _ = _location_in_tiers(location, market)
    if not in_tier:
        return "reject", f"Location '{location}' outside acceptable tiers (market={market})"

    # 5b. Language gate (NL/SE only) — Dutch/Swedish-only roles are hard disqualifiers.
    # Three checks: (a) job title — 1 unambiguous word is enough (titles are short);
    #               (b) description — 2+ distinct requirement phrases;
    #               (c) NL only — JD text written in Dutch (8+ distinct Dutch function words).
    if market in ("nl", "se"):
        _lang_phrases = _DUTCH_PHRASES if market == "nl" else _SWEDISH_PHRASES
        _lang_label   = "Dutch" if market == "nl" else "Swedish"
        # (a) Title-level check
        _title_words = _DUTCH_TITLE_WORDS if market == "nl" else _SWEDISH_TITLE_WORDS
        _title_text  = (job.get("job_title") or "").lower()
        _title_hits  = sum(1 for w in _title_words if re.search(r'\b' + w + r'\b', _title_text))
        if _title_hits >= 1:
            return "reject", (
                f"Job title contains {_lang_label} language indicator "
                f"— likely requires native {_lang_label} proficiency"
            )
        # (b) Description-level requirement phrases check
        _desc_lang    = (job.get("description") or "")[:2000].lower()
        _lang_hits    = sum(1 for p in _lang_phrases if p in _desc_lang)
        if _lang_hits >= 2:
            return "reject", f"Role requires {_lang_label} language proficiency (found {_lang_hits} signal phrases)"
        # (c) NL only — detect JD text written in Dutch
        # A JD written entirely in Dutch implies Dutch proficiency even if not stated explicitly.
        # Threshold: 8+ distinct Dutch function words in first 3,000 chars.
        if market == "nl":
            _DUTCH_FUNCTION_WORDS = {
                "van", "de", "het", "een", "voor", "met", "zijn", "worden",
                "die", "dat", "ook", "maar", "als", "niet", "naar", "bij",
                "door", "over", "kan", "wordt", "geen", "hun", "zich", "nog",
                "heeft", "deze", "wij", "je", "jij",
            }
            _desc_jd   = (job.get("description") or "")[:3000].lower()
            _jd_words  = set(re.findall(r'\b[a-z]{2,}\b', _desc_jd))
            _dutch_hits = len(_jd_words & _DUTCH_FUNCTION_WORDS)
            if _dutch_hits >= 8:
                return "reject", (
                    f"JD text written in Dutch ({_dutch_hits} Dutch function words detected) "
                    "— likely requires Dutch proficiency"
                )

    # 6. Hard-reject titles (from LinkedIn job_title field — poster-declared)
    # Last gate: lets age/remote/contract/salary/location fire first.
    # Pass 2 (Claude) is the final arbiter for borderline titles that pass all gates above.
    company_lower = (job.get("company_name") or "").lower()
    for bad in TITLE_REJECT_CONTAINS:
        if bad == "data science" and any(co in company_lower for co in _ANALYTICS_NAMED_DS_COMPANIES):
            continue   # Deliveroo names analytics roles as "Data Science" — pass to Claude
        if bad in title:
            return "reject", f"Title contains '{bad}'"

    # 6a. Junior Tier-4 title gate (non-senior, non-lead analyst roles)
    # Uses classify_title.py deterministic classifier (pts==5 → Tier 4).
    # Saves the ~$0.027 Claude API call for clearly sub-target titles.
    # Tier 3+ (Senior DA, Senior BA, etc.) still pass to Claude for full scoring.
    # Stale jobs return early at gate 1 and never reach this gate.
    if _classify_title is not None:
        _tier_pts, _tier_reason = _classify_title(title)
        if _tier_pts == 5:
            return "reject", (
                f"Junior Tier-4 title (non-senior/non-lead analyst): "
                f"'{job.get('job_title', '')}' — {_tier_reason}"
            )

    # 6b. SAP-primary title gate (title-level only — clear tooling-first signal)
    # Only blocks when SAP is the defining technology in the title itself.
    # JD-level SAP-primary detection is handled by Claude in Pass 2.
    for _sap_kw in _SAP_PRIMARY_TITLE_KW:
        if _sap_kw in title:
            return "reject", f"SAP-primary title: '{job.get('job_title', '')}'"

    # 7. Visa denial keywords (description — high-confidence explicit phrases only)
    # Shorter phrases like "right to work in the uk" alone are NOT included — a direct
    # employer could say that while still intending to sponsor. Only structurally
    # unambiguous denial phrases are listed.
    if market == "nl":
        _visa_kws = _VISA_DENIAL_KW + _VISA_DENIAL_KW_NL
    elif market == "se":
        _visa_kws = _VISA_DENIAL_KW + _VISA_DENIAL_KW_SE
    else:
        _visa_kws = _VISA_DENIAL_KW
    desc_lower = (job.get("description") or "").lower()
    for kw in _visa_kws:
        if kw in desc_lower:
            return "reject", f"Visa denial keyword in JD: '{kw}'"

    # 8. Contract/IR35/day-rate keywords (description — objective signals)
    desc_contract = (job.get("description") or "")[:1000].lower()
    for kw in _CONTRACT_KW:
        if kw in desc_contract:
            return "reject", f"Contract/IR35 keyword in JD: '{kw}'"

    # 9. Remote-only keywords (description — supplements native LinkedIn field gate 2)
    # High-confidence phrases only — vaguer phrasing ("distributed team") caught by Pass 2.
    desc_remote = (job.get("description") or "")[:800].lower()
    for kw in _REMOTE_ONLY_KW:
        if kw in desc_remote:
            return "reject", f"Remote-only keyword in JD: '{kw}'"

    return "pass", ""


# ─────────────────────────────────────────────────────────────────────────────
# PASS 2 — Claude API semantic scoring
# ─────────────────────────────────────────────────────────────────────────────

SCORE_SYSTEM = """You are a job fit scorer for Vinay Patidar, a Lead Analytics professional with 8+ years of experience seeking UK roles (Skilled Worker Visa sponsorship required).

CANDIDATE PROFILE:
- Target titles: Analytics Lead, Lead Data Analyst, Lead Business Analyst, Analytics Manager, Data Analytics Manager, AI Analytics Manager, Product Analytics Manager, Lead Product Analyst, Growth Analytics Lead/Manager, Analytics Transformation Lead/Manager, AI Analytics Lead, Senior Analytics Lead, Data & AI Lead/Manager, Insights Lead/Manager, Principal Data Analyst, Head of Data, Head of Analytics, Head of Data & Analytics, AI Enablement Specialist, AI & Automation Specialist, Forward Deployed AI [X], AI Practice Lead (when primary work is enabling AI workflows org-wide)
- Key experience: Flipkart (Lead BA — CRM analytics, incrementality testing, experimentation, grocery growth), BeepKart (Analytics Manager — Dynamic Pricing, geo-clustering, team of 5), DeHaat (Lead BA — collections analytics, customer scoring), IIT BHU graduate
- Core skills: SQL, Python, Tableau, BigQuery, Redshift, Looker Studio, experimentation/A/B testing, CRM analytics, pricing optimisation, customer segmentation, propensity modelling, KPI frameworks
- Employment type: MUST be permanent/full-time. Set is_contract=true and action=reject if the JD mentions: day rate, IR35, inside/outside IR35, fixed-term contract, FTC, interim, 6/12-month contract, or contractor. If uncertain default is_contract=false but add a flag.
- Work arrangement: MUST be hybrid or on-site. Set is_remote_only=true and action=reject if the role is fully remote / 100% remote with no office requirement. Hybrid = false.

SCORING RUBRIC (0-100):
  Role title match   (0-20):
    20 = Manager, Lead, Head, or Principal exact match:
         Analytics Manager / Data Analytics Manager / AI Analytics Manager /
         Product Analytics Manager / Growth Analytics Manager,
         Analytics Lead / AI Analytics Lead / Analytics Transformation Lead or Manager,
         Lead Business Analyst / Lead Product Analyst / Lead Data Analyst,
         Senior Analytics Lead / Growth Analytics Lead or Manager,
         Head of Data / Head of Analytics / Head of Data & Analytics,
         Insights Lead / Insights Manager (analytics-heavy), Data & AI Lead or Manager,
         Principal Data Analyst or Principal Analytics role
    15 = Senior non-Lead analytics role OR senior AI Enablement practitioner:
         Senior Business Analyst / Senior Analytics Engineer /
         Senior Performance Analyst / Senior Product Analyst /
         Senior Insights Analyst / BI Manager / BI Product Lead / BI Lead /
         Business Intelligence Manager / Business Intelligence Lead / Head of Business Intelligence /
         AI Enablement Specialist / AI & Automation Specialist /
         Forward Deployed AI Accelerator / AI Practice Lead /
         ANY title where the JD primary work is enabling AI workflows org-wide
         (use JD content to confirm — not just the title alone)
    10 = Senior Data Analyst specifically
     5 = Business Analyst or Data Analyst (non-senior, non-lead)
     0 = Unrelated title (Data Engineer, Data Scientist, Software Engineer, etc.)
  Domain match       (0-25): 25=product/growth/ecommerce/marketplace/fintech, 15=other tech, 5=consulting, 0=unrelated
  Skills match       (0-25): 25=7+ skills overlap, 15=4-6, 5=1-3, 0=none
  Seniority          (0-15): 15=Lead/Manager 5-10yr expected, 10=slightly senior, 5=slightly junior, 0=mismatch
  Location           (0-10): 10=London, 8=Manchester/Birmingham, 6=other Tier2 (Leeds/Reading/Cambridge/Oxford/Coventry/Leicester/Nottingham/MK/Northampton), 0=outside tiers
  Visa sponsorship   (-10/0/3/5): 5=confirmed, 3=large company (likely on UKVI sponsor register), 0=unconfirmed/unknown, -10=explicitly denied

=== AI ENABLEMENT ROLES — special scoring guidance ===
For roles where primary work is enabling AI workflows, practices, or tooling org-wide
(not traditional analytics delivery or data engineering):
  → role_focus: use "mixed" — NOT "insights_strategy" (they have technical requirements)
    and NOT "analytics_engineering" (they don't build data pipelines/dbt models)
  → Skills: Vinay's active AI engineering project (Claude Code, Anthropic API, MCP servers,
    agentic automation, prompt engineering) is DIRECTLY relevant — count these as skills match.
    Award 15 pts (4-6 skills) if AI tooling aligns; 25 pts (7+) if AI + SQL/Python also match.
  → Domain: score the company's domain normally (fintech = 25, etc.)
  → Seniority: 10 pts if JD shows cross-functional scope or org-level impact; 5 pts if unclear

=== VISA SPONSORSHIP — YOU ARE AUTHORITATIVE ===
You receive no pre-screened visa hint. Read the full job description AND the raw job metadata.
Use judgment to assess whether this employer will sponsor a UK Skilled Worker Visa for a
candidate relocating from outside the UK. You are detecting INTENT, not matching exact phrases.

Set visa_sponsorship_status = "Rejected" (score -10, action = reject) if the JD or posting
contains ANY language whose INTENT is that the candidate must already hold the right to work
in the UK independently. This includes — but is not limited to:
  - Explicit no-sponsorship statements ("we cannot sponsor", "no visa sponsorship available",
    "unable to provide visa sponsorship", "sponsorship is not available")
  - Right-to-work requirements ("must have the right to work in the UK",
    "must be eligible to work in the UK", "applicants must have the right to work without
    sponsorship", "you must already have the right to work")
  - LinkedIn "Requirements added by job poster" checkbox fields, e.g.:
    "Authorized to work in United Kingdom" / "Authorised to work in United Kingdom"
    These appear as brief bullet-point labels at the bottom of the JD, not as full
    sentences. Treat them as explicit denial signals regardless of their terse style.
  IMPORTANT: Do NOT infer visa denial from the posting company being an agency or recruiter.
  If an agency post has an anonymised or unknown client, set visa_sponsorship_status = "Unconfirmed" —
  the actual hiring company (not the agency) would sponsor, and you cannot assess their intent
  without knowing who they are. Only set Rejected if the JD text EXPLICITLY denies sponsorship.

  Apply "Rejected" ONLY when the denial is stated in plain language in the JD text itself,
  not inferred from the company type, phrasing about commute, or availability requirements.

Return Confirmed ONLY if the JD explicitly and unambiguously states the company WILL provide
sponsorship (e.g. "certificate of sponsorship", "we will sponsor your visa",
"we are a licensed UK sponsor", "visa sponsorship is available").
Return Unconfirmed if the JD does not address sponsorship at all.

=== COMPANY SIZE AS SPONSORSHIP PROXY ===
When visa_sponsorship_status would be Unconfirmed (JD silent on sponsorship):
SET visa_sponsorship = 3 if company is any of: FTSE 100/250 listed firms,
well-known global tech/fintech companies (Series C+, recognisable brand),
major UK/global banks, large UK retailers or broadcasters, Big 4/top-tier consultancies,
or any multinational clearly operating at UK scale (1,000+ UK employees, global offices).
These are almost universally on the UK Home Office Skilled Worker sponsor register.
KEEP visa_sponsorship = 0 if company is: early-stage startup (<Series B, <100 employees),
niche agency/boutique recruiter, or a company you have no knowledge of.
ALWAYS apply Rejected (-10) if ANY denial phrase appears, regardless of company size.

=== SALARY EXTRACTION ===
Find salary from ANY available source — in order of priority:
  1. Raw job metadata fields (check ALL fields in "Raw Apify Job Data" block below — look for
     any field whose name or value contains salary/compensation/pay/remuneration data, e.g.
     salary, salaryRange, salary_text, salaryText, compensationOnEmploymentTypes, pay, etc.)
  2. Job Description body text (extract stated range or figure)
  3. If absent in both: generate an estimate (see below)

Extract ONLY compensation for this specific role — NOT company revenue, ARR, funding, bonuses,
equity valuations, or any other business metrics.

SALARY RANGE RULE: When salary is a RANGE (e.g. "€90,000–€120,000", "£75k–£95k"):
  → Check ONLY the UPPER end of the range against the threshold
  → Upper end > threshold → salary_gate = "passed" (even if lower end is below threshold)
  → Only set salary_gate = "failed" if BOTH ends are clearly below threshold
  Example: "€90k–€120k" with NL threshold €100k → upper €120k > €100k → salary_gate = "passed"
  Example: "£70k–£80k" with UK threshold £85k → upper £80k < £85k → salary_gate = "failed"

Salary gate: passed if upper end >= threshold; failed if both ends clearly below threshold;
tbc if indeterminate.

If salary is not stated in metadata OR job description body:
  → For UK: estimate in GBP. London adds 10–15% over other UK cities. Use 2025–2026 UK
    analytics market rates. Set salary_stated = "Not stated (est. £X–Y)". Round to nearest £5,000.
    For NL/SE: see Market Context section below for estimation currency and format.
  → ALWAYS set salary_gate = "tbc" when using an estimate — NEVER set salary_gate = "failed"
    for an estimated salary. Only reject on clearly stated (non-estimated) below-threshold pay.
  → Set salary_estimate and salary_estimate_confidence fields accordingly.

=== AGENCY POST DETECTION ===
Set is_agency_post=true if the posting company is a recruiter/staffing agency posting on behalf of another employer.
Signals: "on behalf of", "collaborating with [Company]", "our client", "recruiting for", "connecting you with".
Extract actual_hiring_company as the real employer name if identifiable. Set null if the client is anonymised.
If is_agency_post=true and actual_hiring_company is known, use the REAL company for location and domain scoring.

=== ATS IDENTIFICATION ===
Set ats_type_from_jd to the ATS platform identified from URLs or platform references in the JD.
If both Workday and Lever appear, prefer the one nearest to "apply" language.
Return unknown if no clear platform signal. Do not invent ATS URLs.

=== ACTIONS ===
shortlist — fit_score >= 75 AND visa_sponsorship_status != Rejected AND salary_gate != failed AND is_contract=false AND is_remote_only=false
review    — fit_score 60-74 OR (>=75 but has notable flags)
reject    — fit_score < 60 OR visa_sponsorship_status=Rejected OR salary_gate=failed OR is_contract=true OR is_remote_only=true

IMPORTANT: Return ONLY valid JSON — no markdown, no explanation, nothing else.

JSON schema:
{
  "action": "shortlist|review|reject",
  "fit_score": <int 0-100>,
  "fit_score_breakdown": {
    "role_title": <0-20>,
    "domain": <0-25>,
    "skills": <0-25>,
    "seniority": <0-15>,
    "location": <0-10>,
    "visa_sponsorship": <-10|0|3|5>
  },
  "visa_sponsorship_status": "Confirmed|Rejected|Unconfirmed",
  "sponsorship_notes": "<one sentence explaining the sponsorship assessment and reasoning>",
  "salary_stated": "<display string or 'Not stated'>",
  "salary_gate": "passed|failed|tbc",
  "salary_estimate": "<market-currency range e.g. £85,000–£110,000 (UK) or €100,000–€120,000 (NL) or SEK 900,000–1,100,000 (SE)>" or null,
  "salary_estimate_confidence": "low|medium|high" or null,
  "work_mode": "Remote|Hybrid|On-site|Unknown",
  "is_remote_only": <boolean>,
  "is_contract": <boolean>,
  "is_agency_post": <boolean>,
  "actual_hiring_company": "<string or null>",
  "ats_type_from_jd": "greenhouse|lever|workday|ashby|smartrecruiters|icims|bamboohr|teamtailor|screenloop|unknown",
  "is_investment_domain": <boolean>,
  "is_sap_primary": <boolean>,
  "apply_recommendation": "Apply|Maybe|Skip",
  "company_sponsor_kb": "Known sponsor|Not a known sponsor|Uncertain",
  "experience_req": "<extracted string or null>",
  "flags": ["<flag>"],
  "rejection_reason": "<one sentence>" or null,
  "role_focus": "product_analytics|commercial_analytics|crm_analytics|bi_reporting|analytics_engineering|ai_engineering|credit_risk|management_consulting|insights_strategy|mixed"
}

experience_req: Extract the years-of-experience requirement as explicitly stated anywhere in
the JD text. Match any phrasing that specifies a number of years — e.g. "5 years working in",
"a minimum of 3 years", "ideally 6+ years", "X years' experience", "X–Y years within analytics",
"proven track record of X years", "you will have X years' hands-on" — not just the canonical
"X years of experience" pattern. Normalise to a compact display string: "5+ years",
"5–7 years", "minimum 3 years". Return null ONLY if no years figure appears anywhere in the
JD. Do NOT estimate or infer — only return what is explicitly written in the JD text.

is_investment_domain: true if the EMPLOYER is an investment firm (hedge fund, asset manager,
PE firm, investment bank, quant fund, wealth manager). Judge by industry/sector in JD, not
just keywords. false if the company merely SERVES financial clients (e.g. fintech data
platform for asset managers = false; quant analyst role at a hedge fund = true).

=== SAP-PRIMARY ROLES ===
If SAP (SAP Analytics Cloud, SAP BW/4HANA, SAP BusinessObjects, SAP HANA) is the PRIMARY
analytics environment the role operates in — not merely one tool mentioned among many — set
action=reject and rejection_reason="SAP-primary role: not aligned with candidate's SQL/Python/
Tableau/BigQuery analytics stack." SAP mentioned incidentally does NOT trigger this.
Set is_sap_primary=true in this case, false otherwise.

=== ROLE FOCUS CLASSIFICATION ===
Classify the PRIMARY nature of this role. Choose exactly one:
  "product_analytics"     — product/growth/experimentation/conversion/marketplace analytics
  "commercial_analytics"  — pricing/revenue/commercial strategy/P&L/procurement analytics
  "crm_analytics"         — customer lifecycle/CRM/segmentation/retention analytics
  "bi_reporting"          — PRIMARY output is dashboards/reports/BI tooling delivery
  "analytics_engineering" — dbt/data modelling/data pipeline/analytics platform engineering
  "ai_engineering"        — AI/ML infrastructure/platform roles: MLOps, model deployment,
                            AI platform architecture. Output = AI systems, NOT analytics insights.
                            Use when role BUILDS AI tools; NOT when role USES AI for analytics.
  "credit_risk"           — PRIMARY work is credit risk strategy, underwriting, or risk scoring.
                            Only when credit/risk modelling IS the job. Commercial analytics with
                            incidental risk exposure = "commercial_analytics", not "credit_risk".
  "management_consulting" — client-facing consulting. Known consulting firms (BCG, McKinsey,
                            Bain, Oliver Wyman, Roland Berger, Booz Allen, Big-4 advisory arms:
                            Deloitte, PwC, EY, KPMG, Accenture Strategy) regardless of title.
                            ALSO: any role whose TITLE contains "Consultant" or "Consulting"
                            at a non-product company (client-facing advisory work).
                            EXCEPTION: internal analytics role at a product company where
                            "Consultant" is a seniority label → prefer "mixed".
  "insights_strategy"     — essential skills list has NO hands-on technical analytics tools
                            (SQL, Python, Tableau, BigQuery etc.) AND deliverables are strategy
                            decks, market research, or qualitative insights. Key test: does the
                            required skills section mention ANY analytics tool? YES → not this.
                            When uncertain, prefer "mixed".
  "mixed"                 — analytics strategy primary; BI or consulting present but not dominant

Distinctions: Head of Analytics who owns BI = "mixed"; BI Manager 80% Tableau = "bi_reporting";
Lead BA at McKinsey = "management_consulting". PREFER "mixed" when uncertain.

=== APPLY RECOMMENDATION ===
Independent of the action field. Provide a personal recommendation for Vinay:

Apply  — fit_score >= 75 AND visa_sponsorship_status != Rejected AND salary_gate != failed
         AND fit_score_breakdown.domain >= 20 (product/tech/ecommerce/fintech)
         AND fit_score_breakdown.seniority >= 10 AND no major red flags.

Maybe  — fit_score 60-74, OR score >= 75 but with notable caveats: consulting/services domain
         (domain_score <= 10), seniority_score = 5, salary_gate = tbc for unknown company,
         or visa_sponsorship_status = Unconfirmed for an early-stage/unknown company.

Skip   — fit_score < 60, OR visa_sponsorship_status = Rejected, OR salary_gate = failed,
         OR is_contract = true, OR is_remote_only = true, OR is_sap_primary = true,
         OR Tier-4 title (role_title = 5 in fit_score_breakdown).

=== COMPANY VISA SPONSOR KNOWLEDGE BASE ===
SEPARATE from visa_sponsorship_status (which reads JD text). Uses your training knowledge
(cutoff August 2025). Base on the ACTUAL HIRING COMPANY (use actual_hiring_company if
is_agency_post=true; if actual_hiring_company is null, return "Uncertain").

"Known sponsor" — ONLY when highly confident as of training data: FTSE 100/250,
  major global tech (Google, Meta, Amazon, Microsoft, Apple, Salesforce, Adobe, Spotify),
  Big 4 / top consulting (Deloitte, KPMG, PwC, EY, Accenture, McKinsey, BCG, Bain),
  major UK/global banks (Barclays, HSBC, Lloyds, NatWest, Goldman Sachs, JPMorgan),
  major UK fintechs with large UK headcount (Wise, Revolut, Monzo, Checkout.com),
  large UK retailers / broadcasters (Tesco, Sainsbury's, M&S, Sky, BT, Virgin, BBC).

"Not a known sponsor" — ONLY when certain the company does not/cannot sponsor. Very rare.

"Uncertain" — DEFAULT. Use for early-stage startups, Series A/B, niche agencies, any company
  where confidence is low, or companies founded/rebranded after 2023. PREFER Uncertain."""


_MARKET_ADDONS = {
    "nl": """
=== MARKET CONTEXT: Netherlands ===
Location (0-10): 10=Amsterdam, 8=Rotterdam/The Hague/Utrecht, 0=outside NL Tier 1.
Visa: kennismigrant or EU Blue Card. Reject if JD has explicit EU/NL work-right requirement.
CRITICAL: Do NOT use "UK Skilled Worker Visa" or any UK visa language for this role.
  All visa/sponsorship notes must reference kennismigrant or EU Blue Card only.
Salary threshold: €100,000 (not £85k). Show in EUR. Use salary_gate="tbc" if not stated.
SALARY RANGE: check UPPER end only — upper > €100,000 → salary_gate = "passed".
MONTHLY SALARY: If salary is stated per month (e.g. "€6,500/month", "€7,100 gross/month",
  "per maand"), convert to annual by multiplying × 12 ONLY. Do NOT add estimated bonuses or
  allowances. Compare the annual figure to €100,000. Example: €7,100/month × 12 = €85,200 <
  €100,000 → salary_gate = "failed". Do NOT set salary_gate="passed" for monthly salaries
  below this annual equivalent.
Salary estimation (if unstated): estimate in EUR. Use 2025–2026 Netherlands analytics market
  rates. Set salary_stated = "Not stated (est. €X–Y)" e.g. "Not stated (est. €90,000–€110,000)".
  Round to nearest €5,000.
Sponsor KB (NL): Use your training knowledge. Apply company_sponsor_kb = "Known sponsor" (3 pts)
  for companies highly likely to have IND kennismigrant registration: AEX/AMX index-listed
  companies, major global tech/fintech with large NL offices (e.g. ASML, Booking.com, Adyen,
  ING, ABN AMRO, Philips, Shell, Unilever — as examples of the category, not an exhaustive list),
  and any multinational operating at Netherlands scale (500+ NL employees, global offices).
  Apply "Uncertain" for early-stage startups, small/niche firms, or companies you have low
  confidence about. Use your full training knowledge — do not restrict to named companies only.
""",
    "se": """
=== MARKET CONTEXT: Sweden ===
Location (0-10): 10=Stockholm, 8=Gothenburg/Malmö, 0=outside SE Tier 1.
Visa: Swedish arbetstillstånd (Migrationsverket). Reject if JD has explicit EU/SE work-right requirement.
CRITICAL: Do NOT use "UK Skilled Worker Visa" or any UK visa language for this role.
  All visa/sponsorship notes must reference arbetstillstånd or Migrationsverket only.
Salary threshold: SEK 1,000,000 (not £85k). Show in SEK. Use salary_gate="tbc" if not stated.
SALARY RANGE: check UPPER end only — upper > SEK 1,000,000 → salary_gate = "passed".
MONTHLY SALARY: If salary is stated per month (e.g. "SEK 80,000/month"), convert to annual
  by multiplying × 12 ONLY. Example: SEK 80,000/month × 12 = SEK 960,000 < SEK 1,000,000 →
  salary_gate = "failed". Do NOT add estimated bonuses or allowances.
Salary estimation (if unstated): estimate in SEK. Use 2025–2026 Sweden analytics market rates.
  Set salary_stated = "Not stated (est. SEK X–Y)" e.g. "Not stated (est. SEK 850,000–1,050,000)".
  Round to nearest SEK 25,000.
Sponsor KB (SE): Use your training knowledge. Apply company_sponsor_kb = "Known sponsor" (3 pts)
  for companies highly likely to have Migrationsverket arbetstillstånd approval: OMX Stockholm-listed
  companies, major global tech/fintech with large SE offices (e.g. Spotify, Klarna, Ericsson,
  Volvo, IKEA, H&M, Nordea — as examples of the category, not an exhaustive list), and any
  multinational operating at Sweden scale (500+ SE employees, global offices).
  Apply "Uncertain" for early-stage startups, small/niche firms, or companies you have low
  confidence about. Use your full training knowledge — do not restrict to named companies only.
""",
}


def _build_system_prompt(market: str = "uk") -> str:
    """Return market-appropriate system prompt for Claude scoring."""
    _intros = {
        "nl": "seeking Netherlands roles (kennismigrant or EU Blue Card sponsorship required)",
        "se": "seeking Sweden roles (Swedish arbetstillstånd/work permit sponsorship required)",
    }
    base = SCORE_SYSTEM
    if market in _intros:
        base = base.replace(
            "seeking UK roles (Skilled Worker Visa sponsorship required)",
            _intros[market],
            1,
        )
    addon = _MARKET_ADDONS.get(market, "")
    return base + addon if addon else base


def _build_user_prompt(job: dict) -> str:
    import html as _html_mod
    exp_display = (job.get("experience_years") or {}).get("display") or "Not specified"
    desc        = _html_mod.unescape((job.get("description") or "")[:10000])

    # Deterministic pre-extraction of salary from description header lines.
    # Avoids relying on Claude to find "Salary: £X" buried in prose.
    salary_hint = _extract_salary_hint_from_description(job.get("description") or "")
    salary_hint_str = salary_hint if salary_hint else "Not found by pre-scan"

    # Pass ALL non-description raw Apify fields to Claude so it can find salary,
    # work_mode, and other signals regardless of which field names Apify used.
    # Description is included separately below to avoid duplication.
    _SKIP = {"description", "descriptionHtml", "jd_text"}
    raw_meta = {k: v for k, v in job.items() if k not in _SKIP and v is not None}
    raw_meta_str = json.dumps(raw_meta, indent=2)[:2500]  # cap to stay within token budget

    _source = job.get("_source", "")
    _linkedin_salary = job.get("salary") if _source != "adzuna" else "Not available"
    _adzuna_salary   = job.get("salary") if _source == "adzuna"  else "Not available"

    return f"""Score this job for Vinay Patidar:

Market: {job.get("market", "uk").upper()}
Company: {job.get("company_name", "Unknown")}
Title: {job.get("job_title", "Unknown")}
Location: {job.get("location", "Unknown")}
Posted: {job.get("posted_date") or job.get("postedDate") or "Unknown"}
Experience Required (extracted from JD): {exp_display}
LinkedIn URL: {job.get("job_url") or job.get("url") or ""}
LinkedIn/Apify structured salary: {_linkedin_salary or "Not available"}
Adzuna structured salary: {_adzuna_salary or "Not available"}
Salary extracted from description header: {salary_hint_str}

Raw Job Data (all structured fields — use to find salary, work_mode, etc.):
{raw_meta_str}

Job Description:
{desc}"""


# Accumulates real token counts across all API calls this run (no caller changes needed)
_api_usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0}


def call_claude_api(system: str, user_prompt: str, model: str, api_key: str) -> str:
    payload = json.dumps({
        "model":      model,
        "max_tokens": 1500,
        "system":     system,
        "messages":   [{"role": "user", "content": user_prompt}],
    }).encode()

    req = _ureq.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload, method="POST"
    )
    req.add_header("x-api-key",           api_key)
    req.add_header("anthropic-version",   "2023-06-01")
    req.add_header("content-type",        "application/json")

    with _ureq.urlopen(req, timeout=45) as resp:
        result = json.loads(resp.read())

    usage = result.get("usage", {})
    _api_usage["input_tokens"]  += usage.get("input_tokens", 0)
    _api_usage["output_tokens"] += usage.get("output_tokens", 0)
    _api_usage["calls"]         += 1

    return result["content"][0]["text"]


def parse_score_response(raw: str) -> Optional[dict]:
    """Extract JSON from Claude response even if there's surrounding text."""
    raw = raw.strip()
    try: return json.loads(raw)
    except: pass
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try: return json.loads(m.group(0))
        except: pass
    return None


def _apply_hard_overrides(r: dict) -> dict:
    """Enforce critical scoring gates in code regardless of what Claude returned."""
    changed = []

    # Gate 1: Visa denial is non-negotiable
    if r.get("visa_sponsorship_status") == "Rejected" and r.get("action") != "reject":
        r["action"] = "reject"
        r.setdefault("flags", []).append("OVERRIDE: visa_sponsorship=Rejected forces reject")
        changed.append("visa_sponsorship=Rejected → action=reject")

    # Gate 2: Salary clearly below £85k — only when explicitly stated, never on Claude's estimate.
    # CLAUDE.md rule: "No salary stated → do not disqualify, flag as Salary TBC"
    # "Not stated (est. £X–Y)" is an estimate — treat as TBC, not explicit.
    _salary_stated = r.get("salary_stated") or ""
    _salary_is_explicit = (
        bool(_salary_stated)
        and _salary_stated not in ("", "Not stated", "Not provided")
        and not _salary_stated.startswith("Not stated (est.")
    )
    # Salary sanity: if the highest parsed number in salary_stated >= market threshold,
    # the upper bound of the range passes — override any Claude salary_gate=failed.
    # Prevents false rejections when Claude checks lower end of a range instead of upper.
    # Market-aware thresholds: UK £85k, NL €100k, SE SEK 1M.
    _SALARY_THRESHOLDS = {"uk": 85_000, "nl": 100_000, "se": 1_000_000}
    _market_thr = _SALARY_THRESHOLDS.get(r.get("market", "uk"), 85_000)
    # Pre-parse salary numbers once — reused by Gate 2b and Gate 2 sanity check.
    _sal_nums_raw = re.findall(r"\d[\d,]*", _salary_stated.replace(",", "")) if _salary_is_explicit else []
    _sal_nums = []
    for n in _sal_nums_raw:
        try: _sal_nums.append(int(n))
        except: pass

    # Gate 2b: Monthly salary annualization (NL/SE).
    # Claude may compare monthly figures raw against the annual threshold — this gate
    # overrides salary_gate regardless of what Claude returned, using ×12 only.
    if _salary_is_explicit and "month" in _salary_stated.lower():
        _sal_nums_monthly = [n for n in _sal_nums if 500 < n < 100_000]
        if _sal_nums_monthly:
            _monthly_upper = max(_sal_nums_monthly)
            _annual_equiv  = _monthly_upper * 12
            if _annual_equiv < _market_thr:
                r["salary_gate"] = "failed"
                r.setdefault("flags", []).append(
                    f"OVERRIDE: monthly {_monthly_upper:,}/mo × 12 = {_annual_equiv:,} "
                    f"< {_market_thr:,} threshold → salary_gate=failed")
                changed.append(f"monthly {_monthly_upper:,} × 12 = {_annual_equiv:,} < {_market_thr:,} → failed")
            elif _annual_equiv >= _market_thr and r.get("salary_gate") != "passed":
                r["salary_gate"] = "passed"
                r.setdefault("flags", []).append(
                    f"OVERRIDE: monthly {_monthly_upper:,}/mo × 12 = {_annual_equiv:,} "
                    f">= {_market_thr:,} threshold → salary_gate=passed")
                changed.append(f"monthly {_monthly_upper:,} × 12 = {_annual_equiv:,} >= {_market_thr:,} → passed")

    if r.get("salary_gate") == "failed" and _salary_is_explicit:
        if _sal_nums:
            _upper = max(_sal_nums)
            if _upper < 1000:
                _upper *= 1000   # K notation: 89 → 89000
            if _upper >= _market_thr:
                r["salary_gate"] = "passed"
                r.setdefault("flags", []).append(
                    f"OVERRIDE: salary upper bound {_upper:,} >= {_market_thr:,} threshold — gate passed (was failed)")
                changed.append(f"salary upper {_upper:,} >= {_market_thr:,} → gate passed")
    if (r.get("salary_gate") == "failed"
            and _salary_is_explicit
            and r.get("action") != "reject"):
        r["action"] = "reject"
        r.setdefault("flags", []).append("OVERRIDE: salary_gate=failed (stated) forces reject")
        changed.append("salary_gate=failed (stated) → action=reject")

    # Gate 3: Score < 60 must always be reject
    if r.get("fit_score", 100) < 60 and r.get("action") != "reject":
        r["action"] = "reject"
        r.setdefault("flags", []).append("OVERRIDE: fit_score<60 forces reject")
        changed.append(f"fit_score={r['fit_score']}<60 → action=reject")

    # Gate 4: Score ≥ 75 with no blockers must be shortlist (not review)
    if (r.get("fit_score", 0) >= 75
            and r.get("action") == "review"
            and r.get("visa_sponsorship_status") != "Rejected"
            and r.get("salary_gate") != "failed"
            and not r.get("is_contract")
            and not r.get("is_remote_only")):
        r["action"] = "shortlist"
        r.setdefault("flags", []).append("OVERRIDE: fit_score≥75 with no blockers → shortlist")
        changed.append(f"fit_score={r['fit_score']}≥75, no blockers → shortlist")

    # Gate 5: Correct action when Claude rejected for salary/visa that our overrides cleared.
    # If action is still "reject" but no hard blocker actually applies, upgrade to review/shortlist.
    if (r.get("action") == "reject"
            and r.get("visa_sponsorship_status") != "Rejected"
            and r.get("salary_gate") != "failed"
            and not r.get("is_contract")
            and not r.get("is_remote_only")
            and r.get("fit_score", 0) >= 60):
        new_action = "shortlist" if r.get("fit_score", 0) >= 75 else "review"
        r["action"] = new_action
        r.setdefault("flags", []).append(
            f"OVERRIDE: no hard blockers after gate fixes → upgraded to {new_action}")
        changed.append(f"no hard blockers remain → {new_action}")

    # Gate 6: Role focus filter — block purely BI, analytics-engineering, or consulting roles
    role_focus = r.get("role_focus", "")
    domain_pts = (r.get("fit_score_breakdown") or {}).get("domain", 0)
    fit        = r.get("fit_score", 0)

    if role_focus == "analytics_engineering":
        if r.get("action") != "reject":
            r["action"] = "reject"
            r.setdefault("flags", []).append("OVERRIDE: role_focus=analytics_engineering → reject")
            r["rejection_reason"] = (r.get("rejection_reason")
                or "Analytics engineering/data modelling role — not aligned with analytics leadership profile")
            changed.append("role_focus=analytics_engineering → reject")

    elif role_focus == "ai_engineering":
        if r.get("action") != "reject":
            r["action"] = "reject"
            r.setdefault("flags", []).append("OVERRIDE: role_focus=ai_engineering → reject")
            r["rejection_reason"] = (r.get("rejection_reason")
                or "AI/ML engineering/infrastructure role — not aligned with analytics leadership profile")
            changed.append("role_focus=ai_engineering → reject")

    elif role_focus == "credit_risk":
        if r.get("action") != "reject":
            r["action"] = "reject"
            r.setdefault("flags", []).append("OVERRIDE: role_focus=credit_risk → reject")
            r["rejection_reason"] = (r.get("rejection_reason")
                or "Credit/risk strategy role — primary output is risk modelling, not analytics leadership")
            changed.append("role_focus=credit_risk → reject")

    elif role_focus == "bi_reporting":
        if domain_pts < 20:
            if r.get("action") != "reject":
                r["action"] = "reject"
                r.setdefault("flags", []).append(
                    "OVERRIDE: role_focus=bi_reporting + domain<20 → reject")
                r["rejection_reason"] = (r.get("rejection_reason")
                    or "Purely BI/reporting role — primary output is dashboards, not analytics strategy")
                changed.append("role_focus=bi_reporting, domain<20 → reject")
        else:
            if r.get("action") == "shortlist":
                r["action"] = "review"
                r.setdefault("flags", []).append(
                    "OVERRIDE: role_focus=bi_reporting at product/tech company → shortlist→review")
                changed.append("role_focus=bi_reporting, domain≥20 → review (not shortlist)")

    elif role_focus == "management_consulting":
        if fit < 88:
            if r.get("action") != "reject":
                r["action"] = "reject"
                r.setdefault("flags", []).append(
                    f"OVERRIDE: role_focus=management_consulting + fit={fit}<88 → reject")
                r["rejection_reason"] = (r.get("rejection_reason")
                    or f"Consulting/advisory role — fit_score {fit} below the ≥88 threshold (CLAUDE.md)")
                changed.append(f"role_focus=management_consulting, fit={fit}<88 → reject")
        else:
            if r.get("action") == "shortlist":
                r["action"] = "review"
                r.setdefault("flags", []).append(
                    f"OVERRIDE: role_focus=management_consulting, fit={fit}≥88 → review (human judgment)")
                changed.append(f"role_focus=management_consulting, fit={fit}≥88 → review")

    elif role_focus == "insights_strategy":
        if r.get("action") != "reject":
            r["action"] = "reject"
            r.setdefault("flags", []).append("OVERRIDE: role_focus=insights_strategy → reject")
            r["rejection_reason"] = (r.get("rejection_reason")
                or "Insights/strategy role — primary output is qualitative insights or "
                   "operational management with no hands-on analytics tooling required")
            changed.append("role_focus=insights_strategy → reject")

    if changed:
        print(f"    [override] Hard gates applied: {'; '.join(changed)}")
    return r


# Bare "not stated" patterns — no estimate was provided by Claude
_SALARY_BARE_MISSING = {"", "not stated", "not provided", "not disclosed",
                        "not available", "not specified"}


def _patch_missing_salary_estimate(score_result: dict, job: dict, key: str) -> dict:
    """
    Safety net: if Claude returned bare 'Not stated' (without an estimate), make a
    focused Haiku call to fill the gap. Only fires when Claude ignores the Pass 2
    salary estimation instruction — when Claude follows it correctly this is a no-op.
    Supports all markets (UK/GBP, NL/EUR, SE/SEK).
    """
    market = job.get("market", "uk")

    stated = (score_result.get("salary_stated") or "").strip().lower()
    needs_patch = (
        stated in _SALARY_BARE_MISSING
        or (stated.startswith("not stated") and "est." not in stated)
        or (stated.startswith("not provided") and "est." not in stated)
    )
    if not needs_patch:
        return score_result

    role     = job.get("job_title") or job.get("role") or "Unknown Role"
    company  = job.get("company_name") or job.get("company") or ""
    location = job.get("location") or "United Kingdom"

    _market_cfg = {
        "uk": {
            "label":         "UK",
            "currency":      "GBP",
            "example":       '{"salary_range": "£85,000–£110,000", "confidence": "medium"}',
            "currency_char": "£",
            "premium":       "London adds ~10-15% premium",
        },
        "nl": {
            "label":         "Netherlands",
            "currency":      "EUR",
            "example":       '{"salary_range": "€90,000–€110,000", "confidence": "medium"}',
            "currency_char": "€",
            "premium":       "Amsterdam adds ~10-15% premium",
        },
        "se": {
            "label":         "Sweden",
            "currency":      "SEK",
            "example":       '{"salary_range": "SEK 850,000–SEK 1,050,000", "confidence": "medium"}',
            "currency_char": "SEK",
            "premium":       "Stockholm adds ~10-15% premium",
        },
    }
    cfg = _market_cfg.get(market, _market_cfg["uk"])

    prompt = (
        f"Role: {role}\nCompany: {company}\nLocation: {location}\n\n"
        f"Estimate the annual {cfg['currency']} salary range for this role in the "
        f"{cfg['label']} analytics market (2025-2026). "
        f"Base your estimate on role title, seniority, company type, and location "
        f"({cfg['premium']}). "
        f"Return ONLY valid JSON with no extra text: {cfg['example']}"
    )
    try:
        payload = json.dumps({
            "model":      "claude-haiku-4-5-20251001",
            "max_tokens": 80,
            "messages":   [{"role": "user", "content": prompt}],
            "system":     f"You are a {cfg['label']} salary benchmarking assistant. Return only valid JSON.",
        }).encode()
        req = _ureq.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={"Content-Type": "application/json",
                     "anthropic-version": "2023-06-01",
                     **({"x-api-key": key} if key else {})},
            method="POST",
        )
        with _ureq.urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read())["content"][0]["text"].strip()
        raw = re.sub(r"```json|```", "", raw).strip()
        parsed = json.loads(raw)
        rng  = parsed.get("salary_range", "")
        conf = parsed.get("confidence", "low")
        if rng and cfg["currency_char"] in rng:
            score_result["salary_stated"]              = f"Not stated (est. {rng})"
            score_result["salary_estimate"]            = rng
            score_result["salary_estimate_confidence"] = conf
            score_result.setdefault("salary_gate", "tbc")
            print(f"    [salary_patch] Estimated: {rng} (conf={conf})")
        else:
            print(f"    [salary_patch] Unexpected format '{rng}' — keeping bare Not stated")
            score_result.setdefault("salary_gate", "tbc")
    except Exception as e:
        print(f"    [salary_patch] Failed ({e}) — keeping bare Not stated, gate=tbc")
        score_result.setdefault("salary_gate", "tbc")
    return score_result


def _canonical_rejection_reason(r: dict) -> str:
    """
    Return the single most-significant rejection reason in confirmed hierarchy order.
    Called after all _apply_hard_overrides() gates so all flags are already set.
    Hierarchy: Remote > Contract > Visa Denied > Salary Failed > Score/Fit (Pass 2 semantic)
    Always includes numeric fit score when the root cause is score-based.
    """
    market = r.get("market", "uk")
    if r.get("is_remote_only"):
        _msgs = {
            "uk": "Remote-only role — physical UK presence required for visa relocation",
            "nl": "Remote-only role — in-person Netherlands presence required for kennismigrant permit",
            "se": "Remote-only role — in-person Sweden presence required for arbetstillstånd permit",
        }
        return _msgs.get(market, _msgs["uk"])
    if r.get("is_contract"):
        _visas = {"uk": "Skilled Worker Visa", "nl": "kennismigrant permit", "se": "arbetstillstånd"}
        visa = _visas.get(market, "work visa")
        return f"Contract role — {visa} requires permanent employment"
    if r.get("visa_sponsorship_status") == "Rejected":
        return "Visa sponsorship explicitly denied in job description"
    if (r.get("salary_gate") == "failed"
            and r.get("salary_stated") not in ("", "Not stated", "Not provided", None)):
        _thresholds = {"uk": "£85k", "nl": "€100k", "se": "SEK 1M"}
        thr = _thresholds.get(market, "£85k")
        return f"Salary below {thr} threshold (stated: {r.get('salary_stated', '')})"
    # Score-based: always surface both the number and Claude's semantic reason.
    claude_reason = r.get("rejection_reason") or "Domain/fit mismatch"
    return f"Fit score {r.get('fit_score', 0)}/100 — {claude_reason}"


# ─────────────────────────────────────────────────────────────────────────────
# MAIN SCORING LOOP
# ─────────────────────────────────────────────────────────────────────────────

env = load_env()
api_key = env.get("ANTHROPIC_API_KEY", "")
if not api_key and not DRY_RUN:
    print("ERROR: ANTHROPIC_API_KEY not found in .env")
    sys.exit(1)

# ── Retroactive stale sweep (runs before scoring new jobs) ───────────────────
_stale_swept = recategorize_stale_entries()
if _stale_swept:
    print(f"[score_jobs] Retroactive stale sweep: {_stale_swept} entries updated to Stale\n")

# Counters
pass1_rejected   = []
pass1_stale_jobs = []
pass2_scored     = []
skipped_dupes    = []
apify_upgrades   = []   # Apify-over-Adzuna data patches emitted to scored_jobs.json

p1_rejects   = 0
p1_stale     = 0
p1_passes    = 0
p2_shortlist = 0
p2_review    = 0
p2_reject    = 0
p2_error     = 0

seen_urls_this_run    = set()
seen_co_role_this_run = set()

for i, job in enumerate(enriched):
    title   = job.get("job_title") or "Unknown"
    company = job.get("company_name") or "Unknown"
    jd_url  = (job.get("job_url") or job.get("url") or "").strip()

    # ── Intra-batch dedup ─────────────────────────────────────────────────────
    co_role_key = f"{company.lower()}::{title.lower()}"
    if jd_url and jd_url in seen_urls_this_run:
        print(f"  — SKIP (intra-batch dupe) {company} / {title}")
        skipped_dupes.append((job, {"id": "intra-batch", "status": "duplicate"}))
        continue
    if co_role_key in seen_co_role_this_run:
        print(f"  — SKIP (intra-batch dupe) {company} / {title}")
        skipped_dupes.append((job, {"id": "intra-batch", "status": "duplicate"}))
        continue

    # ── Cross-run match check ─────────────────────────────────────────────────
    match_result   = _find_match(job)
    match_decision = match_result["decision"]
    matched_entry  = match_result["matched_entry"]
    matched_id     = match_result["matched_id"]

    # Attach match metadata to job dict — carried through to write_tracker.py
    job["_match_decision"] = match_decision
    job["_matched_id"]     = matched_id if match_decision in ("new_entry", "update_in_place") else None
    job["_match_exists"]   = matched_entry is not None
    job["_score_reused"]   = False

    # Apify-over-Adzuna upgrade: when Apify finds a job that matches an existing
    # Shortlisted/Review Needed entry, queue a data patch so write_tracker.py
    # can upgrade that entry's fields (jd_url → LinkedIn URL, career_page_url
    # from applyUrl, salary, etc.). Applies to both dedup and update_in_place.
    _queued_upgrade = False
    if (job.get("_source") == "apify"
            and matched_entry
            and matched_entry.get("status") in ("Shortlisted", "Review Needed")):
        apify_upgrades.append({
            "_matched_id":          matched_entry["id"],
            "_company":             company,
            "_role":                title,
            "jd_url":               jd_url,
            "salary_stated":        job.get("salary_stated") or "",
            "work_mode":            job.get("work_mode") or "",
            "experience_req":       (job.get("experience_years") or {}).get("display") or "",
            "ats_type":             job.get("ats_type") or "",
            "career_page_url_hint": job.get("career_page_url") or "",
        })
        _queued_upgrade = True

    if match_decision == "dedup":
        upgrade_note = " → Apify upgrade queued" if _queued_upgrade else ""
        print(f"  — SKIP (dedup/{matched_id}) {company} / {title}{upgrade_note}")
        skipped_dupes.append((job, matched_entry or {"id": "?", "status": "dedup"}))
        if jd_url: seen_urls_this_run.add(jd_url)
        seen_co_role_this_run.add(co_role_key)
        continue

    if jd_url: seen_urls_this_run.add(jd_url)
    seen_co_role_this_run.add(co_role_key)

    # ── Pass 1 ────────────────────────────────────────────────────────────────
    if not NO_PREFILTER:
        p1_result, reason = pass1_filter(job)
        if p1_result == "reject":
            p1_rejects += 1
            print(f"  ✗ P1-REJECT  {company} / {title} — {reason}")
            pass1_rejected.append((job, reason))
            continue
        elif p1_result == "stale":
            p1_stale += 1
            print(f"  ~ P1-STALE   {company} / {title} — {reason}")
            pass1_stale_jobs.append(job)
            continue

    p1_passes += 1

    # ── Re-scoring eligibility ────────────────────────────────────────────────
    if matched_entry and matched_entry.get("score_exists"):
        last_scored = matched_entry.get("latest_scoring_date")
        if last_scored:
            try:
                days_since = (date.fromisoformat(TODAY) - date.fromisoformat(last_scored)).days
                if days_since <= REPOST_GAP_DAYS:
                    fit_sc = matched_entry.get("fit_score")
                    print(f"  ↩ SCORE REUSED [{fit_sc}] {company} / {title} "
                          f"(scored {days_since}d ago, ≤{REPOST_GAP_DAYS}d threshold)")
                    full_entry = next((e for e in tracker if e.get("id") == matched_id), None)
                    if full_entry:
                        reused_result = _reconstruct_result_from_entry(full_entry)
                        job["_score_reused"] = True
                        pass2_scored.append((job, reused_result))
                        continue
            except ValueError:
                pass

    # ── Pass 2 — Claude API ───────────────────────────────────────────────────
    if DRY_RUN:
        print(f"  [dry-run] Would score: {company} / {title}")
        continue

    user_prompt = _build_user_prompt(job)
    try:
        raw = call_claude_api(_build_system_prompt(job.get("market", "uk")), user_prompt, MODEL, api_key)
        result = parse_score_response(raw)
        if not result:
            print(f"  ⚠ PARSE ERROR {company} / {title} — raw: {raw[:120]}")
            p2_error += 1
            continue

        # ── Post-score hard overrides ─────────────────────────────────────────
        # Critical gates enforced in code regardless of what Claude returned.
        result = _apply_hard_overrides(result)
        # Enforce apply_recommendation consistency: reject → always Skip
        if result.get("action") == "reject" and result.get("apply_recommendation") != "Skip":
            result["apply_recommendation"] = "Skip"
        # Salary patch: if Claude skipped the estimation instruction, fill it now.
        if not DRY_RUN:
            result = _patch_missing_salary_estimate(result, job, api_key)
        # Contract/remote: set action=reject if not already set.
        if result.get("is_contract") and result.get("action") != "reject":
            result["action"] = "reject"
        if result.get("is_remote_only") and result.get("action") != "reject":
            result["action"] = "reject"
        # Rewrite rejection_reason to the highest-hierarchy cause.
        # Runs after ALL override gates so every flag is already set.
        # Ensures the displayed reason always reflects the most significant signal,
        # not whichever gate happened to write rejection_reason first.
        if result.get("action") == "reject":
            result["rejection_reason"] = _canonical_rejection_reason(result)

        action = result.get("action", "reject")
        score  = result.get("fit_score", 0)
        visa   = result.get("visa_sponsorship_status", "Unconfirmed")
        sal_g  = result.get("salary_gate", "tbc")

        if action == "shortlist":
            p2_shortlist += 1
            tag = "✓ SHORTLIST"
        elif action == "review":
            p2_review += 1
            tag = "⚠ REVIEW   "
        else:
            p2_reject += 1
            tag = "✗ P2-REJECT"

        reason_str = f" — {result.get('rejection_reason')}" if result.get("rejection_reason") else ""
        agency_str = f" [via {company}→{result.get('actual_hiring_company')}]" if result.get("is_agency_post") and result.get("actual_hiring_company") else ""
        print(f"  {tag} [{score}] {company} / {title}{agency_str}{reason_str}")
        if result.get("flags"):
            print(f"            flags: {', '.join(result['flags'])}")

        pass2_scored.append((job, result))
        time.sleep(0.3)

    except _uerr.HTTPError as e:
        body = e.read().decode()[:200]
        print(f"  ⚠ API ERROR  {company} / {title} — HTTP {e.code}: {body}")
        p2_error += 1
    except Exception as e:
        print(f"  ⚠ ERROR      {company} / {title} — {e}")
        p2_error += 1


# ─────────────────────────────────────────────────────────────────────────────
# WRITE OUTPUTS
# ─────────────────────────────────────────────────────────────────────────────

def _build_rejection_entry(job: dict, reason: str, score_result: Optional[dict], source: str) -> dict:
    has_score = score_result is not None and score_result.get("fit_score") is not None
    return {
        "id":                   f"rej_{len(auto_rejected) + 1:04d}",
        "job_id":               job.get("jobId") or job.get("job_id") or "",
        "company":              job.get("company_name") or "",
        "role":                 job.get("job_title") or "",
        "location":             job.get("location") or "",
        "jd_url":               job.get("job_url") or job.get("url") or "",
        "posted_date":          job.get("posted_date") or job.get("postedDate") or "",
        "fit_score":            score_result.get("fit_score") if score_result else None,
        "rejection_reason":     reason,
        "salary_stated":        score_result.get("salary_stated") if score_result else (job.get("salary") or ""),
        "visa_hint":            "n/a",
        "scout_run_date":       TODAY,
        "source":               source,
        "score_exists":         has_score,
        "latest_scoring_date":  TODAY if has_score else None,
        "role_focus":           score_result.get("role_focus", "") if score_result else "",
        "market":               job.get("market", "uk"),
    }


shortlisted_for_tracker = []
newly_rejected_entries  = []

for job, result in pass2_scored:
    action = result.get("action", "reject")

    # Heuristic fallback: augment is_agency_post for known agencies Claude may have missed.
    # Uses word-boundary match to avoid false positives (e.g. "saltus" != "salt").
    if not result.get("is_agency_post"):
        co_lower = (job.get("company_name") or "").lower().strip()
        if any(re.search(r'\b' + re.escape(ag) + r'\b', co_lower) for ag in KNOWN_AGENCIES):
            result["is_agency_post"] = True
            if not result.get("actual_hiring_company"):
                hint = _extract_hiring_company_hint(job)
                if hint:
                    result["actual_hiring_company"] = hint

    if action in ("shortlist", "review"):
        # Resolve ATS type: prefer Claude's semantic identification over heuristic URL match
        ats_from_claude = result.get("ats_type_from_jd") or "unknown"
        ats_final = ats_from_claude if ats_from_claude != "unknown" else (job.get("ats_type") or "unknown")

        # Resolve company: use actual_hiring_company if agency post
        display_company = (
            result.get("actual_hiring_company") or job.get("company_name") or ""
        ) if result.get("is_agency_post") else (job.get("company_name") or "")

        entry = {
            "job":    job,
            "score":  result,
            "status": "Shortlisted" if action == "shortlist" else "Review Needed",
            # Resolved fields for tracker writer convenience
            "_resolved_company":   display_company,
            "_resolved_ats_type":  ats_final,
            # Match metadata (for write_tracker.py routing)
            "_match_decision":     job.get("_match_decision", "no_match"),
            "_matched_id":         job.get("_matched_id"),
            "_match_exists":       job.get("_match_exists", False),
            "_score_reused":       job.get("_score_reused", False),
        }
        shortlisted_for_tracker.append(entry)
    else:
        reason = result.get("rejection_reason") or f"fit_score={result.get('fit_score',0)}"
        newly_rejected_entries.append(
            _build_rejection_entry(job, reason, result, "pass2")
        )

for job, reason in pass1_rejected:
    newly_rejected_entries.append(
        _build_rejection_entry(job, reason, None, "pass1")
    )

# Stale jobs: tracked in scored_jobs.json with status=Stale, no Claude score.
# They appear in the Google Sheet (grey row) so the user can see them but they
# don't clutter Shortlisted. A fresh repost of the same role bypasses dedup and
# gets scored normally.
for job in pass1_stale_jobs:
    shortlisted_for_tracker.append({
        "job":               job,
        "score":             None,
        "status":            "Stale",
        "_resolved_company":  job.get("company_name", ""),
        "_resolved_ats_type": job.get("ats_type") or "unknown",
        "_match_decision":    job.get("_match_decision", "no_match"),
        "_matched_id":        job.get("_matched_id"),
        "_match_exists":      job.get("_match_exists", False),
        "_score_reused":      False,
    })

if not DRY_RUN:
    scored_output = {
        "jobs":           shortlisted_for_tracker,
        "apify_upgrades": apify_upgrades,
        "_run_stats": {
            "input_jobs":   len(enriched),
            "duplicates":   len(skipped_dupes),
            "p1_rejected":  p1_rejects,
            "p1_stale":     p1_stale,
            "p1_passed":    p1_passes,
            "p2_shortlist": p2_shortlist,
            "p2_review":    p2_review,
            "p2_reject":    p2_reject,
            "p2_error":     p2_error,
            "run_date":     TODAY,
        },
    }
    SCORED_PATH.write_text(json.dumps(scored_output, indent=2, ensure_ascii=False))
    print(f"\n[score_jobs] Wrote {len(shortlisted_for_tracker)} jobs + "
          f"{len(apify_upgrades)} Apify upgrades → {SCORED_PATH.name}")

    existing_urls = {e.get("jd_url") for e in auto_rejected if e.get("jd_url")}
    fresh = [e for e in newly_rejected_entries if e.get("jd_url") not in existing_urls]
    auto_rejected.extend(fresh)
    for idx, entry in enumerate(auto_rejected, 1):
        entry["id"] = f"rej_{idx:04d}"
    AUTO_REJ_PATH.write_text(json.dumps({"auto_rejected": auto_rejected}, indent=2, ensure_ascii=False))
    print(f"[score_jobs] Appended {len(fresh)} new rejects → {AUTO_REJ_PATH.name}")

# ── Summary ───────────────────────────────────────────────────────────────────
total_api_calls = p1_passes if not DRY_RUN else 0
# Haiku: ~$0.25/1M input (~15000 tokens: 11500 SCORE_SYSTEM + 3500 user) + $1.25/1M output (~1000 tokens)
est_cost = total_api_calls * ((15000 * 0.25 + 1000 * 1.25) / 1_000_000)

haiku_cost_usd = (
    _api_usage["input_tokens"] * 0.80 + _api_usage["output_tokens"] * 4.00
) / 1_000_000

print(f"""
[score_jobs] ══════════════════════════════════════════
  Input jobs:      {len(enriched)}
  Duplicates:      {len(skipped_dupes)} skipped
  Pass 1 rejected: {p1_rejects}  (native field gates — hard cutoff)
  Pass 1 stale:    {p1_stale}  (>{STALE_AGE_DAYS}d old → tracked as Stale)
  Pass 1 passed:   {p1_passes} → sent to Claude API
  ─────────────────────────────────────────
  Shortlisted:     {p2_shortlist}
  For review:      {p2_review}
  API rejected:    {p2_reject}
  API errors:      {p2_error}
  ─────────────────────────────────────────
  API tokens:      {_api_usage["input_tokens"]:,} in / {_api_usage["output_tokens"]:,} out  ({_api_usage["calls"]} calls)
  Actual cost:     ${haiku_cost_usd:.4f}
  Est. API cost:   ~${est_cost:.4f}
[score_jobs] ══════════════════════════════════════════

Next step: python3 scripts/sheets_sync.py push
""")

# Write actual token counts to monitoring (only meaningful for real runs with API calls)
if not DRY_RUN and _api_usage["calls"] > 0:
    try:
        monitor_dir = ROOT / "data" / "monitoring"
        monitor_dir.mkdir(parents=True, exist_ok=True)
        scoring_path = monitor_dir / "scoring_run.json"
        records = json.loads(scoring_path.read_text()) if scoring_path.exists() else []
        records.append({
            "date":           TODAY,
            "timestamp":      datetime.now().isoformat(timespec="seconds"),
            "jobs_p2":        _api_usage["calls"],
            "input_tokens":   _api_usage["input_tokens"],
            "output_tokens":  _api_usage["output_tokens"],
            "cost_usd":       round(haiku_cost_usd, 6),
            "model":          MODEL,
        })
        scoring_path.write_text(json.dumps(records, indent=2))
        print(f"[score_jobs] Monitoring → {scoring_path.name} (actual tokens logged)")
    except Exception as e:
        print(f"[score_jobs] Warning: could not write monitoring data: {e}")
