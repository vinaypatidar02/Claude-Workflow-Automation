#!/usr/bin/env python3
"""
inject_manual.py — Inject manually-sourced jobs into the raw scrape output pipeline
=====================================================================================
Reads data/manual_jobs_input.json, stamps market and _source fields,
and appends new jobs to data/pipeline/raw_scrape_output.json (deduped by job_url).

After running this script, re-run enrich_jobs.py and score_jobs.py.
Jobs already in job_tracker.json will be deduped by score_jobs.py's
_EXISTING_POOL check — only the manual jobs not yet in the tracker will
be scored with Claude API calls.

Usage:
  python3 scripts/inject_manual.py                    # inject all manual jobs
  python3 scripts/inject_manual.py --dry-run          # show what would be injected
"""

import json, sys
from pathlib import Path

ROOT          = Path(__file__).parent.parent
MANUAL_PATH   = ROOT / "data" / "manual_jobs_input.json"
RAW_PATH      = ROOT / "data" / "pipeline" / "raw_scrape_output.json"

DRY_RUN = "--dry-run" in sys.argv

# ── Market derivation from location string ────────────────────────────────────

_NL_SIGNALS = {"netherlands", "amsterdam", "rotterdam", "den haag", "the hague", "utrecht"}
_SE_SIGNALS = {"sweden", "stockholm", "gothenburg", "göteborg", "malmo", "malmö"}

def _derive_market(location: str) -> str:
    loc = (location or "").lower()
    if any(s in loc for s in _NL_SIGNALS):
        return "nl"
    if any(s in loc for s in _SE_SIGNALS):
        return "se"
    return "uk"


# ── Load inputs ───────────────────────────────────────────────────────────────

if not MANUAL_PATH.exists():
    print(f"[inject] No manual jobs file found at {MANUAL_PATH} — nothing to inject.")
    sys.exit(0)

manual_jobs = json.loads(MANUAL_PATH.read_text())
if not manual_jobs:
    print("[inject] manual_jobs_input.json is empty — nothing to inject.")
    sys.exit(0)

print(f"[inject] Loaded {len(manual_jobs)} manual job(s) from {MANUAL_PATH.name}")

# ── Load existing raw output (if present) ────────────────────────────────────

existing_jobs = []
if RAW_PATH.exists():
    existing_jobs = json.loads(RAW_PATH.read_text())
    print(f"[inject] Existing raw_scrape_output.json: {len(existing_jobs)} job(s)")
else:
    print(f"[inject] raw_scrape_output.json not found — will create from manual jobs only")

existing_urls = {
    (j.get("job_url") or j.get("url") or "").strip()
    for j in existing_jobs
    if j.get("job_url") or j.get("url")
}

# ── Prepare manual jobs for injection ────────────────────────────────────────

to_inject = []
skipped   = []

for job in manual_jobs:
    url = (job.get("job_url") or "").strip()
    if url and url in existing_urls:
        skipped.append(job.get("job_title", "?") + " @ " + job.get("company_name", "?"))
        continue

    market = _derive_market(job.get("location", ""))

    # Normalize to pipeline-compatible dict (field names already match)
    injected = {
        "job_title":    job.get("job_title", ""),
        "company_name": job.get("company_name", ""),
        "location":     job.get("location", ""),
        "salary":       job.get("salary", "Not stated"),
        "job_type":     job.get("job_type", "Full-time"),
        "posted_date":  job.get("posted_date", ""),
        "job_url":      url,
        "description":  job.get("description", ""),
        "_source":      "manual_inject",
        "market":       market,
    }
    to_inject.append(injected)
    existing_urls.add(url)   # prevent intra-batch duplicates

# ── Summary ───────────────────────────────────────────────────────────────────

print(f"\n[inject] To inject: {len(to_inject)}")
for j in to_inject:
    mkt = j["market"].upper()
    print(f"  [{mkt}] {j['job_title']} @ {j['company_name']}  ({j['location']})")

if skipped:
    print(f"\n[inject] Already in raw output (skipped): {len(skipped)}")
    for s in skipped:
        print(f"  - {s}")

if not to_inject:
    print("\n[inject] Nothing new to inject — raw_scrape_output.json unchanged.")
    sys.exit(0)

if DRY_RUN:
    print("\n[inject] --dry-run: not writing. Re-run without --dry-run to apply.")
    sys.exit(0)

# ── Write merged output ───────────────────────────────────────────────────────

merged = existing_jobs + to_inject
RAW_PATH.parent.mkdir(parents=True, exist_ok=True)
RAW_PATH.write_text(json.dumps(merged, indent=2, ensure_ascii=False))

print(f"\n[inject] Wrote {len(merged)} total jobs to {RAW_PATH}")
print(f"[inject] ({len(existing_jobs)} scraped + {len(to_inject)} injected manual)")
print(f"\n[inject] Next steps:")
print(f"  python3 scripts/enrich_jobs.py")
print(f"  python3 scripts/score_jobs.py")
