#!/usr/bin/env python3
"""
fetch_jd.py — Cache-first JD lookup for application_prep agent.

Lookup order:
  0. data/jd_text_cache.json          (manually/live-fetched descriptions, keyed by job_id)
  1. data/enriched_scrape_output.json  (best quality, full history)
  2. data/raw_scrape_output.json       (pre-enrichment version)
  3. data/apify_cache/*.json           (recent Apify scrape results)
  4. data/adzuna_cache/*.json          (recent Adzuna scrape results)
  5. data/manual_jobs_input.json       (fuzzy company+role match via tracker — no job_id index needed)
  6. not_found → agent falls back to WebFetch tool

Saving live-fetched JDs:
  After fetching a JD via WebFetch, persist it with:
    python3 scripts/fetch_jd.py --save --job_id <id> --company <c> --role <r> --description "<text>"
  Or call save_to_jd_cache(job_id, company, role, description) directly from Python.

Usage:
  python3 scripts/fetch_jd.py --job_id 4429589502
  python3 scripts/fetch_jd.py --job_id 4429589502 --jd_url https://...
  python3 scripts/fetch_jd.py            # no job_id → returns no_job_id signal

Output: single JSON line to stdout. Logs to stderr.
"""

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
JD_TEXT_CACHE    = DATA_DIR / "jd_text_cache.json"
TRACKER_PATH     = DATA_DIR / "job_tracker.json"
MANUAL_JOBS_PATH = DATA_DIR / "manual_jobs_input.json"


def save_to_jd_cache(job_id: str, company: str, role: str, description: str) -> None:
    """Persist a live-fetched JD so future fetch_jd calls don't need WebFetch."""
    cache = {}
    if JD_TEXT_CACHE.exists():
        try:
            with open(JD_TEXT_CACHE) as f:
                cache = json.load(f)
        except Exception:
            pass
    cache[str(job_id)] = {
        "description": description,
        "company": company,
        "role": role,
        "source": "webfetch",
        "cached_at": str(date.today()),
    }
    with open(JD_TEXT_CACHE, "w") as f:
        json.dump(cache, f, indent=2)
    log(f"Saved JD for job_id={job_id} to jd_text_cache.json")


def search_jd_text_cache(job_id: str) -> dict | None:
    """Check data/jd_text_cache.json for a manually/live-cached JD."""
    if not JD_TEXT_CACHE.exists():
        return None
    try:
        with open(JD_TEXT_CACHE) as f:
            cache = json.load(f)
        entry = cache.get(str(job_id))
        if entry and entry.get("description"):
            return {
                "job_id": job_id,
                "description": entry["description"],
                "company_name": entry.get("company", ""),
                "job_title": entry.get("role", ""),
            }
    except Exception as e:
        log(f"Warning: could not read jd_text_cache.json: {e}")
    return None


def log(msg: str):
    print(f"[fetch_jd] {msg}", file=sys.stderr)


def search_flat_file(path: Path, job_id: str) -> dict | None:
    """Search a root-level JSON array for a matching job_id."""
    if not path.exists():
        return None
    try:
        with open(path) as f:
            jobs = json.load(f)
        if not isinstance(jobs, list):
            return None
        for job in jobs:
            # check "job_id" (enriched) and "id" (raw Apify output) fields
            if str(job.get("job_id") or job.get("id") or "") == job_id:
                return job
    except Exception as e:
        log(f"Warning: could not read {path.name}: {e}")
    return None


def search_cache_dir(cache_dir: Path, job_id: str, source_label: str) -> dict | None:
    """Search all .json files in a cache directory for a matching job_id."""
    if not cache_dir.exists():
        return None
    for cache_file in sorted(cache_dir.glob("*.json")):
        try:
            with open(cache_file) as f:
                data = json.load(f)
            results = data.get("results", [])
            for job in results:
                # check "job_id" (enriched) and "id" (raw Apify output) fields
                if str(job.get("job_id") or job.get("id") or "") == job_id:
                    log(f"Hit in {source_label}: {cache_file.name}")
                    return job
        except Exception as e:
            log(f"Warning: could not read {cache_file.name}: {e}")
    return None


def build_result(source: str, job: dict) -> dict:
    return {
        "source": source,
        "description": job.get("description") or "",
        "job_title": job.get("job_title", ""),
        "company": job.get("company_name", job.get("company", "")),
        "job_url": job.get("job_url", ""),
    }


def search_manual_jobs(job_id: str) -> dict | None:
    """Fuzzy-match by company+role from tracker → description in manual_jobs_input.json."""
    if not MANUAL_JOBS_PATH.exists():
        return None
    try:
        # Look up company, agency_name, and role for this job_id from the tracker
        tracker_company, tracker_role, tracker_agency = "", "", ""
        if TRACKER_PATH.exists():
            tracker_data = json.loads(TRACKER_PATH.read_text())
            for e in tracker_data.get("applications", []):
                if str(e.get("job_id", "")) == str(job_id):
                    tracker_company = (e.get("company") or "").lower()
                    tracker_role    = (e.get("role") or "").lower()
                    tracker_agency  = (e.get("agency_name") or "").lower()
                    break

        if not tracker_company and not tracker_role:
            return None

        manual_data = json.loads(MANUAL_JOBS_PATH.read_text())
        jobs_list = manual_data if isinstance(manual_data, list) else manual_data.get("jobs", [])

        best_j, best_score = None, 0
        for j in jobs_list:
            desc = j.get("description") or ""
            if not desc:
                continue
            m_co   = (j.get("company_name") or "").lower()
            m_role = (j.get("job_title") or "").lower()
            # Try primary company name; if that fails, try agency_name as a secondary signal
            co_overlap = set(tracker_company.split()) & set(m_co.split())
            if len(co_overlap) < 1 and tracker_agency:
                co_overlap = set(tracker_agency.split()) & set(m_co.split())
            role_overlap = set(tracker_role.split()) & set(m_role.split())
            if len(co_overlap) < 1 or len(role_overlap) < 2:
                continue
            # Prefer the entry whose role is most similar in length (fewer extra words)
            t_words = set(tracker_role.split())
            m_words = set(m_role.split())
            precision = len(role_overlap) / max(len(m_words), 1)
            recall    = len(role_overlap) / max(len(t_words), 1)
            score = precision + recall
            if score > best_score:
                best_score = score
                best_j = j
        if best_j:
            desc = best_j.get("description", "")
            m_co = (best_j.get("company_name") or "").lower()
            log(f"Hit in manual_jobs_input.json via fuzzy match "
                f"({tracker_company!r} ≈ {m_co!r}) ({len(desc)} chars)")
            return {
                "job_id":       job_id,
                "description":  desc,
                "company_name": best_j.get("company_name", ""),
                "job_title":    best_j.get("job_title", ""),
            }
    except Exception as e:
        log(f"Warning: could not read manual_jobs_input.json: {e}")
    return None


def main():
    parser = argparse.ArgumentParser(description="Cache-first JD lookup")
    parser.add_argument("--job_id", default="", help="LinkedIn job ID (numeric string)")
    parser.add_argument("--jd_url", default="", help="LinkedIn JD URL (informational)")
    parser.add_argument("--save", action="store_true", help="Save a live-fetched JD to jd_text_cache.json")
    parser.add_argument("--company", default="", help="Company name (used with --save)")
    parser.add_argument("--role", default="", help="Role title (used with --save)")
    parser.add_argument("--description", default="", help="JD description text (used with --save)")
    args = parser.parse_args()

    if args.save:
        save_to_jd_cache(args.job_id, args.company, args.role, args.description)
        print(json.dumps({"saved": True, "job_id": args.job_id}))
        return

    job_id = (args.job_id or "").strip()

    if not job_id or job_id in ("null", "None", ""):
        log("No job_id provided — WebFetch required (Excel import or manual entry)")
        print(json.dumps({
            "source": "no_job_id",
            "description": None,
            "reason": "job_id missing — WebFetch required"
        }))
        return

    log(f"Looking up job_id={job_id}")

    # 0. jd_text_cache.json (live-fetched / manually saved)
    job = search_jd_text_cache(job_id)
    if job:
        log(f"Hit in jd_text_cache.json ({len(job.get('description',''))} chars)")
        print(json.dumps(build_result("jd_text_cache", job)))
        return

    # 1. enriched_scrape_output.json
    job = search_flat_file(DATA_DIR / "pipeline" / "enriched_scrape_output.json", job_id)
    if job:
        log(f"Hit in enriched_scrape_output.json ({len(job.get('description',''))} chars)")
        print(json.dumps(build_result("enriched", job)))
        return

    # 2. raw_scrape_output.json
    job = search_flat_file(DATA_DIR / "pipeline" / "raw_scrape_output.json", job_id)
    if job:
        log(f"Hit in raw_scrape_output.json ({len(job.get('description',''))} chars)")
        print(json.dumps(build_result("raw", job)))
        return

    # 3. Apify cache
    job = search_cache_dir(DATA_DIR / "apify_cache", job_id, "apify_cache")
    if job:
        print(json.dumps(build_result("apify_cache", job)))
        return

    # 4. Adzuna cache
    job = search_cache_dir(DATA_DIR / "adzuna_cache", job_id, "adzuna_cache")
    if job:
        print(json.dumps(build_result("adzuna_cache", job)))
        return

    # 5. manual_jobs_input.json (fuzzy company+role match via tracker)
    job = search_manual_jobs(job_id)
    if job:
        print(json.dumps(build_result("manual_jobs_input", job)))
        return

    # 5.5 URL-embedded job_id fallback — Apify actor "id" sometimes diverges from
    # the LinkedIn numeric ID in the job_url. Try extracting from jd_url if provided.
    _url_m = re.search(r'[/-](\d{9,13})(?:[?/]|$)', args.jd_url or "")
    if _url_m:
        url_job_id = _url_m.group(1)
        if url_job_id != job_id:
            log(f"Retrying with URL-extracted job_id={url_job_id} (stored was {job_id})")
            job = search_jd_text_cache(url_job_id)
            if not job:
                job = search_flat_file(DATA_DIR / "pipeline" / "enriched_scrape_output.json", url_job_id)
            if not job:
                job = search_flat_file(DATA_DIR / "pipeline" / "raw_scrape_output.json", url_job_id)
            if not job:
                job = search_cache_dir(DATA_DIR / "apify_cache", url_job_id, "apify_cache")
            if not job:
                job = search_cache_dir(DATA_DIR / "adzuna_cache", url_job_id, "adzuna_cache")
            if job:
                log(f"URL-fallback hit for url_job_id={url_job_id}")
                print(json.dumps(build_result("url_fallback", job)))
                return

    # 6. Not found in any local cache
    log(f"job_id={job_id} not found in any local cache — WebFetch fallback needed")
    print(json.dumps({
        "source": "not_found",
        "description": None,
        "reason": "job_id not in any local cache",
        "jd_url": args.jd_url,
    }))


if __name__ == "__main__":
    main()
