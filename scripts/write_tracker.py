#!/usr/bin/env python3
"""
write_tracker.py — Write scored_jobs.json entries to job_tracker.json
======================================================================
Called by job_scout agent (Step 2) after score_jobs.py completes.

Reads:  data/scored_jobs.json      (scored + stale entries from this run)
Writes: data/job_tracker.json      (new Shortlisted / Review Needed / Stale entries,
                                    plus in-place updates for matched active entries)

Match decisions (set by score_jobs.py via _find_match()):
  "no_match"         → new tracker entry (app_XXX)
  "new_entry"        → new tracker entry; matched_entry_id points to old superseded entry
  "update_in_place"  → update existing entry's metadata fields (no new entry created)
  "dedup"            → never reaches here (score_jobs.py skips dedup jobs)

Stale entries:
  Written to tracker as status=Stale so they appear in the Sheet (grey row)
  and the user can see the volume of near-miss postings.

Usage:
  python3 scripts/write_tracker.py             # default
  python3 scripts/write_tracker.py --dry-run   # print what would change, no write
"""

import json, re, sys
from pathlib import Path
from datetime import date
from typing import Optional

ROOT         = Path(__file__).parent.parent
SCORED_PATH  = ROOT / "data" / "pipeline" / "scored_jobs.json"
TRACKER_PATH = ROOT / "data" / "job_tracker.json"

DRY_RUN = "--dry-run" in sys.argv[1:]
TODAY   = date.today().isoformat()

# ── Load inputs ───────────────────────────────────────────────────────────────
if not SCORED_PATH.exists():
    print("ERROR: data/scored_jobs.json not found — run score_jobs.py first")
    sys.exit(1)

# scored_jobs.json is now {"jobs": [...], "apify_upgrades": [...]}
# Backward-compat: also accept bare list (pre-upgrade format)
_scored_raw     = json.loads(SCORED_PATH.read_text())
if isinstance(_scored_raw, list):
    scored          = _scored_raw
    apify_upgrades  = []
else:
    scored          = _scored_raw.get("jobs", [])
    apify_upgrades  = _scored_raw.get("apify_upgrades", [])

raw     = json.loads(TRACKER_PATH.read_text()) if TRACKER_PATH.exists() else {"applications": []}
tracker = raw.get("applications", [])

# ── Backfill: add new columns to existing entries that predate this refactor ──
def _scoring_date_proxy(entry: dict) -> Optional[str]:
    if entry.get("latest_scoring_date"):
        return entry["latest_scoring_date"]
    if entry.get("fit_score") is None:
        return None
    if entry.get("source") == "excel_import":
        return "2026-06-18"
    for h in (entry.get("status_history") or []):
        d = h.get("date") or ""
        if d and len(d) >= 10:
            return d[:10]
    return None


_backfilled = 0
for entry in tracker:
    changed = False
    if "score_exists" not in entry:
        entry["score_exists"] = entry.get("fit_score") is not None
        changed = True
    if "latest_scoring_date" not in entry:
        entry["latest_scoring_date"] = _scoring_date_proxy(entry)
        changed = True
    if "match_exists" not in entry:
        entry["match_exists"] = None
        changed = True
    if "matched_entry_id" not in entry:
        entry["matched_entry_id"] = None
        changed = True
    if changed:
        _backfilled += 1

if _backfilled:
    print(f"[write_tracker] Backfilled {_backfilled} existing entries with new columns")

# ── Build dedup index (exact jd_url) for safety — prevents double-run issues ──
existing_jd_urls = {e.get("jd_url") for e in tracker if e.get("jd_url")}

# ── Next sequential ID ────────────────────────────────────────────────────────
nums    = [int(e["id"][4:]) for e in tracker if e.get("id","").startswith("app_") and e["id"][4:].isdigit()]
next_id = max(nums) + 1 if nums else 1

# ── Helper: find full tracker entry by id ─────────────────────────────────────
def _get_entry_by_id(entry_id: str) -> Optional[dict]:
    return next((e for e in tracker if e.get("id") == entry_id), None)


# ── Helper: apply update-in-place to an existing tracker entry ────────────────
def _update_in_place(existing: dict, jb: dict, sc: Optional[dict],
                     resolved_company: str, resolved_ats: str) -> bool:
    """Update metadata fields on an existing entry without touching status/history/prep.
    Returns True if any field was changed."""
    changed = False

    # Fields that may have fresher data from the new scrape — only overwrite if value changed
    updates: dict = {}
    new_jd_url = (jb.get("job_url") or jb.get("url") or "").strip()
    if new_jd_url and not existing.get("jd_url"):
        updates["jd_url"] = new_jd_url
    if not existing.get("job_id") and (jb.get("job_id") or jb.get("jobId")):
        updates["job_id"] = jb.get("job_id") or jb.get("jobId")

    if sc:
        for field, key in [
            ("salary_stated",           "salary_stated"),
            ("work_mode",               "work_mode"),
            ("experience_req",          "experience_req"),
            ("visa_sponsorship_status", "visa_sponsorship_status"),
            ("actual_hiring_company",   "actual_hiring_company"),
            ("is_contract",             "is_contract"),
            ("is_remote_only",          "is_remote_only"),
            ("is_agency_post",          "is_agency_post"),
            ("is_investment_domain",    "is_investment_domain"),
            ("apply_recommendation",    "apply_recommendation"),
            ("company_sponsor_kb",      "company_sponsor_kb"),
            ("role_focus",              "role_focus"),
        ]:
            val = sc.get(key)
            if val is not None and val != existing.get(field):
                updates[field] = val

        ats = resolved_ats or jb.get("ats_type") or ""
        if ats and ats != "unknown" and ats != existing.get("ats_type"):
            updates["ats_type"] = ats

        # Re-score update: only update fit_score if this is NOT a reused score
        if not jb.get("_score_reused") and sc.get("fit_score") is not None:
            if sc["fit_score"] != existing.get("fit_score"):
                updates["fit_score"]              = sc["fit_score"]
                updates["fit_score_breakdown"]    = sc.get("fit_score_breakdown")
                updates["score_exists"]           = True
                updates["latest_scoring_date"]    = TODAY

    if resolved_company and not existing.get("actual_hiring_company") and sc and sc.get("is_agency_post"):
        updates["actual_hiring_company"] = resolved_company

    for k, v in updates.items():
        existing[k] = v
        changed = True

    # Always refresh match metadata
    existing["match_exists"] = True
    existing["posted_date"]  = jb.get("posted_date") or jb.get("postedDate") or existing.get("posted_date")

    return changed or True  # always note as touched


# ── Helpers ───────────────────────────────────────────────────────────────────
def _url_authoritative_job_id(jb: dict) -> Optional[str]:
    """Return the LinkedIn numeric job_id from the job_url when it conflicts with
    the actor's own 'id' field (Apify sometimes returns a stale or pagination ID
    that doesn't match the LinkedIn job_id embedded in the URL).
    URL wins when both exist and differ."""
    stored_id = str(jb.get("job_id") or jb.get("id") or "").strip() or None
    url = (jb.get("job_url") or jb.get("url") or "").strip()
    m = re.search(r'[/-](\d{9,13})(?:[?/]|$)', url)
    if m:
        url_id = m.group(1)
        if stored_id and url_id != stored_id:
            return url_id  # URL is authoritative
        return url_id
    return stored_id


# ── Build new entry dict ───────────────────────────────────────────────────────
def _build_new_entry(app_id: str, status: str, jb: dict, sc: Optional[dict],
                     resolved_company: str, resolved_ats: str,
                     match_decision: str, matched_id: Optional[str]) -> dict:
    jd_url = (jb.get("job_url") or jb.get("url") or "").strip()
    score_exists = sc is not None and sc.get("fit_score") is not None
    return {
        "id":                     app_id,
        "source":                 {"apify": "Apify", "adzuna": "Adzuna"}.get(
                                      (jb.get("_source") or "").lower(), "job_scout_agent"),
        "job_id":                 _url_authoritative_job_id(jb),
        "company":                resolved_company or jb.get("company_name", ""),
        "agency_name":            jb.get("company_name") if (sc or {}).get("is_agency_post") else None,
        "role":                   jb.get("job_title"),
        "jd_url":                 jd_url or None,
        "career_page_url":        jb.get("career_page_url"),
        "fit_score":              (sc or {}).get("fit_score"),
        "fit_score_breakdown":    (sc or {}).get("fit_score_breakdown"),
        "visa_sponsorship_status":(sc or {}).get("visa_sponsorship_status"),
        "salary_stated":          (sc or {}).get("salary_stated"),
        "salary_raw_linkedin":    jb.get("salary"),
        "salary_meets_threshold": (True  if (sc or {}).get("salary_gate") == "passed"
                                   else None if (sc or {}).get("salary_gate") == "tbc"
                                   else False),
        "location":               jb.get("location"),
        "work_mode":              (sc or {}).get("work_mode"),
        "experience_req":         ((sc or {}).get("experience_req")
                                   or (jb.get("experience_years") or {}).get("display")),
        "ats_type":               resolved_ats,
        "is_easy_apply":          jb.get("is_easy_apply", False),
        "is_contract":            (sc or {}).get("is_contract", False),
        "is_remote_only":         (sc or {}).get("is_remote_only", False),
        "is_agency_post":         (sc or {}).get("is_agency_post", False),
        "actual_hiring_company":  (sc or {}).get("actual_hiring_company"),
        "is_investment_domain":   (sc or {}).get("is_investment_domain", False),
        "posted_date":            jb.get("posted_date"),
        "applied_date":           None,
        "status":                 status,
        "status_history": [
            {"status": status, "date": TODAY, "source": "job_scout_agent"}
        ],
        "tracking_url":           None,
        "resume_path":            None,
        "cover_letter_path":      None,
        "emails_received":        [],
        "notes":                  " | ".join((sc or {}).get("flags", [])) or None,
        "flags":                  (sc or {}).get("flags", []),
        # New dedup/scoring columns
        "match_exists":           jb.get("_match_exists", False),
        "matched_entry_id":       matched_id,          # None unless decision=new_entry
        "score_exists":           score_exists,
        "latest_scoring_date":    TODAY if score_exists else None,
        # Enhancement columns (populated for new scored entries only, null for Stale)
        "apply_recommendation":   (sc or {}).get("apply_recommendation"),
        "company_sponsor_kb":     (sc or {}).get("company_sponsor_kb", "Uncertain") if sc else None,
        "adzuna_salary_stated":   jb.get("salary", "") if jb.get("_source") == "adzuna" else "",
        "role_focus":             (sc or {}).get("role_focus", ""),
        "market":                 jb.get("market", "uk"),
    }


# ── Process scored entries ─────────────────────────────────────────────────────
added      = []
updated    = []
skipped    = []

for entry in scored:
    status = entry.get("status")         # Shortlisted | Review Needed | Stale
    jb     = entry.get("job") or {}
    sc     = entry.get("score") or {}
    jd_url = (jb.get("job_url") or jb.get("url") or "").strip()

    match_decision = entry.get("_match_decision") or jb.get("_match_decision") or "no_match"
    matched_id     = entry.get("_matched_id")     or jb.get("_matched_id")
    resolved_co    = entry.get("_resolved_company", "")
    resolved_ats   = entry.get("_resolved_ats_type", "unknown")

    # ── update_in_place: update existing entry, don't create new ──────────────
    if match_decision == "update_in_place" and matched_id:
        existing = _get_entry_by_id(matched_id)
        if existing:
            if not DRY_RUN:
                _update_in_place(existing, jb, sc if sc else None, resolved_co, resolved_ats)
            updated.append((matched_id, existing.get("company", "?"), existing.get("role", "?")))
            print(f"  ↻ UPDATE {matched_id}: {existing.get('company','')} / {existing.get('role','')}"
                  f" ({'score reused' if jb.get('_score_reused') else 'rescored'})")
            continue
        # matched_id not found in tracker (e.g. was in auto_rejected) — fall through to new entry

    # ── Safety dedup: skip if jd_url already in tracker (double-run protection) ──
    if jd_url and jd_url in existing_jd_urls and match_decision not in ("new_entry",):
        skipped.append((resolved_co or "?", jb.get("job_title", "?"), "jd_url already in tracker"))
        continue

    # ── new_entry or no_match: create a new tracker entry ─────────────────────
    app_id   = f"app_{next_id:03d}"
    next_id += 1

    new_entry = _build_new_entry(
        app_id, status, jb, sc if sc else None,
        resolved_co, resolved_ats,
        match_decision, matched_id,
    )

    if jd_url:
        existing_jd_urls.add(jd_url)

    added.append(new_entry)

# ── Apify data upgrades: patch existing Shortlisted/Review Needed entries ─────
upgraded   = []
PROTECTED  = {"Approved", "Prep Complete", "Applied", "Under Review",
              "Interview Scheduled", "Assessment", "Offer Received"}

for upg in apify_upgrades:
    matched_id = upg.get("_matched_id")
    existing   = _get_entry_by_id(matched_id)
    if not existing:
        print(f"  [UPGRADE] SKIP {matched_id}: not found in tracker")
        continue
    if existing.get("status") in PROTECTED or existing.get("resume_path"):
        print(f"  [UPGRADE] SKIP {matched_id} {existing.get('company','?')} / "
              f"{existing.get('role','?')}: protected (status={existing.get('status')})")
        continue

    changed_fields = []

    # Always replace jd_url with LinkedIn URL — more stable than Adzuna redirect
    new_jd_url = upg.get("jd_url", "")
    if new_jd_url and new_jd_url != existing.get("jd_url"):
        existing["jd_url"] = new_jd_url
        changed_fields.append("jd_url")

    # Fill career_page_url if empty and Apify has a direct ATS URL
    if not existing.get("career_page_url") and upg.get("career_page_url_hint"):
        existing["career_page_url"] = upg["career_page_url_hint"]
        changed_fields.append("career_page_url")

    # Update data fields — prefer non-empty Apify values
    for field in ("salary_stated", "work_mode", "experience_req", "ats_type"):
        val = upg.get(field, "")
        if val and val != existing.get(field):
            existing[field] = val
            changed_fields.append(field)

    # Mark source as Apify so future runs skip this entry for re-upgrade
    existing["source"] = "Apify"

    existing.setdefault("status_history", []).append({
        "event":         "data_upgraded",
        "from_source":   "adzuna",
        "to_source":     "apify",
        "date":          TODAY,
        "fields_updated": changed_fields,
    })

    fields_str = ", ".join(changed_fields) if changed_fields else "no field changes"
    print(f"  [UPGRADE] {matched_id} {existing.get('company','?')} / "
          f"{existing.get('role','?')}: {fields_str}")
    upgraded.append(matched_id)

# ── Write ─────────────────────────────────────────────────────────────────────
by_status = {}
for e in added:
    by_status.setdefault(e["status"], []).append(e)

print(f"\n[write_tracker] scored_jobs.json: {len(scored)} entries")
print(f"[write_tracker] New: {len(added)}  |  Updated in-place: {len(updated)}  |  Skipped: {len(skipped)}")

for status_label in ("Shortlisted", "Review Needed", "Stale"):
    for e in by_status.get(status_label, []):
        score_str = f"[{e['fit_score']}] " if e.get("fit_score") is not None else ""
        mid_str   = f" ← supersedes {e['matched_entry_id']}" if e.get("matched_entry_id") else ""
        print(f"  + {e['id']}: {score_str}{e['company']} — {e['role']} ({status_label}){mid_str}")

if skipped:
    print(f"\n[write_tracker] Skipped:")
    for company, role, reason in skipped:
        print(f"  — {company} / {role}: {reason}")

if not DRY_RUN and (added or updated or upgraded or _backfilled):
    tracker.extend(added)
    raw["applications"] = tracker
    TRACKER_PATH.write_text(json.dumps(raw, indent=2, ensure_ascii=False))
    print(f"\n[write_tracker] ✓ Wrote {len(added)} new + {len(updated)} updated"
          f" + {len(upgraded)} Apify-upgraded (+{_backfilled} backfilled) to job_tracker.json")
elif DRY_RUN:
    print(f"\n[write_tracker] --dry-run: no changes written")
else:
    print(f"\n[write_tracker] Nothing to add or update.")
