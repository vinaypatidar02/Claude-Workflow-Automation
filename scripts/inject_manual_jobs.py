#!/usr/bin/env python3
"""
inject_manual_jobs.py — Inject manually-provided jobs through enrichment + scoring
===================================================================================
Processes data/manual_jobs_input.json through the full post-scraper pipeline:
  enrich → Claude score → write to job_tracker.json

No dedup blocking — all 13 jobs are scored regardless of age or existing entries.

Duplicate handling:
  - Stale match found  → update that entry in-place (new score, new status, history preserved)
  - Any other match    → add as NEW entry alongside the existing one (different job_id = new posting)

Usage:
  python3 scripts/inject_manual_jobs.py
  python3 scripts/inject_manual_jobs.py --dry-run
"""

import ast, json, re, sys, time
from pathlib import Path
from datetime import date
from typing import Optional
from urllib import request as _ureq, error as _uerr

ROOT         = Path(__file__).parent.parent
TRACKER_PATH = ROOT / "data" / "job_tracker.json"
AUTO_REJ_PATH= ROOT / "data" / "auto_rejected.json"
ENV_FILE     = ROOT / ".env"

DRY_RUN = "--dry-run" in sys.argv[1:]
TODAY   = date.today().isoformat()
MODEL   = "claude-haiku-4-5-20251001"

_input_arg = next((sys.argv[i+1] for i, a in enumerate(sys.argv[1:], 1) if a == "--input"), None)
INPUT_PATH = Path(_input_arg) if _input_arg else ROOT / "data" / "manual_jobs_input.json"


# ─────────────────────────────────────────────────────────────────────────────
# ENV
# ─────────────────────────────────────────────────────────────────────────────

def load_env() -> dict:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


# ─────────────────────────────────────────────────────────────────────────────
# ENRICHMENT (mirrored from enrich_jobs.py)
# ─────────────────────────────────────────────────────────────────────────────

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
}

APPLY_KW = re.compile(
    r"\b(apply\b|application\b|submit your|apply now|apply via|"
    r"apply at|apply through|apply using|apply for this role|"
    r"apply for this position|click here to apply)\b",
    re.IGNORECASE
)


def _extract_experience_years(text: str) -> dict:
    if not text:
        return {"found": False, "min_yrs": None, "max_yrs": None, "display": "Not specified"}
    m = re.search(
        r"(\d+)\s*(?:-|to|–|and)\s*(\d+)\s+years?(?:'s?)?\s*(?:of\s+)?(?:relevant\s+)?experience",
        text, re.IGNORECASE)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        return {"found": True, "min_yrs": lo, "max_yrs": hi, "display": f"{lo}–{hi} years"}
    m2 = re.search(
        r"(?:at\s+least\s+|minimum\s+(?:of\s+)?)?(\d+)\+?\s+years?(?:'s?)?\s*(?:of\s+)?(?:relevant\s+)?experience",
        text, re.IGNORECASE)
    if m2:
        yrs = int(m2.group(1))
        return {"found": True, "min_yrs": yrs, "max_yrs": None, "display": f"{yrs}+ years"}
    if re.search(r"\bseveral\s+years\b|\bextensive\s+experience\b", text, re.I):
        return {"found": True, "min_yrs": None, "max_yrs": None, "display": "Several years (not specified)"}
    return {"found": False, "min_yrs": None, "max_yrs": None, "display": "Not specified"}


def enrich_job(job: dict) -> dict:
    desc = job.get("description", "") or ""
    career_page_url = None
    ats_type = "unknown"
    ats_candidates = []
    for name, pattern in ATS_PATTERNS.items():
        for m in re.finditer(pattern, desc, re.IGNORECASE):
            pos = m.start()
            window_start = max(0, pos - 300)
            window = desc[window_start: pos + 300]
            apply_m = APPLY_KW.search(window)
            dist = abs((pos - window_start) - apply_m.start()) if apply_m else 9999
            ats_candidates.append((dist, name, m.group(0)))
    if ats_candidates:
        ats_candidates.sort(key=lambda x: x[0])
        _, ats_type, career_page_url = ats_candidates[0]
    is_easy = bool(re.search(r"\beasy\s*apply\b", desc, re.IGNORECASE))
    exp = _extract_experience_years(desc)
    return {**job, "experience_years": exp, "career_page_url": career_page_url,
            "ats_type": ats_type, "is_easy_apply": is_easy}


# ─────────────────────────────────────────────────────────────────────────────
# SCORING — borrow SCORE_SYSTEM from score_jobs.py via AST (no import side-effects)
# ─────────────────────────────────────────────────────────────────────────────

def _load_score_system() -> str:
    """Extract SCORE_SYSTEM string from score_jobs.py without executing the module."""
    src = (Path(__file__).parent / "score_jobs.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "SCORE_SYSTEM":
                    return ast.literal_eval(node.value)
    raise RuntimeError("Could not extract SCORE_SYSTEM from score_jobs.py")


SCORE_SYSTEM = _load_score_system()


def _build_user_prompt(job: dict) -> str:
    exp_display = (job.get("experience_years") or {}).get("display") or "Not specified"
    desc = (job.get("description") or "")[:5000]
    _SKIP = {"description", "descriptionHtml", "jd_text"}
    raw_meta = {k: v for k, v in job.items() if k not in _SKIP and v is not None}
    raw_meta_str = json.dumps(raw_meta, indent=2)[:2500]
    return f"""Score this job for Vinay Patidar:

Company: {job.get("company_name", "Unknown")}
Title: {job.get("job_title", "Unknown")}
Location: {job.get("location", "Unknown")}
Posted: {job.get("posted_date") or "Unknown"}
Experience Required (extracted from JD): {exp_display}
LinkedIn URL: {job.get("job_url") or ""}

Raw Job Data (all structured fields — use to find salary, work_mode, etc.):
{raw_meta_str}

Job Description:
{desc}"""


def _call_claude(system: str, user_prompt: str, api_key: str) -> str:
    payload = json.dumps({
        "model":      MODEL,
        "max_tokens": 1400,
        "system":     system,
        "messages":   [{"role": "user", "content": user_prompt}],
    }).encode()
    req = _ureq.Request("https://api.anthropic.com/v1/messages", data=payload, method="POST")
    req.add_header("x-api-key",          api_key)
    req.add_header("anthropic-version",  "2023-06-01")
    req.add_header("content-type",       "application/json")
    with _ureq.urlopen(req, timeout=45) as resp:
        result = json.loads(resp.read())
    return result["content"][0]["text"]


def _parse_score(raw: str) -> Optional[dict]:
    raw = raw.strip()
    try: return json.loads(raw)
    except: pass
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try: return json.loads(m.group(0))
        except: pass
    return None


def _apply_hard_overrides(r: dict) -> dict:
    if r.get("visa_sponsorship_status") == "Rejected" and r.get("action") != "reject":
        r["action"] = "reject"
        r.setdefault("flags", []).append("OVERRIDE: visa_sponsorship=Rejected forces reject")
    _salary_stated = r.get("salary_stated") or ""
    _explicit = (bool(_salary_stated)
                 and _salary_stated not in ("", "Not stated", "Not provided")
                 and not _salary_stated.startswith("Not stated (est."))
    # Salary sanity: if the highest parsed number in salary_stated is >= £85k,
    # the upper bound passes — override Claude's salary_gate=failed.
    if r.get("salary_gate") == "failed" and _explicit:
        _sal_nums_raw = re.findall(r"\d[\d,]*", _salary_stated.replace(",", ""))
        _sal_nums = []
        for n in _sal_nums_raw:
            try: _sal_nums.append(int(n))
            except: pass
        if _sal_nums:
            _upper = max(_sal_nums)
            if _upper < 1000:
                _upper *= 1000
            if _upper >= 85000:
                r["salary_gate"] = "tbc"
                r.setdefault("flags", []).append(
                    f"OVERRIDE: salary upper bound £{_upper:,} >= £85k — gate cleared")
    if r.get("salary_gate") == "failed" and _explicit and r.get("action") != "reject":
        r["action"] = "reject"
        r.setdefault("flags", []).append("OVERRIDE: salary_gate=failed (stated) forces reject")
    if r.get("fit_score", 100) < 60 and r.get("action") != "reject":
        r["action"] = "reject"
        r.setdefault("flags", []).append("OVERRIDE: fit_score<60 forces reject")
    if (r.get("fit_score", 0) >= 75
            and r.get("action") == "review"
            and r.get("visa_sponsorship_status") != "Rejected"
            and r.get("salary_gate") != "failed"
            and not r.get("is_contract")
            and not r.get("is_remote_only")):
        r["action"] = "shortlist"
        r.setdefault("flags", []).append("OVERRIDE: fit_score>=75 with no blockers → shortlist")

    # Gate 5: Correct action when Claude rejected for salary/visa that our overrides cleared.
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
    return r


def _canonical_rejection_reason(r: dict) -> str:
    """Hierarchy: Remote > Contract > Visa Denied > Salary Failed > Score/Fit."""
    if r.get("is_remote_only"):
        return "Remote-only role — physical UK presence required for visa relocation"
    if r.get("is_contract"):
        return "Contract role — Skilled Worker Visa requires permanent employment"
    if r.get("visa_sponsorship_status") == "Rejected":
        return "Visa sponsorship explicitly denied in job description"
    if (r.get("salary_gate") == "failed"
            and r.get("salary_stated") not in ("", "Not stated", "Not provided", None)):
        return f"Salary below £85k threshold (stated: {r.get('salary_stated', '')})"
    claude_reason = r.get("rejection_reason") or "Domain/fit mismatch"
    return f"Fit score {r.get('fit_score', 0)}/100 — {claude_reason}"


# ─────────────────────────────────────────────────────────────────────────────
# TRACKER MATCHING — decide update-in-place vs new entry
# ─────────────────────────────────────────────────────────────────────────────

def _linkedin_job_id(url: str) -> Optional[str]:
    """Extract numeric LinkedIn job ID from URL."""
    m = re.search(r'/jobs/view/(\d+)', url or "")
    return m.group(1) if m else None


def _fuzzy_match(a: str, b: str, min_words: int = 1) -> bool:
    """True if a and b share at least min_words common words (case-insensitive)."""
    if not a or not b:
        return False
    words_a = set(re.sub(r'[^a-z0-9 ]', '', a.lower()).split())
    words_b = set(re.sub(r'[^a-z0-9 ]', '', b.lower()).split())
    return len(words_a & words_b) >= min_words


def find_stale_match(job: dict, tracker: list) -> Optional[dict]:
    """
    Returns an existing tracker entry ONLY if it is Stale AND matches this job.
    Match = same LinkedIn job_id OR (company fuzzy + role fuzzy).
    Used to decide in-place update vs new entry.
    """
    jd_url  = job.get("job_url") or ""
    company = (job.get("company_name") or "").lower().strip()
    title   = (job.get("job_title") or "").lower().strip()
    new_job_id = _linkedin_job_id(jd_url)

    for entry in tracker:
        if entry.get("status") != "Stale":
            continue
        e_url    = entry.get("jd_url") or ""
        e_co     = (entry.get("company") or "").lower().strip()
        e_role   = (entry.get("role") or "").lower().strip()
        e_job_id = _linkedin_job_id(e_url)

        # Signal 1: exact URL match
        if jd_url and e_url and jd_url == e_url:
            return entry
        # Signal 2: same LinkedIn job_id
        if new_job_id and e_job_id and new_job_id == e_job_id:
            return entry
        # Signal 3: company + role fuzzy (≥1 company word + ≥2 role words)
        if _fuzzy_match(company, e_co, 1) and _fuzzy_match(title, e_role, 2):
            return entry

    return None


# ─────────────────────────────────────────────────────────────────────────────
# ACTION → STATUS
# ─────────────────────────────────────────────────────────────────────────────

ACTION_TO_STATUS = {
    "shortlist": "Shortlisted",
    "review":    "Review Needed",
    "reject":    "Auto-Rejected",
}


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    if not INPUT_PATH.exists():
        print(f"ERROR: {INPUT_PATH} not found.")
        sys.exit(1)

    jobs = json.loads(INPUT_PATH.read_text())
    env  = load_env()
    api_key = env.get("ANTHROPIC_API_KEY") or env.get("CLAUDE_API_KEY") or ""
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not found in .env")
        sys.exit(1)

    tracker_raw = json.loads(TRACKER_PATH.read_text()) if TRACKER_PATH.exists() else {"applications": []}
    tracker     = tracker_raw.get("applications", [])

    auto_rej_raw = {"auto_rejected": []}
    if AUTO_REJ_PATH.exists():
        try: auto_rej_raw = json.loads(AUTO_REJ_PATH.read_text())
        except: pass
    auto_rejected = auto_rej_raw.get("auto_rejected", [])

    # Next available app_XXX ID
    nums    = [int(e["id"][4:]) for e in tracker if e.get("id","").startswith("app_") and e["id"][4:].isdigit()]
    next_id = max(nums) + 1 if nums else 1

    # Next auto_rejected ID
    rej_nums    = [int(e["id"][4:]) for e in auto_rejected if e.get("id","").startswith("rej_") and e["id"][4:].isdigit()]
    next_rej_id = max(rej_nums) + 1 if rej_nums else 1

    print(f"\n[inject] ─────────────────────────────────────────────")
    print(f"[inject] Input:   {len(jobs)} jobs from {INPUT_PATH.name}")
    print(f"[inject] Tracker: {len(tracker)} existing entries")
    print(f"[inject] Model:   {MODEL}")
    print(f"[inject] Dry run: {DRY_RUN}")
    print(f"[inject] ─────────────────────────────────────────────\n")

    updated_stale  = []   # (app_id, company, role, old_status, new_status, fit_score)
    new_entries    = []   # new tracker entries to append
    auto_rej_new   = []   # new auto_rejected entries to append

    for i, raw_job in enumerate(jobs):
        company = raw_job.get("company_name", "?")
        title   = raw_job.get("job_title", "?")
        print(f"[inject] {i+1}/{len(jobs)}: {title} @ {company}")

        # Enrich
        job = enrich_job(raw_job)

        # Score via Claude
        user_prompt = _build_user_prompt(job)
        try:
            raw_response = _call_claude(SCORE_SYSTEM, user_prompt, api_key)
            score = _parse_score(raw_response)
            if not score:
                print(f"  ✗ Parse failed — skipping")
                continue
            score = _apply_hard_overrides(score)
        except Exception as e:
            print(f"  ✗ Claude API error: {e}")
            continue

        action    = score.get("action", "reject")
        status    = ACTION_TO_STATUS.get(action, "Review Needed")
        fit_score = score.get("fit_score")
        visa      = score.get("visa_sponsorship_status", "Unconfirmed")
        salary    = score.get("salary_stated", "Not stated")
        work_mode = score.get("work_mode", "Unknown")
        flags     = score.get("flags", [])

        score_str = f"[{fit_score}]" if fit_score is not None else ""
        flag_str  = f"  flags: {', '.join(flags[:2])}" if flags else ""
        print(f"  → {status} {score_str}  visa={visa}  salary={salary}")
        if flag_str: print(f"  {flag_str}")

        # Resolved company (for agency posts)
        resolved_company = score.get("actual_hiring_company") or company
        resolved_ats     = score.get("ats_type_from_jd") or job.get("ats_type") or "unknown"
        jd_url           = (job.get("job_url") or "").strip()

        if status == "Auto-Rejected":
            # Write to auto_rejected.json; do NOT add to tracker
            rej_id = f"rej_{next_rej_id:04d}"
            next_rej_id += 1
            rej_entry = {
                "id":                     rej_id,
                "job_id":                 _linkedin_job_id(jd_url),
                "jd_url":                 jd_url or None,
                "company":                resolved_company,
                "agency_name":            company if score.get("is_agency_post") else None,
                "role":                   title,
                "location":               job.get("location"),
                "fit_score":              fit_score,
                "rejection_reason":       _canonical_rejection_reason(score),
                "salary_stated":          salary,
                "visa_hint":              visa,
                "work_mode":              work_mode,
                "is_contract":            score.get("is_contract", False),
                "is_remote_only":         score.get("is_remote_only", False),
                "scout_run_date":         TODAY,
                "source":                 "manual_inject",
                "flags":                  flags,
            }
            auto_rej_new.append(rej_entry)
            print(f"  → auto_rejected.json  ({_canonical_rejection_reason(score)[:60]})")
            time.sleep(0.5)
            continue

        # Check for Stale match (for in-place update)
        stale_match = find_stale_match(job, tracker)

        if stale_match:
            # Update existing Stale entry in-place
            app_id = stale_match["id"]
            old_status = stale_match.get("status", "Stale")
            stale_match.update({
                "fit_score":              fit_score,
                "fit_score_breakdown":    score.get("fit_score_breakdown"),
                "visa_sponsorship_status":visa,
                "salary_stated":          salary,
                "salary_raw_linkedin":    job.get("salary"),
                "salary_meets_threshold": (True  if score.get("salary_gate") == "passed"
                                           else None if score.get("salary_gate") == "tbc"
                                           else False),
                "salary_estimate":        score.get("salary_estimate"),
                "salary_estimate_confidence": score.get("salary_estimate_confidence"),
                "work_mode":              work_mode,
                "experience_req":         (score.get("experience_req")
                                           or (job.get("experience_years") or {}).get("display")),
                "ats_type":               resolved_ats,
                "is_contract":            score.get("is_contract", False),
                "is_remote_only":         score.get("is_remote_only", False),
                "is_agency_post":         score.get("is_agency_post", False),
                "actual_hiring_company":  score.get("actual_hiring_company"),
                "is_investment_domain":   score.get("is_investment_domain", False),
                "posted_date":            job.get("posted_date") or stale_match.get("posted_date"),
                "status":                 status,
                "resume_path":            None,    # reset so prep can run
                "cover_letter_path":      None,
                "notes":                  " | ".join(flags) if flags else stale_match.get("notes"),
                "flags":                  flags,
            })
            # Only update jd_url if the new one is different/better
            if jd_url and not stale_match.get("jd_url"):
                stale_match["jd_url"] = jd_url
            stale_match.setdefault("status_history", []).append({
                "status": status,
                "date":   TODAY,
                "source": "manual_inject",
                "reason": f"Re-scored via inject_manual_jobs.py — {old_status} → {status}",
            })
            updated_stale.append((app_id, resolved_company, title, old_status, status, fit_score))
            print(f"  → UPDATED {app_id} in-place ({old_status} → {status})")

        else:
            # New entry
            app_id  = f"app_{next_id:03d}"
            next_id += 1
            new_entry = {
                "id":                     app_id,
                "source":                 "manual_inject",
                "job_id":                 _linkedin_job_id(jd_url),
                "company":                resolved_company,
                "agency_name":            company if score.get("is_agency_post") else None,
                "role":                   title,
                "jd_url":                 jd_url or None,
                "career_page_url":        job.get("career_page_url"),
                "fit_score":              fit_score,
                "fit_score_breakdown":    score.get("fit_score_breakdown"),
                "visa_sponsorship_status":visa,
                "salary_stated":          salary,
                "salary_raw_linkedin":    job.get("salary"),
                "salary_meets_threshold": (True  if score.get("salary_gate") == "passed"
                                           else None if score.get("salary_gate") == "tbc"
                                           else False),
                "salary_estimate":        score.get("salary_estimate"),
                "salary_estimate_confidence": score.get("salary_estimate_confidence"),
                "location":               job.get("location"),
                "work_mode":              work_mode,
                "experience_req":         (score.get("experience_req")
                                           or (job.get("experience_years") or {}).get("display")),
                "ats_type":               resolved_ats,
                "is_easy_apply":          job.get("is_easy_apply", False),
                "is_contract":            score.get("is_contract", False),
                "is_remote_only":         score.get("is_remote_only", False),
                "is_agency_post":         score.get("is_agency_post", False),
                "actual_hiring_company":  score.get("actual_hiring_company"),
                "is_investment_domain":   score.get("is_investment_domain", False),
                "posted_date":            job.get("posted_date"),
                "applied_date":           None,
                "status":                 status,
                "status_history": [
                    {"status": status, "date": TODAY, "source": "manual_inject"}
                ],
                "tracking_url":           None,
                "resume_path":            None,
                "cover_letter_path":      None,
                "emails_received":        [],
                "notes":                  " | ".join(flags) if flags else None,
                "flags":                  flags,
            }
            new_entries.append(new_entry)
            print(f"  → NEW {app_id}")

        time.sleep(0.5)   # rate-limit courtesy pause

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n[inject] ══════════════════════════════════════════")
    print(f"  Total processed:  {len(jobs)}")
    print(f"  Stale updated:    {len(updated_stale)}")
    print(f"  New tracker:      {len(new_entries)}")
    print(f"  Auto-rejected:    {len(auto_rej_new)}")
    print(f"[inject] ══════════════════════════════════════════")

    if updated_stale:
        print("\n  Updated Stale entries:")
        for app_id, co, role, old, new, score in updated_stale:
            print(f"    {app_id}: [{score}] {co} / {role}  ({old} → {new})")
    if new_entries:
        print("\n  New tracker entries:")
        for e in new_entries:
            sc_str = f"[{e['fit_score']}] " if e.get("fit_score") is not None else ""
            print(f"    {e['id']}: {sc_str}{e['company']} / {e['role']}  ({e['status']})")
    if auto_rej_new:
        print("\n  Auto-rejected:")
        for e in auto_rej_new:
            print(f"    {e['id']}: [{e.get('fit_score')}] {e['company']} / {e['role']}")

    # ── Write ─────────────────────────────────────────────────────────────────
    if DRY_RUN:
        print("\n[inject] --dry-run: no files written.")
        return

    if new_entries or updated_stale:
        tracker.extend(new_entries)
        tracker_raw["applications"] = tracker
        TRACKER_PATH.write_text(json.dumps(tracker_raw, indent=2, ensure_ascii=False))
        print(f"\n[inject] ✓ Wrote {len(new_entries)} new + {len(updated_stale)} updated → job_tracker.json")

    if auto_rej_new:
        auto_rejected.extend(auto_rej_new)
        auto_rej_raw["auto_rejected"] = auto_rejected
        AUTO_REJ_PATH.write_text(json.dumps(auto_rej_raw, indent=2, ensure_ascii=False))
        print(f"[inject] ✓ Wrote {len(auto_rej_new)} rejected → auto_rejected.json")

    print("\n[inject] Next: python3 scripts/sheets_sync.py push")


if __name__ == "__main__":
    main()
