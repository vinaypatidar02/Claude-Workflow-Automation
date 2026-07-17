#!/usr/bin/env python3
"""
enrich_jobs.py — Post-processing enrichment for raw Apify scrape output
========================================================================
Lightweight normaliser: adds experience_years, ATS URL detection, and
recency sort. All semantic extraction (salary, visa, work mode, contract,
agency) is handled by Claude in score_jobs.py Pass 2 using raw fields.

Run:
  python3 scripts/enrich_jobs.py

Input:  data/raw_scrape_output.json   (from run_scout.py)
Output: data/enriched_scrape_output.json
"""

import json, re, sys
from pathlib import Path
from typing import Optional

ROOT     = Path(__file__).parent.parent
RAW_PATH = ROOT / "data" / "pipeline" / "raw_scrape_output.json"
OUT_PATH = ROOT / "data" / "pipeline" / "enriched_scrape_output.json"

if not RAW_PATH.exists():
    print(f"ERROR: {RAW_PATH} not found.")
    print("  Run python3 scripts/run_scout.py first.")
    sys.exit(1)

jobs = json.loads(RAW_PATH.read_text())
print(f"\n[enrich] Loaded {len(jobs)} jobs from {RAW_PATH.name}")


# ─────────────────────────────────────────────────────────────────────────────
# EXTRACTOR 1 — Years of experience (deterministic regex, factual extraction)
# ─────────────────────────────────────────────────────────────────────────────
def extract_experience_years(text: str) -> dict:
    """
    Extracts years of experience requirement from description.
    Returns: found, min_yrs, max_yrs, display
    """
    if not text:
        return {"found": False, "min_yrs": None, "max_yrs": None,
                "display": "Not specified"}

    # Range: 3-5 years / 3 to 5 years
    m = re.search(
        r"(\d+)\s*(?:-|to|–|and)\s*(\d+)\s+years?(?:'s?)?\s*"
        r"(?:of\s+)?(?:relevant\s+)?experience",
        text, re.IGNORECASE)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        return {"found": True, "min_yrs": lo, "max_yrs": hi,
                "display": f"{lo}–{hi} years"}

    # Single: 5+ years / at least 6 years / minimum 4 years
    m2 = re.search(
        r"(?:at\s+least\s+|minimum\s+(?:of\s+)?)?(\d+)\+?\s+years?"
        r"(?:'s?)?\s*(?:of\s+)?(?:relevant\s+)?experience",
        text, re.IGNORECASE)
    if m2:
        yrs = int(m2.group(1))
        return {"found": True, "min_yrs": yrs, "max_yrs": None,
                "display": f"{yrs}+ years"}

    # Soft: several years / extensive experience
    if re.search(r"\bseveral\s+years\b|\bextensive\s+experience\b", text, re.I):
        return {"found": True, "min_yrs": None, "max_yrs": None,
                "display": "Several years (not specified)"}

    return {"found": False, "min_yrs": None, "max_yrs": None,
            "display": "Not specified"}


# ─────────────────────────────────────────────────────────────────────────────
# EXTRACTOR 2 — Remote-only hint (pre-Claude P1 cost saver)
# ─────────────────────────────────────────────────────────────────────────────
# Unambiguous signals only — "fully remote", "100% remote", etc.
# Ambiguous language ("flexible", "happy to consider outside London") is left
# for Claude P2 to evaluate from full JD context.
_REMOTE_ONLY_SIGNALS = [
    "100% remote", "fully remote", "remote only", "remote-only",
    "remote first", "remote-first", "entirely remote", "work from anywhere",
    "no office requirement",
]

def _detect_remote_hint(description: str) -> bool:
    d = (description or "").lower()
    return any(sig in d for sig in _REMOTE_ONLY_SIGNALS)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ENRICHMENT LOOP
# ─────────────────────────────────────────────────────────────────────────────
enriched = []
stats    = {"experience": 0, "apply_url": 0}

# ATS URL patterns — used for description-based URL extraction
# (Claude identifies the ATS type semantically in score_jobs.py)
ATS_PATTERNS = {
    "greenhouse":      r"https?://(?:boards\.)?greenhouse\.io/[\w/-]+",
    "lever":           r"https?://jobs\.lever\.co/[\w/-]+",
    "workday":         r"https?://[\w-]+\.myworkdayjobs\.com/[\w/-]+",
    "ashby":           r"https?://jobs\.ashbyhq\.com/[\w/-]+",
    "smartrecruiters": r"https?://jobs\.smartrecruiters\.com/[\w/-]+",
    "icims":           r"https?://[\w-]+\.icims\.com/jobs/[\w/-]+",
    "bamboohr":        r"https?://[\w-]+\.bamboohr\.com/jobs/[\w/-]+",
    "teamtailor":      r"https?://[\w-]+\.teamtailor\.com/jobs/[\w/-]+",
    "screenloop":      r"https?://[\w-]+\.screenloop\.io/[\w/-]+",
    "hackajob":        r"https?://hackajob\.com/[\w/.-]+",
}

# Passive platforms — employer-initiated contact only, no direct apply button.
# When detected, ats_type is renamed to "<name>_passive" and a warning is printed.
_PASSIVE_PLATFORMS = {"hackajob"}

# Apply-related keywords for proximity scoring
APPLY_KW = re.compile(
    r"\b(apply\b|application\b|submit your|apply now|apply via|"
    r"apply at|apply through|apply using|apply for this role|"
    r"apply for this position|click here to apply)\b",
    re.IGNORECASE
)

for i, job in enumerate(jobs):
    desc = job.get("description", "") or ""

    # ── ATS URL proximity detection ────────────────────────────────────────────
    # Collects all ATS URL matches and picks the one nearest to "apply" language.
    # This avoids grabbing stale Lever/Greenhouse URLs from reference sections
    # when the actual apply button points to a different ATS (e.g. Zeno London).
    career_page_url = None
    ats_type = "unknown"
    ats_candidates = []  # (dist_to_apply_keyword, ats_name, url)

    for name, pattern in ATS_PATTERNS.items():
        for m in re.finditer(pattern, desc, re.IGNORECASE):
            pos = m.start()
            window_start = max(0, pos - 300)
            window = desc[window_start: pos + 300]
            apply_m = APPLY_KW.search(window)
            if apply_m:
                dist = abs((pos - window_start) - apply_m.start())
            else:
                dist = 9999  # no apply keyword nearby — lowest priority
            ats_candidates.append((dist, name, m.group(0)))

    if ats_candidates:
        ats_candidates.sort(key=lambda x: x[0])
        _, ats_type, career_page_url = ats_candidates[0]

    # Fallback: use apply_url_hint from Apify actor (applyUrl field) when
    # description-regex extraction found nothing. apply_url_hint is already
    # pre-filtered to known ATS domains in apify_cache._normalize().
    if not career_page_url and job.get("apply_url_hint"):
        career_page_url = job["apply_url_hint"]
        for name, pattern in ATS_PATTERNS.items():
            if re.search(pattern, career_page_url, re.IGNORECASE):
                ats_type = name
                break
        else:
            ats_type = "direct"

    # Passive platform detection — rename ats_type to "<name>_passive"
    if ats_type in _PASSIVE_PLATFORMS:
        ats_type = f"{ats_type}_passive"

    is_easy = bool(re.search(r"\beasy\s*apply\b", desc, re.IGNORECASE))

    # ── Experience years (deterministic regex) ─────────────────────────────────
    exp = extract_experience_years(desc)

    print(f"\n[enrich] {i+1}/{len(jobs)}: "
          f"{job.get('job_title','?')} @ {job.get('company_name','?')}")
    print(f"  experience:   {'✓ ' + exp['display'] if exp['found'] else '✗ not specified'}")
    if career_page_url and ats_type.endswith("_passive"):
        print(f"  career URL:   ⚠ PASSIVE PLATFORM ({ats_type}) — employer-initiated only, no direct apply: {career_page_url[:55]}")
    elif career_page_url:
        print(f"  career URL:   ✓ found in description ({ats_type}): {career_page_url[:60]}")
    elif is_easy:
        print(f"  career URL:   ⚠ Easy Apply (no external URL)")
    else:
        print(f"  career URL:   — needs manual input after approval")

    if exp['found']:     stats["experience"]  += 1
    if career_page_url:  stats["apply_url"]   += 1

    remote_hint = _detect_remote_hint(desc)
    if remote_hint:
        print(f"  remote_hint:  ⚠ unambiguous remote-only signal detected")

    enriched.append({
        **job,
        "experience_years": exp,
        "career_page_url":  career_page_url,  # null until manually filled or found above
        "ats_type":         ats_type,
        "is_easy_apply":    is_easy,
        "remote_hint":      remote_hint,
    })

# ── Sort by recency before saving ─────────────────────────────────────────────
def recency_sort_key(job):
    d = str(job.get("posted_date") or job.get("postedDate") or "").strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", d):
        return (0, d)
    m = re.search(r"(\d+)\s+(hours?|days?|weeks?|months?)", d, re.I)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower().rstrip("s")
        days = n * {"hour": 1/24, "day": 1, "week": 7, "month": 30}[unit]
        return (0, f"{99999 - days:013.3f}")
    return (2, "")

enriched_sorted = sorted(enriched, key=recency_sort_key, reverse=True)
OUT_PATH.write_text(json.dumps(enriched_sorted, indent=2, ensure_ascii=False))
n = len(enriched_sorted)
print(f"""
[enrich] ────────────────────────────────────────
  Jobs processed: {n}  (sorted newest first)
  Experience:     {stats['experience']}/{n} found
  Apply URL:      {stats['apply_url']}/{n} found in description
  Note: salary, visa, work mode, contract, agency → Claude (score_jobs.py)
[saved] {OUT_PATH}
[enrich] ────────────────────────────────────────
""")
