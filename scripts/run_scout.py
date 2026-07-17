#!/usr/bin/env python3
"""
run_scout.py — Job Scout Runner
================================
Claude Code calls this script directly. Real CLI arguments — no ambiguity.

Usage:
  python3 scripts/run_scout.py                         # default: Apify only (UK filtered, 24h)
  python3 scripts/run_scout.py --dry-run               # print search list + cost estimate, no API calls
  python3 scripts/run_scout.py --max-jobs 50           # cap Apify per-URL results (default: 100)
  python3 scripts/run_scout.py --yes                   # skip confirmation prompt (for scripted runs)
  python3 scripts/run_scout.py --source apify          # Apify only (4 LinkedIn URLs, 24h) — same as default
  python3 scripts/run_scout.py --source adzuna         # Adzuna only (UK-wide, 12 searches, age=4)
  python3 scripts/run_scout.py --source auto           # both Apify + Adzuna
  python3 scripts/run_scout.py --source adzuna --age 7 # Adzuna with custom age
  python3 scripts/run_scout.py --market all --intl-age 7  # one-time: NL/SE Apify covers 7 days instead of 24h

Source behaviour:
  apify (default) — 4 pre-filtered LinkedIn URLs, past 24h, UK-wide (high quality, targeted)
  auto — runs BOTH sources every time:
    • Apify:  4 pre-filtered LinkedIn URLs, past 24h, UK-wide (LinkedIn data quality)
    • Adzuna: 12 broad UK-wide keyword searches, past 4 days (free, maximises breadth)
    • Merge:  Apify results first; Adzuna fills in gaps (Apify wins by ordering)
    • --age is ignored for Apify in auto mode (f_TPR=r86400 baked into URL)
  apify  — Apify only, SEARCHES_APIFY (3 LinkedIn URLs), default 100 jobs/URL
  adzuna — Adzuna only, SEARCHES_ADZUNA (12 UK-wide searches), default --age 4

Actor: curious_coder/linkedin-jobs-scraper ($1.00 / 1,000 results)
  4 single-keyword URLs with filter params (f_JT=F, f_E=4,5, f_WT=3,1, f_TPR=r86400).
  Single-keyword-per-URL is optimal — actor scrapes without browser session so LinkedIn's
  AI search is not engaged. Filter params still honoured at URL level despite UI removal.

Cost:
  auto:   Apify ~$0.10–$0.40/run (100 cap × 4 URLs × $0.001/job) + Adzuna $0.00
  apify:  ~$0.10–$0.40/run (100 cap × 4 URLs = 400 max slots × $0.001/job)
  adzuna: free (12 API calls = 4.8% of 250 req/day, 3.6% of 10k/month)
  Cache:  24h TTL — re-running same day costs $0 for both sources

Adzuna limit note:
  250/day and 10,000/month = API request CALLS, not result row counts.
  Each keyword = 1 call regardless of max_jobs (result count per call).
  12 keywords = 12 calls; result rows are unlimited and not metered.

Adzuna setup (free, one-time):
  1. Register at https://developer.adzuna.com/
  2. Add to .env:  ADZUNA_APP_ID=your_id
                   ADZUNA_APP_KEY=your_key
"""

import sys, json, os, subprocess, time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

LOCK_FILE = ROOT / "data" / "pipeline" / ".scout_lock"

# ── Search configurations ─────────────────────────────────────────────────────

# Apify: 4 pre-filtered LinkedIn URLs, UK-wide, past 24h.
# Single-keyword-per-URL format — optimal for Apify actor (scrapes without browser session;
# LinkedIn's AI search semantic layer requires cookies and is not engaged by the actor).
# Single precise keywords return clean, targeted results; boolean OR and space-separated
# multi-title formats tested and rejected (too loose / AND condition returns 0).
# Filter params f_JT, f_E, f_WT still honoured at URL level despite LinkedIn UI removal.
# 4th URL (AI Analytics) is exploratory — evaluate result quality after first run.
# 3-tuples: (label, linkedin_search_url, per_url_max_jobs)
SEARCHES_APIFY = [
    ("Lead Data Analyst",
     "https://www.linkedin.com/jobs/search?keywords=Lead%20Data%20Analyst&location=United%20Kingdom&geoId=101165590&f_TPR=r86400&f_JT=F&f_E=4%2C5&f_WT=3%2C1&position=1&pageNum=0",
     100),
    ("Lead Business Analyst",
     "https://www.linkedin.com/jobs/search?keywords=Lead%20Business%20Analyst&location=United%20Kingdom&geoId=101165590&f_TPR=r86400&f_JT=F&f_E=4%2C5&f_WT=3%2C1&position=1&pageNum=0",
     100),
    ("Analytics Manager",
     "https://www.linkedin.com/jobs/search?keywords=Analytics%20Manager&location=United%20Kingdom&geoId=101165590&f_TPR=r86400&f_JT=F&f_E=4%2C5&f_WT=1%2C3&position=1&pageNum=0",
     100),
    ("AI Analytics",
     "https://www.linkedin.com/jobs/search?keywords=AI%20Analytics&location=United%20Kingdom&geoId=101165590&f_TPR=r86400&f_JT=F&f_E=4%2C5&f_WT=3%2C1&position=1&pageNum=0",
     100),
]

# AI search format (tested 2026-07, does not work with actor — requires browser session/cookies):
# SEARCHES_APIFY_AI_SEARCH = [
#     ("Analytics Manager",  natural language keywords, 100),
#     ("Lead Analyst",       natural language keywords, 100),
#     ("Insights Manager",   natural language keywords, 100),
# ]
DEFAULT_APIFY_MAX_JOBS = 100  # fallback when using 2-tuple searches; per-URL caps above take precedence

# Adzuna: 12 broad UK-wide searches, fixed 4-day window in auto mode.
# Per-search max_jobs scaled by expected UK signal density (Adzuna API cap = 50/call).
# Adzuna limit: 250 req/day, 10k/month — each tuple = 1 API call regardless of max_jobs.
SEARCHES_ADZUNA = [
    # Tier A — high volume, clean signal (45 each = 180 total slots)
    ("Analytics Manager",             "United Kingdom", 45),
    ("Lead Business Analyst",         "United Kingdom", 45),
    ("Lead Data Analyst",             "United Kingdom", 45),
    ("Analytics Lead",                "United Kingdom", 45),
    # Tier B — specific, established titles (35 each = 140 total slots)
    ("Product Analytics Manager",     "United Kingdom", 35),
    ("Lead Product Analyst",          "United Kingdom", 35),
    ("Growth Analytics Lead",         "United Kingdom", 35),
    ("Senior Analytics Lead",         "United Kingdom", 35),
    # Tier C — medium volume or mixed-signal (25 each = 50 total slots)
    ("Insights Manager",              "United Kingdom", 25),
    ("AI Analytics Manager",          "United Kingdom", 25),
    # Tier D — niche / newer titles, lower UK posting density (20 each = 40 total slots)
    ("AI Analytics Lead",             "United Kingdom", 20),
    ("Analytics Transformation Lead", "United Kingdom", 20),
]
# Total Adzuna max result slots: 435 across 12 API calls → ~200–280 unique after dedup

# ── NL — Netherlands, country-level, all 4 keywords matching UK format ────────
# geoId 102890719 = Netherlands (country-level — same approach as UK geoId 101165590)
# Cost: $0.001/job × 100 jobs × 4 URLs = $0.40 max (same cap as UK)
# Scoring gate: NL_TIER1 in score_jobs.py filters to Randstad cities
SEARCHES_APIFY_NL = [
    ("NL Lead Data Analyst",
     "https://www.linkedin.com/jobs/search?keywords=Lead%20Data%20Analyst&location=Netherlands&geoId=102890719&f_TPR=r86400&f_JT=F&f_E=4%2C5&f_WT=3%2C1&position=1&pageNum=0",
     100),
    ("NL Lead Business Analyst",
     "https://www.linkedin.com/jobs/search?keywords=Lead%20Business%20Analyst&location=Netherlands&geoId=102890719&f_TPR=r86400&f_JT=F&f_E=4%2C5&f_WT=3%2C1&position=1&pageNum=0",
     100),
    ("NL Analytics Manager",
     "https://www.linkedin.com/jobs/search?keywords=Analytics%20Manager&location=Netherlands&geoId=102890719&f_TPR=r86400&f_JT=F&f_E=4%2C5&f_WT=1%2C3&position=1&pageNum=0",
     100),
    ("NL AI Analytics",
     "https://www.linkedin.com/jobs/search?keywords=AI%20Analytics&location=Netherlands&geoId=102890719&f_TPR=r86400&f_JT=F&f_E=4%2C5&f_WT=3%2C1&position=1&pageNum=0",
     100),
]
SEARCHES_ADZUNA_NL = [
    ("Analytics Manager",             "Netherlands", 45),
    ("Lead Business Analyst",         "Netherlands", 45),
    ("Lead Data Analyst",             "Netherlands", 45),
    ("Analytics Lead",                "Netherlands", 45),
    ("Product Analytics Manager",     "Netherlands", 35),
    ("Lead Product Analyst",          "Netherlands", 35),
    ("Growth Analytics Lead",         "Netherlands", 35),
    ("Senior Analytics Lead",         "Netherlands", 35),
    ("Insights Manager",              "Netherlands", 25),
    ("AI Analytics Manager",          "Netherlands", 25),
    ("AI Analytics Lead",             "Netherlands", 20),
    ("Analytics Transformation Lead", "Netherlands", 20),
]

# ── SE — Sweden, country-level, all 4 keywords matching UK format ──────────────
# geoId 105117694 = Sweden (country-level)
# Cost: $0.001/job × 100 jobs × 4 URLs = $0.40 max
# Scoring gate: SE_TIER1 in score_jobs.py filters to Stockholm / Gothenburg
SEARCHES_APIFY_SE = [
    ("SE Lead Data Analyst",
     "https://www.linkedin.com/jobs/search?keywords=Lead%20Data%20Analyst&location=Sweden&geoId=105117694&f_TPR=r86400&f_JT=F&f_E=4%2C5&f_WT=3%2C1&position=1&pageNum=0",
     100),
    ("SE Lead Business Analyst",
     "https://www.linkedin.com/jobs/search?keywords=Lead%20Business%20Analyst&location=Sweden&geoId=105117694&f_TPR=r86400&f_JT=F&f_E=4%2C5&f_WT=3%2C1&position=1&pageNum=0",
     100),
    ("SE Analytics Manager",
     "https://www.linkedin.com/jobs/search?keywords=Analytics%20Manager&location=Sweden&geoId=105117694&f_TPR=r86400&f_JT=F&f_E=4%2C5&f_WT=1%2C3&position=1&pageNum=0",
     100),
    ("SE AI Analytics",
     "https://www.linkedin.com/jobs/search?keywords=AI%20Analytics&location=Sweden&geoId=105117694&f_TPR=r86400&f_JT=F&f_E=4%2C5&f_WT=3%2C1&position=1&pageNum=0",
     100),
]
SEARCHES_ADZUNA_SE = [
    ("Analytics Manager",             "Sweden", 45),
    ("Lead Business Analyst",         "Sweden", 45),
    ("Lead Data Analyst",             "Sweden", 45),
    ("Analytics Lead",                "Sweden", 45),
    ("Product Analytics Manager",     "Sweden", 35),
    ("Lead Product Analyst",          "Sweden", 35),
    ("Growth Analytics Lead",         "Sweden", 35),
    ("Senior Analytics Lead",         "Sweden", 35),
    ("Insights Manager",              "Sweden", 25),
    ("AI Analytics Manager",          "Sweden", 25),
    ("AI Analytics Lead",             "Sweden", 20),
    ("Analytics Transformation Lead", "Sweden", 20),
]

ADZUNA_AGE_DAYS = 4  # fixed in auto mode — exact 4-day window via Adzuna API
_ADZUNA_CC = {"uk": "gb", "nl": "nl", "se": "se"}  # market → Adzuna country code

# ── Parse arguments ───────────────────────────────────────────────────────────
args              = sys.argv[1:]
yes               = "--yes" in args
dry_run           = "--dry-run" in args
source            = "apify"
max_jobs          = DEFAULT_APIFY_MAX_JOBS  # per URL; overridden by --max-jobs N
max_jobs_explicit = False
age               = None     # None = use source default; only applies in single-source mode

if "--age" in args:
    idx = args.index("--age")
    try:
        age = int(args[idx + 1])
    except (IndexError, ValueError):
        print("ERROR: --age requires a number, e.g. --age 1")
        sys.exit(1)

intl_age_days = None   # when set, overrides NL/SE Apify f_TPR for this run only
if "--intl-age" in args:
    idx = args.index("--intl-age")
    try:
        intl_age_days = int(args[idx + 1])
    except (IndexError, ValueError):
        print("ERROR: --intl-age requires a number of days, e.g. --intl-age 7")
        sys.exit(1)

if "--max-jobs" in args:
    idx = args.index("--max-jobs")
    try:
        max_jobs = int(args[idx + 1])
        max_jobs_explicit = True
        if max_jobs < 1 or max_jobs > 300:
            print("ERROR: --max-jobs must be between 1 and 300")
            sys.exit(1)
    except (IndexError, ValueError):
        print("ERROR: --max-jobs requires a number, e.g. --max-jobs 15")
        sys.exit(1)

if "--source" in args:
    idx = args.index("--source")
    try:
        source = args[idx + 1]
        if source not in ("apify", "adzuna", "auto"):
            print(f"ERROR: --source must be apify, adzuna, or auto (got '{source}')")
            sys.exit(1)
    except IndexError:
        print("ERROR: --source requires a value: apify, adzuna, or auto")
        sys.exit(1)

market = "uk"  # default: UK-only, fully backward-compatible
if "--market" in args:
    idx = args.index("--market")
    try:
        market = args[idx + 1]
        if market not in ("uk", "nl", "se", "all"):
            print(f"ERROR: --market must be uk, nl, se, or all (got '{market}')")
            sys.exit(1)
    except IndexError:
        print("ERROR: --market requires a value: uk, nl, se, or all")
        sys.exit(1)

# Effective ages for Adzuna (Apify age is baked into the URL)
adzuna_age = age if age is not None else ADZUNA_AGE_DAYS


# ── --intl-age: patch NL/SE Apify URLs to use a longer time window ───────────
# Only active when --intl-age N is passed (e.g. first NL/SE run covering 7 days).
# Default NL/SE URLs use f_TPR=r86400 (24h) — same as UK.
import re as _re_tpr
if intl_age_days:
    _intl_tpr = f"f_TPR=r{intl_age_days * 86400}"
    def _patch_tpr(searches):
        return [(l, _re_tpr.sub(r'f_TPR=r\d+', _intl_tpr, u), m) for l, u, m in searches]
    SEARCHES_APIFY_NL = _patch_tpr(SEARCHES_APIFY_NL)
    SEARCHES_APIFY_SE = _patch_tpr(SEARCHES_APIFY_SE)


def _tagged(searches, mkt):
    """Add market tag as 4th element to 3-tuple search entries."""
    return [(t[0], t[1], t[2], mkt) for t in searches]


def _build_apify_searches(mkt):
    out = []
    if mkt in ("uk", "all"): out += _tagged(SEARCHES_APIFY, "uk")
    if mkt in ("nl", "all"): out += _tagged(SEARCHES_APIFY_NL, "nl")
    if mkt in ("se", "all"): out += _tagged(SEARCHES_APIFY_SE, "se")
    return out


def _build_adzuna_searches(mkt):
    out = []
    if mkt in ("uk", "all"): out += _tagged(SEARCHES_ADZUNA, "uk")
    if mkt in ("nl", "all"): out += _tagged(SEARCHES_ADZUNA_NL, "nl")
    if mkt in ("se", "all"): out += _tagged(SEARCHES_ADZUNA_SE, "se")
    return out


apify_searches_tagged  = _build_apify_searches(market)
adzuna_searches_tagged = _build_adzuna_searches(market)
# 3-tuples for display/slot counting helpers (strip market tag)
apify_searches  = [(t[0], t[1], t[2]) for t in apify_searches_tagged]

# Adzuna: apply --max-jobs override only when explicitly set
def _override_max_jobs(searches, mj):
    return [(kw, loc, mj) for kw, loc, _ in searches]

if max_jobs_explicit and source == "adzuna":
    adzuna_searches = _override_max_jobs(
        [(t[0], t[1], t[2]) for t in adzuna_searches_tagged], max_jobs)
    # Rebuild tagged list to keep market info
    adzuna_searches_tagged = [(kw, loc, mj, t[3])
                               for (kw, loc, mj), t in zip(adzuna_searches, adzuna_searches_tagged)]
else:
    adzuna_searches = [(t[0], t[1], t[2]) for t in adzuna_searches_tagged]

apify_max_jobs  = max_jobs  # fallback for 2-tuples or explicit --max-jobs override

# ── Age label helper (Adzuna only) ───────────────────────────────────────────
def _age_label_adzuna(a):
    return f"past {a} day(s) exactly (Adzuna precise)"

# ── Display header ────────────────────────────────────────────────────────────
adzuna_slots  = sum(t[2] for t in adzuna_searches)
# Per-URL Apify caps from 3-tuples; fallback to global max_jobs if 2-tuples used
apify_slots   = sum(t[2] if len(t) == 3 else apify_max_jobs for t in apify_searches)
apify_max_display = apify_max_jobs

markets_str = market.upper() if market != "all" else "UK + NL + SE"
print(f"\n[scout] ──────────────────────────────────────────")
print(f"[scout] Markets:     {markets_str}")
if source == "auto":
    print(f"[scout] Source:      auto — Apify + Adzuna (dual-source, always both)")
    print(f"[scout] Apify:       {len(apify_searches)} LinkedIn URLs — past 24h (in URL)")
    for label, url, per_max in apify_searches:
        print(f"[scout]              {label:<35} max {per_max} jobs")
    print(f"[scout] Adzuna:      {len(adzuna_searches)} searches — past 4 days (free)")
    print(f"[scout] Max slots:   Apify {apify_slots} total | Adzuna {adzuna_slots} total")
    est_max = apify_slots * 0.001
    print(f"[scout] Est. cost:   Apify ~${est_max:.2f} max + Adzuna $0.00 = ~${est_max:.2f}/run")
    print(f"[scout] Actor:       curious_coder/linkedin-jobs-scraper ($0.001/job)")
elif source == "apify":
    print(f"[scout] Source:      Apify only (LinkedIn, curious_coder actor)")
    if intl_age_days:
        print(f"[scout] Window:      UK=24h | NL/SE={intl_age_days}d (--intl-age override)")
    else:
        print(f"[scout] Window:      past 24h all markets (f_TPR=r86400 in URL)")
    print(f"[scout] Searches:    {len(apify_searches)} LinkedIn URLs")
    for entry in apify_searches:
        label = entry[0]; per_max = entry[2] if len(entry) == 3 else apify_max_jobs
        print(f"[scout]              {label:<35} max {per_max} jobs")
    est_max = apify_slots * 0.001
    print(f"[scout] Est. cost:   ~${est_max:.2f} max ({apify_slots} total slots × $0.001/job)")
elif source == "adzuna":
    print(f"[scout] Source:      Adzuna only (free)")
    print(f"[scout] Searches:    {len(adzuna_searches)}")
    print(f"[scout] Posting age: {_age_label_adzuna(adzuna_age)}")
    print(f"[scout] Max slots:   {adzuna_slots}")
    print(f"[scout] Cost:        FREE ({len(adzuna_searches)} API calls = {len(adzuna_searches)/250*100:.1f}% of 250 req/day)")
print(f"[scout] ──────────────────────────────────────────\n")

# ── Cache status check ────────────────────────────────────────────────────────
from scripts.apify_cache import CachedScraper, read_cache as read_apify_cache
from scripts.adzuna_scraper import AdzunaScraper, read_adzuna_cache

_ADZUNA_CC = {"uk": "gb", "nl": "nl", "se": "se"}

if source == "auto":
    apify_hits  = sum(1 for e in apify_searches_tagged
                      if read_apify_cache(e[0], e[1]) is not None)
    adzuna_hits = sum(1 for t in adzuna_searches_tagged
                      if read_adzuna_cache(t[0], t[1], ADZUNA_AGE_DAYS,
                                           _ADZUNA_CC.get(t[3], "gb")) is not None)
    apify_live  = len(apify_searches_tagged)  - apify_hits
    adzuna_live = len(adzuna_searches_tagged) - adzuna_hits
    print(f"[scout] Cache: Apify {apify_hits}/{len(apify_searches_tagged)} hits, "
          f"Adzuna {adzuna_hits}/{len(adzuna_searches_tagged)} hits")
    if apify_live == 0:
        print(f"[scout] All Apify results from cache — $0.00")
    else:
        live_slots = sum(t[2] for t in apify_searches_tagged
                         if read_apify_cache(t[0], t[1]) is None)
        print(f"[scout] Apify: {apify_live} live call(s) → ~${live_slots * 0.001:.2f} max")
    print(f"[scout] Adzuna: {adzuna_live} live API call(s) → free")

elif source == "apify":
    cache_hits = sum(1 for e in apify_searches_tagged
                     if read_apify_cache(e[0], e[1]) is not None)
    live_calls = len(apify_searches_tagged) - cache_hits
    if live_calls == 0:
        print(f"[scout] All results from Apify cache — $0.00 cost")
    else:
        est_live = live_calls * apify_max_jobs * 0.001
        print(f"[scout] Cache: {cache_hits}/{len(apify_searches_tagged)} cached → {live_calls} live call(s)")
        print(f"[scout] Est. cost: ~${est_live:.2f} max ({live_calls} URLs × {apify_max_jobs} × $0.001/job)")

elif source == "adzuna":
    cache_hits = sum(1 for t in adzuna_searches_tagged
                     if read_adzuna_cache(t[0], t[1], adzuna_age,
                                          _ADZUNA_CC.get(t[3], "gb")) is not None)
    live_calls = len(adzuna_searches_tagged) - cache_hits
    print(f"[scout] Cache: {cache_hits}/{len(adzuna_searches_tagged)} cached → {live_calls} live Adzuna call(s) (free)")

# ── Dry run — print search lists and exit ────────────────────────────────────
if dry_run:
    if source in ("auto", "apify"):
        print(f"\n[scout] Apify searches ({len(apify_searches)}) — per-URL caps:")
        for entry in apify_searches:
            label, url = entry[0], entry[1]
            per_max = entry[2] if len(entry) == 3 else apify_max_jobs
            print(f"  • {label:<35} max {per_max} jobs  (curious_coder, $0.001/job)")
            print(f"    {url[:90]}{'...' if len(url) > 90 else ''}")
    if source in ("auto", "adzuna"):
        print(f"\n[scout] Adzuna searches ({len(adzuna_searches)}):")
        for kw, loc, mj in adzuna_searches:
            print(f"  • {kw:<35} / {loc}  (max {mj})")
    print(f"\n[scout] --dry-run: exiting without making any API calls.")
    sys.exit(0)

if yes:
    print("\n[scout] --yes flag set — skipping confirmation prompt")
else:
    confirm = input("\nProceed? [Y/n]: ").strip().lower()
    if confirm not in ("", "y", "yes"):
        print("[scout] Aborted.")
        sys.exit(0)

# ── Concurrent run prevention ─────────────────────────────────────────────────
def _acquire_lock():
    """Return True if lock acquired; False if another run is actively in progress."""
    if LOCK_FILE.exists():
        try:
            info  = json.loads(LOCK_FILE.read_text())
            pid   = info.get("pid", 0)
            age_h = (time.time() - LOCK_FILE.stat().st_mtime) / 3600
            try:
                os.kill(pid, 0)   # signal 0 = existence check only
                alive = True
            except (ProcessLookupError, PermissionError):
                alive = False
            if alive and age_h < 2:
                print(f"[scout] Another run is in progress "
                      f"(PID {pid}, started {info.get('started_at', '?')}). Exiting.")
                return False
            print(f"[scout] WARNING: stale lock found "
                  f"(PID {pid}, {age_h:.1f}h old, process {'alive' if alive else 'dead'}). "
                  f"Overriding.")
        except Exception:
            pass   # corrupt/unreadable lock file — overwrite
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(json.dumps({
        "pid": os.getpid(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }))
    return True

if not _acquire_lock():
    sys.exit(0)
import atexit
atexit.register(lambda: LOCK_FILE.unlink(missing_ok=True))

# ── Load credentials ──────────────────────────────────────────────────────────
apify_token    = None
adzuna_app_id  = None
adzuna_app_key = None
try:
    from scripts.apify_cache import load_token
    apify_token = load_token()
except Exception:
    pass
try:
    from scripts.adzuna_scraper import load_credentials
    adzuna_app_id, adzuna_app_key = load_credentials()
except Exception:
    pass

def _make_apify():
    return CachedScraper(token=apify_token)

def _make_adzuna():
    if not adzuna_app_id or not adzuna_app_key:
        print("\n[scout] ERROR: ADZUNA_APP_ID / ADZUNA_APP_KEY not set in .env")
        print("  Register free at https://developer.adzuna.com/")
        print("  Then add ADZUNA_APP_ID and ADZUNA_APP_KEY to your .env file")
        sys.exit(1)
    return AdzunaScraper(adzuna_app_id, adzuna_app_key)

# ── Run scrape ────────────────────────────────────────────────────────────────
def _scrape_apify_all_markets(scraper, tagged, max_j):
    """Run Apify per market, stamp market on each job, return combined list."""
    all_jobs = []
    for mkt_tag in sorted(set(t[3] for t in tagged)):
        mkt_list = [(t[0], t[1], t[2]) for t in tagged if t[3] == mkt_tag]
        print(f"[scout] Apify [{mkt_tag.upper()}]: {len(mkt_list)} URL(s)...")
        mkt_jobs = scraper.get_batch(mkt_list, max_jobs=max_j)
        for j in mkt_jobs:
            j["market"] = mkt_tag
        all_jobs.extend(mkt_jobs)
        print(f"[scout] Apify [{mkt_tag.upper()}]: {len(mkt_jobs)} jobs")
    return all_jobs

def _scrape_adzuna_all_markets(scraper, tagged, age_days):
    """Run Adzuna per market with correct country_code, stamp market, return combined list."""
    all_jobs = []
    for mkt_tag in sorted(set(t[3] for t in tagged)):
        mkt_list = [(t[0], t[1], t[2]) for t in tagged if t[3] == mkt_tag]
        cc = _ADZUNA_CC.get(mkt_tag, "gb")
        print(f"[scout] Adzuna [{mkt_tag.upper()}]: {len(mkt_list)} searches (country={cc})...")
        mkt_jobs = scraper.get_batch(mkt_list, post_age_days=age_days, country_code=cc)
        for j in mkt_jobs:
            j["market"] = mkt_tag
        all_jobs.extend(mkt_jobs)
        print(f"[scout] Adzuna [{mkt_tag.upper()}]: {len(mkt_jobs)} jobs")
    return all_jobs

# ── Pre-run Apify budget snapshot (for cost delta in monitoring) ──────────────
def _write_pre_run_apify_snapshot():
    try:
        from urllib import request as _req2
        token = apify_token
        if not token:
            return
        req = _req2.Request(
            f"https://api.apify.com/v2/users/me/usage/monthly?token={token}",
            headers={"Content-Type": "application/json"},
        )
        with _req2.urlopen(req, timeout=10) as r:
            d = json.loads(r.read())
        services = d["data"]["monthlyServiceUsage"]
        used = sum(v.get("amountAfterVolumeDiscountUsd", 0) for v in services.values())
        snap_path = ROOT / "data" / "monitoring" / "pre_run_snapshot.json"
        snap_path.parent.mkdir(parents=True, exist_ok=True)
        snap_path.write_text(json.dumps({
            "timestamp":            datetime.now().isoformat(timespec="seconds"),
            "apify_used_usd_before": round(used, 4),
        }))
        print(f"[scout] Pre-run Apify snapshot: ${used:.4f} used this cycle")
    except Exception as e:
        print(f"[scout] Pre-run snapshot skipped: {e}")

if source in ("auto", "apify"):
    _write_pre_run_apify_snapshot()

if source == "auto":
    print("[scout] Step 1/2 — Apify (LinkedIn URLs, past 24h)...")
    apify_jobs = _scrape_apify_all_markets(_make_apify(), apify_searches_tagged, apify_max_jobs)
    print(f"[scout] Apify total: {len(apify_jobs)} unique jobs across all markets")

    print("[scout] Step 2/2 — Adzuna (past 4 days)...")
    adzuna_jobs = _scrape_adzuna_all_markets(_make_adzuna(), adzuna_searches_tagged, ADZUNA_AGE_DAYS)

    # Merge: Apify data wins — Adzuna only adds URLs not already in Apify results
    seen_urls = {j.get("job_url") or j.get("url") for j in apify_jobs
                 if j.get("job_url") or j.get("url")}
    backfill  = [j for j in adzuna_jobs
                 if (j.get("job_url") or j.get("url")) not in seen_urls]
    results   = apify_jobs + backfill
    print(f"[scout] Adzuna backfill: {len(backfill)} new jobs added ({len(adzuna_jobs)} total, "
          f"{len(adzuna_jobs) - len(backfill)} URL-deduped against Apify)")
    print(f"[scout] Combined total:  {len(results)} unique jobs")

elif source == "apify":
    try:
        results = _scrape_apify_all_markets(_make_apify(), apify_searches_tagged, apify_max_jobs)
        print(f"\n[scout] Source used: Apify ({len(results)} unique jobs)")
    except RuntimeError as e:
        print(f"\n[scout] ERROR: Apify run failed — {e}")
        print(f"[scout]   Check APIFY_TOKEN in .env and apify.com/account")
        print(f"[scout]   Fallback: python3 scripts/run_scout.py --source adzuna")
        sys.exit(1)

elif source == "adzuna":
    results = _scrape_adzuna_all_markets(_make_adzuna(), adzuna_searches_tagged, adzuna_age)
    print(f"\n[scout] Source used: Adzuna ({len(results)} unique jobs)")

# ── Save raw output ───────────────────────────────────────────────────────────
raw_path = ROOT / "data" / "pipeline" / "raw_scrape_output.json"
raw_path.parent.mkdir(parents=True, exist_ok=True)
raw_path.write_text(json.dumps(results, indent=2, ensure_ascii=False))
print(f"\n[scout] Saved {len(results)} raw jobs → {raw_path}")

# ── Run enrichment ────────────────────────────────────────────────────────────
print(f"\n[scout] Running enrichment...")
enrich = subprocess.run(
    [sys.executable, str(ROOT / "scripts" / "enrich_jobs.py")],
    cwd=ROOT
)
if enrich.returncode != 0:
    print("[scout] ERROR: enrich_jobs.py failed — aborting")
    sys.exit(1)
print(f"[scout] Enriched output → data/pipeline/enriched_scrape_output.json")

# ── Run scoring ───────────────────────────────────────────────────────────────
print(f"\n[scout] Running scorer...")
score = subprocess.run(
    [sys.executable, str(ROOT / "scripts" / "score_jobs.py")],
    cwd=ROOT
)
if score.returncode != 0:
    print("[scout] ERROR: score_jobs.py failed — aborting")
    sys.exit(1)

print(f"\n[scout] Running post-run analysis...")
subprocess.run(
    [sys.executable, str(ROOT / "scripts" / "scout_analysis.py"), "--source", source],
    cwd=ROOT
)

print(f"\n[scout] Scout complete. Scored output → data/pipeline/scored_jobs.json")
print(f"[scout] Next: agent writes shortlisted entries to tracker,")
print(f"[scout]       then run: python3 scripts/sheets_sync.py push")

# ── Monitoring ─────────────────────────────────────────────────────────────
triggered_by = "github_actions" if os.environ.get("GITHUB_ACTIONS") else "local_manual"
subprocess.run(
    [sys.executable, str(ROOT / "scripts" / "monitor_scout.py"),
     "--triggered-by", triggered_by],
    cwd=ROOT
)
