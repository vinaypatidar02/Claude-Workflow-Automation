#!/usr/bin/env python3
"""
adzuna_scraper.py — Adzuna API job scraper for UK, NL, and SE analytics roles
==============================================================================
Drop-in replacement for CachedScraper when Apify is unavailable.

Same interface as apify_cache.CachedScraper:
    scraper = AdzunaScraper(app_id, app_key)
    results = scraper.get_batch(searches, max_jobs=25, post_age_days=7,
                                country_code="gb")  # "gb" | "nl" | "se"

Key advantages over Apify/LinkedIn:
    - Free (250 req/day, 10,000/month, no credit card)
    - Precise date filtering: max_days_old accepts ANY integer (not fixed 1/7/30 buckets)
      e.g. --age 2 or --age 3 work exactly — Apify would round these up to past-week
    - Multi-country: supports /gb/, /nl/, /se/ endpoints via country_code param

Limitations vs Apify/LinkedIn:
    - No LinkedIn-specific job IDs (Adzuna uses its own IDs)
    - No native work_type field (Claude handles this in score_jobs.py Pass 2)
    - Not LinkedIn-sourced; some London-exclusive LinkedIn roles may be missing

Setup:
    1. Register free at https://developer.adzuna.com/
    2. Add to .env:  ADZUNA_APP_ID=your_id
                     ADZUNA_APP_KEY=your_key
"""

import json, os, re
from datetime import datetime
from pathlib import Path
from urllib import request, parse
from typing import Optional

ROOT      = Path(__file__).parent.parent
CACHE_DIR = ROOT / "data" / "adzuna_cache"
CACHE_TTL = 24   # hours — matches Apify cache TTL

CACHE_DIR.mkdir(parents=True, exist_ok=True)

_ADZUNA_URL_TEMPLATE = "https://api.adzuna.com/v1/api/jobs/{country_code}/search/1"
API_BASE = _ADZUNA_URL_TEMPLATE.format(country_code="gb")  # backwards-compat alias

# Title relevance filter — Adzuna `what` matches description text, not just titles.
# Keep only jobs whose title contains at least one of these substrings so that
# "Personal Injury Solicitor", "Graduate Maths Teacher" etc. are dropped before
# they reach enrich → score (saves Claude API cost and reduces tracker noise).
TITLE_KEYWORDS = {
    "analytic", "analyst", "data", "insight", "intelligence",
    "reporting", "bi manager", "bi lead", "bi director",
}

def _title_is_relevant(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in TITLE_KEYWORDS)


# ── Credential loader (mirrors apify_cache.load_token) ───────────────────────

def load_credentials() -> tuple[Optional[str], Optional[str]]:
    """Load ADZUNA_APP_ID and ADZUNA_APP_KEY from .env or environment."""
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        with open(ROOT / ".env") as _f:
            for line in _f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
    return os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")


# ── Cache helpers ─────────────────────────────────────────────────────────────

def _cache_key(keyword: str, location: str, post_age_days: int,
               country_code: str = "gb") -> str:
    raw = f"{keyword.lower().strip()}_{location.lower().strip()}_{country_code}_{post_age_days}d"
    return re.sub(r"[^a-z0-9]+", "_", raw).strip("_")

def _cache_path(keyword: str, location: str, post_age_days: int,
                country_code: str = "gb") -> Path:
    return CACHE_DIR / f"{_cache_key(keyword, location, post_age_days, country_code)}.json"

def read_adzuna_cache(keyword: str, location: str, post_age_days: int,
                      country_code: str = "gb") -> Optional[list]:
    path = _cache_path(keyword, location, post_age_days, country_code)
    if not path.exists():
        return None
    try:
        cached     = json.loads(path.read_text())
        fetched_at = datetime.fromisoformat(cached["fetched_at"])
        age_hours  = (datetime.now() - fetched_at).total_seconds() / 3600
        if age_hours >= CACHE_TTL:
            print(f"  [adzuna cache] STALE  {keyword} / {location} [{country_code}] "
                  f"({age_hours:.1f}h old, TTL={CACHE_TTL}h) — will re-fetch")
            return None
        print(f"  [adzuna cache] HIT    {keyword} / {location} [{country_code}] ({age_hours:.1f}h old)")
        return cached["results"]
    except Exception:
        return None

def _write_cache(keyword: str, location: str, post_age_days: int, results: list,
                  max_jobs: int = None, country_code: str = "gb"):
    path = _cache_path(keyword, location, post_age_days, country_code)
    path.write_text(json.dumps({
        "fetched_at":    datetime.now().isoformat(),
        "post_age_days": post_age_days,
        "keyword":       keyword,
        "location":      location,
        "country_code":  country_code,
        "requested_max": max_jobs,
        "results":       results,
    }, indent=2, ensure_ascii=False))


# ── Field normalisation ───────────────────────────────────────────────────────

def _normalize(item: dict, country_code: str = "gb") -> dict:
    """Normalise an Adzuna result to the pipeline's flat job-dict contract."""
    sal_min = item.get("salary_min")
    sal_max = item.get("salary_max")
    _CURRENCY_PFX = {"gb": "£", "nl": "€", "se": "SEK "}
    cur = _CURRENCY_PFX.get(country_code, "£")
    if sal_min and sal_max:
        salary_str = f"{cur}{int(sal_min):,} – {cur}{int(sal_max):,}"
    elif sal_min:
        salary_str = f"{cur}{int(sal_min):,}+"
    else:
        salary_str = ""

    return {
        "job_title":    item.get("title", ""),
        "company_name": (item.get("company") or {}).get("display_name", "Unknown"),
        "location":     (item.get("location") or {}).get("display_name", ""),
        "description":  item.get("description", ""),
        "job_url":      item.get("redirect_url", ""),
        "posted_date":  (item.get("created") or "")[:10],   # ISO → "YYYY-MM-DD"
        "salary":       salary_str,
        "job_id":       str(item.get("id", "")),
        "work_type":    "",          # not in Adzuna — Claude handles in Pass 2
        "job_type":     "full-time", # we always filter full_time=1
        "_source":      "adzuna",
    }


# ── API call ──────────────────────────────────────────────────────────────────

_COUNTRY_LEVEL = {
    "united kingdom", "uk", "great britain", "england", "britain",
    "netherlands", "the netherlands", "holland",
    "sweden",
}

def call_adzuna(keyword: str, location: str,
                app_id: str, app_key: str,
                max_jobs: int = 25, post_age_days: int = 7,
                country_code: str = "gb") -> list:
    url_base = _ADZUNA_URL_TEMPLATE.format(country_code=country_code)
    # Strip country suffix: "London, United Kingdom" → "London"
    where = location.split(",")[0].strip()
    params = {
        "app_id":           app_id,
        "app_key":          app_key,
        "what":             keyword,
        "full_time":        1,
        "max_days_old":     post_age_days,    # exact integer — any value works
        "results_per_page": min(max_jobs, 50),
        "content-type":     "application/json",
    }
    # Omit 'where' for country-level locations — endpoint already scopes by country
    if where.lower() not in _COUNTRY_LEVEL:
        params["where"] = where
    url = f"{url_base}?{parse.urlencode(params)}"
    print(f"  [adzuna] Calling API: {keyword} / {location} [{country_code}] "
          f"(max {max_jobs}, last {post_age_days} day(s))")
    with request.urlopen(url, timeout=30) as r:
        data = json.loads(r.read())
    items = data.get("results", [])
    print(f"  [adzuna] Returned {len(items)} results")
    return [_normalize(item, country_code) for item in items]


# ── Scraper class — same interface as CachedScraper ──────────────────────────

class AdzunaScraper:
    """
    Drop-in replacement for apify_cache.CachedScraper.
    Same get_batch() signature; caches to data/adzuna_cache/.

    Usage:
        scraper = AdzunaScraper(app_id, app_key)
        results = scraper.get_batch(searches, max_jobs=25, post_age_days=7)
    """

    def __init__(self, app_id: str = None, app_key: str = None):
        if not app_id or not app_key:
            app_id, app_key = load_credentials()
        if not app_id or not app_key:
            raise ValueError(
                "ADZUNA_APP_ID / ADZUNA_APP_KEY not found.\n"
                "  Register free at https://developer.adzuna.com/\n"
                "  Then add to .env: ADZUNA_APP_ID=... ADZUNA_APP_KEY=..."
            )
        self.app_id  = app_id
        self.app_key = app_key
        self.stats   = {"cache_hits": 0, "api_calls": 0, "total_jobs": 0}

    def get(self, keyword: str, location: str,
            max_jobs: int = 25, post_age_days: int = 7,
            country_code: str = "gb") -> list:
        cached = read_adzuna_cache(keyword, location, post_age_days, country_code)
        if cached is not None:
            self.stats["cache_hits"] += 1
            self.stats["total_jobs"] += len(cached)
            return cached

        self.stats["api_calls"] += 1
        results = call_adzuna(keyword, location, self.app_id, self.app_key,
                              max_jobs, post_age_days, country_code)
        _write_cache(keyword, location, post_age_days, results, max_jobs, country_code)
        self.stats["total_jobs"] += len(results)
        return results

    def get_batch(self, searches: list, max_jobs: int = 25,
                  post_age_days: int = 7, country_code: str = "gb") -> list:
        """
        Run multiple searches, deduplicated by job_url.
        searches: [(keyword, location), ...] or [(keyword, location, max_jobs), ...]
        3-tuples use per-entry max_jobs; 2-tuples fall back to the global max_jobs param.
        post_age_days: exact integer (any value) — Adzuna max_days_old param.
        country_code: "gb" (UK), "nl" (Netherlands), "se" (Sweden) — applied to all entries
                      in this batch. Call get_batch separately per market from run_scout.py.

        Title pre-filter: Adzuna's `what` matches across full descriptions, so
        results include noisy off-topic jobs. Only jobs whose title contains a
        data/analytics keyword are kept — irrelevant titles are dropped before
        they reach enrich_jobs.py and score_jobs.py.
        """
        seen_urls = set()
        combined  = []
        filtered  = 0
        for entry in searches:
            if len(entry) == 3:
                keyword, location, entry_max = entry
            else:
                keyword, location = entry
                entry_max = max_jobs
            results = self.get(keyword, location, entry_max, post_age_days, country_code)
            for job in results:
                title = job.get("job_title", "")
                if not _title_is_relevant(title):
                    filtered += 1
                    continue
                url = job.get("job_url") or job.get("url") or ""
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    combined.append(job)
                elif not url:
                    combined.append(job)

        print(f"\n  [adzuna] Batch complete [{country_code.upper()}]:")
        print(f"    Cache hits:    {self.stats['cache_hits']}")
        print(f"    API calls:     {self.stats['api_calls']}")
        print(f"    Title-filtered: {filtered} irrelevant titles dropped")
        print(f"    Total unique:  {len(combined)} jobs (source: Adzuna/{country_code.upper()})")
        return combined
