#!/usr/bin/env python3
"""
eval_scout.py — Quality evaluator for scout run output.

Runs after write_tracker.py, before sheets_sync.py push.
Checks scored_jobs.json for quality issues, applies ephemeral fixes,
and describes root-cause edits for the agent to apply inline.

Checks (all deterministic, zero API cost except SC-S1):
  SC1 — Title leakage: Pass 1 missed a title it should have rejected
  SC2 — role_focus misclassification: "mixed" entries lacking SQL/Python
  SC3 — Visa & salary gaps: operational alerts on high-risk entries
  SC4 — Duplicate slip-through: fuzzy dedup cross-check
  SC-S1 — Semantic title review: ONE Haiku call per run (skip with --skip-semantic)

Usage:
  python3 scripts/eval_scout.py
  python3 scripts/eval_scout.py --apply-ephemeral
  python3 scripts/eval_scout.py --skip-semantic
"""

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from urllib import error as _uerr
from urllib import request as _ureq

ROOT          = Path(__file__).parent.parent
SCORED_PATH   = ROOT / "data" / "pipeline" / "scored_jobs.json"
TRACKER_PATH  = ROOT / "data" / "job_tracker.json"
AUTO_REJ_PATH = ROOT / "data" / "auto_rejected.json"

sys.path.insert(0, str(Path(__file__).parent))
from eval_base import (
    ENV_FILE, Issue, company_fuzzy_match,
    load_env, print_report, role_word_overlap,
)

HAIKU = "claude-haiku-4-5-20251001"

# ─────────────────────────────────────────────────────────────────────────────
# TITLE LEAKAGE PATTERNS  (SC1)
# These are patterns that TITLE_REJECT_CONTAINS in score_jobs.py should catch
# but may not yet contain.  Detection here feeds systemic fix suggestions.
# ─────────────────────────────────────────────────────────────────────────────

# Patterns that already exist in TITLE_REJECT_CONTAINS (don't suggest twice)
_KNOWN_REJECT_PATTERNS = {
    "data engineer", "software engineer", "machine learning engineer", "ml engineer",
    "devops engineer", "cloud engineer", "platform engineer", "site reliability",
    "hr analyst", "people analyst", "payroll analyst", "payroll manager",
    "paid social", "seo analyst", "seo manager", "digital marketing analyst",
    "cro specialist", "cro consultant", "financial analyst", "finance analyst",
    "solutions architect", "data architect", "bi developer", "etl developer",
    "data science", "scientist", "graduate analyst",
    "revenue operations manager", "revenue operations lead",
    "business operations manager", "business operations lead",
    "talent acquisition", "talent partner", "people operations", "people partner",
    "commercial transformation",
}

# New patterns not yet in the reject list — flag if title matches these
_SUSPECT_TITLE_PATTERNS = [
    # Non-analytics operations/strategy (no core tooling)
    (r"\boperations\s+manager\b(?!.*analytic)", "ops_manager",
     "Contains 'operations manager' without 'analytics' — likely insights_strategy territory"),
    (r"\bbusiness\s+ops\b", "biz_ops",
     "Contains 'business ops' — likely BizOps without analytics tooling"),
    (r"\brevenue\s+ops\b", "rev_ops",
     "Contains 'revenue ops' — Salesforce/CRM ops domain, not analytics"),
    # Engineering roles that sneaked through
    (r"\banalytics\s+engineer\b", "analytics_eng",
     "Analytics Engineer — dbt/data modelling domain, not analytics management"),
    # Clearly too junior
    (r"\bjunior\b|\bgraduate\b|\bentry.level\b", "too_junior",
     "Junior/graduate level — below 8+ year Lead/Manager profile"),
    # Pure marketing roles
    (r"\bppc\b|\bpaid\s+media\b|\bperformance\s+marketing\b", "paid_media",
     "Paid media / PPC role — not analytics management"),
]

# JD text: analytics tooling signals (absence = likely insights_strategy)
_ANALYTICS_TOOLING_RE = re.compile(
    r"\b(sql|python|tableau|looker|bigquery|redshift|powerbi|power\s+bi|"
    r"databricks|snowflake|excel(?!\s+at)|r\b)\b",
    re.IGNORECASE,
)

# Salary patterns for SC3 salary gap check
_SALARY_RE = re.compile(
    r"£[\d,]+\s*[-–to]+\s*£[\d,]+"      # £80,000 – £100,000
    r"|up to £[\d,]+"                     # up to £90k
    r"|£[\d,]+\s*(?:per annum|pa\b|k\b)" # £90k / £90,000 pa
    r"|(\bOTE\b|\bbase\b).*?£[\d,]+",    # OTE / base £...
    re.IGNORECASE,
)


# ─────────────────────────────────────────────────────────────────────────────
# DATA LOADERS
# ─────────────────────────────────────────────────────────────────────────────

def _load_scored() -> list[dict]:
    if not SCORED_PATH.exists():
        print(f"[eval_scout] scored_jobs.json not found at {SCORED_PATH}")
        return []
    raw = json.loads(SCORED_PATH.read_text())
    return raw.get("jobs", [])


def _load_tracker() -> list[dict]:
    if not TRACKER_PATH.exists():
        return []
    raw = json.loads(TRACKER_PATH.read_text())
    return raw.get("applications", [])


def _active_scored(jobs: list[dict]) -> list[dict]:
    """Return only Shortlisted and Review Needed entries."""
    return [j for j in jobs if j.get("status") in ("Shortlisted", "Review Needed")]


# ─────────────────────────────────────────────────────────────────────────────
# SC1 — TITLE LEAKAGE
# ─────────────────────────────────────────────────────────────────────────────

def check_sc1_title_leakage(active: list[dict]) -> list[Issue]:
    issues = []
    for entry in active:
        job   = entry.get("job", {})
        title = job.get("job_title", "").lower().strip()
        company = job.get("company_name", "Unknown")
        status  = entry.get("status", "")

        # Skip if already in the known reject list (Pass 1 should have caught it — SC1 would be redundant)
        if any(p in title for p in _KNOWN_REJECT_PATTERNS):
            continue

        for pattern, slug, reason in _SUSPECT_TITLE_PATTERNS:
            if re.search(pattern, title):
                # Extra guard: if JD text has clear analytics tooling, let it through
                jd = job.get("description", "") or job.get("descriptionText", "")
                if slug == "analytics_eng" and _ANALYTICS_TOOLING_RE.search(jd):
                    # Analytics engineer with SQL/Python still in JD → borderline, not a clear reject
                    continue

                issues.append(Issue(
                    check_id="SC1",
                    severity="high",
                    title="Title leakage — Pass 1 missed a suspect pattern",
                    evidence=(
                        f"Title: \"{job.get('job_title', '')}\" @ {company} [{status}]\n"
                        f"Pattern: {reason}"
                    ),
                    ephemeral_fix={
                        "description": f"Demote from {status} → Review Needed in tracker",
                        "job_url": job.get("job_url", ""),
                        "field": "status",
                        "value": "Review Needed",
                        "company": company,
                    },
                    systemic_fix={
                        "file": "scripts/score_jobs.py",
                        "description": (
                            f"Add exact title text to TITLE_REJECT_CONTAINS list "
                            f"(search for 'TITLE_REJECT_CONTAINS' in the file). "
                            f"Pattern to add: '{job.get('job_title','').lower()}' "
                            f"or a shorter slug covering the pattern ({slug})."
                        ),
                    },
                ))
                break  # one issue per entry

    return issues


# ─────────────────────────────────────────────────────────────────────────────
# SC2 — ROLE_FOCUS MISCLASSIFICATION
# ─────────────────────────────────────────────────────────────────────────────

def check_sc2_role_focus(active: list[dict]) -> list[Issue]:
    """Flag Shortlisted entries where role_focus='mixed' but JD has no analytics tooling."""
    issues = []
    for entry in active:
        if entry.get("status") != "Shortlisted":
            continue
        score = entry.get("score", {})
        role_focus = score.get("role_focus", "")
        if role_focus not in ("mixed", "insights_strategy"):
            continue

        job = entry.get("job", {})
        jd  = job.get("description", "") or job.get("descriptionText", "")
        if _ANALYTICS_TOOLING_RE.search(jd):
            continue  # Has tooling — fine as Shortlisted

        company = job.get("company_name", "Unknown")
        title   = job.get("job_title", "")
        issues.append(Issue(
            check_id="SC2",
            severity="medium",
            title="role_focus misclassification — no analytics tooling in JD",
            evidence=(
                f"Title: \"{title}\" @ {company} [Shortlisted, role_focus={role_focus!r}]\n"
                f"JD has no SQL/Python/Tableau/Looker/BigQuery in required skills."
            ),
            ephemeral_fix={
                "description": f"Demote {company} / {title} from Shortlisted → Review Needed",
                "job_url": job.get("job_url", ""),
                "field": "status",
                "value": "Review Needed",
                "company": company,
            },
            systemic_fix={
                "file": "scripts/score_jobs.py",
                "description": (
                    "In _apply_hard_overrides(): add a gate after the existing "
                    "role_focus checks — if role_focus == 'mixed' and no SQL/Python/"
                    "Tableau/Looker/BigQuery found in JD required skills, force "
                    "role_focus = 'insights_strategy' (which then auto-rejects it). "
                    f"First seen: {company} / {title}"
                ),
            },
        ))
    return issues


# ─────────────────────────────────────────────────────────────────────────────
# SC3 — VISA & SALARY GAPS (operational alerts)
# ─────────────────────────────────────────────────────────────────────────────

def check_sc3_visa_salary(active: list[dict]) -> list[Issue]:
    issues = []
    for entry in active:
        score   = entry.get("score", {})
        job     = entry.get("job", {})
        company = job.get("company_name", entry.get("_resolved_company", "Unknown"))
        title   = job.get("job_title", "")

        # Visa gap: both signals say no sponsorship
        visa   = score.get("visa_sponsorship_status", "")
        kb     = score.get("company_sponsor_kb", "")
        if visa == "Unconfirmed" and "not a known sponsor" in kb.lower():
            issues.append(Issue(
                check_id="SC3",
                severity="medium",
                title="Visa blindspot — high-risk entry",
                evidence=(
                    f"Company: {company}  Title: {title}\n"
                    f"visa_sponsorship_status=Unconfirmed AND company_sponsor_kb='{kb}'\n"
                    f"Neither JD text nor KB confirms sponsorship."
                ),
                ephemeral_fix={
                    "description": f"Set apply_recommendation=Skip for {company} / {title}",
                    "job_url": job.get("job_url", ""),
                    "field": "apply_recommendation",
                    "value": "Skip",
                    "company": company,
                },
                systemic_fix=None,
            ))

        # Salary gap: salary_gate=tbc but JD text contains a detectable salary
        if score.get("salary_gate") == "tbc":
            jd = job.get("description", "") or job.get("descriptionText", "")
            m  = _SALARY_RE.search(jd)
            if m:
                found = m.group(0)[:60].replace("\n", " ")
                issues.append(Issue(
                    check_id="SC3",
                    severity="low",
                    title="Salary gap — salary present in JD but not extracted",
                    evidence=(
                        f"Company: {company}  salary_gate=tbc\n"
                        f"Salary text found in JD: \"{found}\"\n"
                        f"Update salary_stated manually in Google Sheet Col F."
                    ),
                    ephemeral_fix=None,
                    systemic_fix={
                        "file": "scripts/enrich_jobs.py",
                        "description": (
                            f"Search for '_SALARY_RE' or 'SALARY_PATTERNS' in enrich_jobs.py "
                            f"and add a new regex alternative to catch: \"{found[:50]}\""
                        ),
                    },
                ))

    return issues


# ─────────────────────────────────────────────────────────────────────────────
# SC4 — DUPLICATE SLIP-THROUGH
# ─────────────────────────────────────────────────────────────────────────────

def check_sc4_duplicates(active: list[dict], tracker: list[dict]) -> list[Issue]:
    """Cross-check new scored entries against ALL tracker entries for fuzzy duplicates."""
    issues = []
    # Only check entries NOT already in the tracker (fresh from this run)
    tracker_urls = {a.get("jd_url", "") for a in tracker if a.get("jd_url")}

    for entry in active:
        job = entry.get("job", {})
        jd_url  = job.get("job_url", "")
        if jd_url in tracker_urls:
            continue  # already in tracker (was just written) — dedup already handled this

        company = job.get("company_name", "")
        role    = job.get("job_title", "")

        candidates = []
        for app in tracker:
            if not company_fuzzy_match(company, app.get("company", "")):
                continue
            overlap = role_word_overlap(role, app.get("role", ""))
            if overlap >= 2:
                candidates.append(app)

        for cand in candidates:
            issues.append(Issue(
                check_id="SC4",
                severity="low",
                title="Potential duplicate slip-through",
                evidence=(
                    f"New:      \"{role}\" @ {company}\n"
                    f"Existing: \"{cand.get('role')}\" @ {cand.get('company')} "
                    f"[{cand.get('id')}] status={cand.get('status')}"
                ),
                ephemeral_fix={
                    "description": (
                        f"Confirm manually — set status=Duplicate in Sheet for "
                        f"\"{role}\" @ {company} if it is a re-post"
                    ),
                    "job_url": jd_url,
                    "field": "status",
                    "value": "Duplicate",
                    "company": company,
                },
                systemic_fix={
                    "file": "scripts/score_jobs.py",
                    "description": (
                        f"In _check_duplicate(): review why {company!r} / {role!r} "
                        f"was not caught. Check if the existing entry's jd_url is set "
                        f"and whether the fuzzy match threshold covers this case. "
                        f"Tighten signal 3 (fuzzy company+role) if needed."
                    ),
                },
            ))
            break  # one issue per new entry

    return issues


# ─────────────────────────────────────────────────────────────────────────────
# SC-S1 — SEMANTIC TITLE REVIEW  (ONE Haiku call per run)
# ─────────────────────────────────────────────────────────────────────────────

_SEMANTIC_SYSTEM = (
    "You are a job search quality reviewer for Vinay Patidar — 8+ years Lead/Manager-level "
    "analytics professional targeting UK roles (Analytics Manager, Analytics Lead, Lead Business "
    "Analyst, Lead Data Analyst, or equivalent senior analytics management titles).\n\n"
    "Review a list of shortlisted job titles. Flag any that look clearly wrong — e.g. obviously "
    "too junior, wrong domain (pure engineering, finance, HR, marketing ops), or clearly a "
    "non-analytics management role — for an 8-year Lead/Manager analytics profile.\n\n"
    "Be conservative: only flag titles you are confident are mismatches. Do NOT flag borderline "
    "or ambiguous titles. Respond ONLY with JSON: "
    "{\"flagged\": [{\"title\": \"...\", \"company\": \"...\", \"reason\": \"one line\"}]}. "
    "Empty list if nothing is wrong."
)


def check_sc_s1_semantic(active: list[dict], api_key: str) -> list[Issue]:
    """ONE Haiku API call to catch ambiguous title mismatches not caught by SC1-SC4."""
    shortlisted = [e for e in active if e.get("status") == "Shortlisted"]
    if not shortlisted:
        return []

    title_list = "\n".join(
        f"- {e['job'].get('job_title', '')} @ {e['job'].get('company_name', '')} "
        f"(fit_score={e.get('score', {}).get('fit_score', '?')})"
        for e in shortlisted
    )
    user_prompt = f"Shortlisted titles from this scout run:\n{title_list}"

    try:
        payload = json.dumps({
            "model": HAIKU,
            "max_tokens": 500,
            "system": _SEMANTIC_SYSTEM,
            "messages": [{"role": "user", "content": user_prompt}],
        }).encode()
        req = _ureq.Request("https://api.anthropic.com/v1/messages", data=payload, method="POST")
        req.add_header("x-api-key",         api_key)
        req.add_header("anthropic-version", "2023-06-01")
        req.add_header("content-type",      "application/json")
        with _ureq.urlopen(req, timeout=30) as resp:
            raw = json.loads(resp.read())["content"][0]["text"].strip()
    except Exception as e:
        print(f"[eval_scout] SC-S1 API call failed: {e}")
        return []

    try:
        data  = json.loads(raw)
        flagged = data.get("flagged", [])
    except Exception:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        try:
            flagged = json.loads(m.group(0)).get("flagged", []) if m else []
        except Exception:
            flagged = []

    issues = []
    for item in flagged:
        title   = item.get("title", "")
        company = item.get("company", "")
        reason  = item.get("reason", "")
        # Find the matching entry to get the job_url
        job_url = ""
        for e in shortlisted:
            if (title.lower() in e["job"].get("job_title", "").lower() or
                    e["job"].get("job_title", "").lower() in title.lower()):
                if company.lower() in e["job"].get("company_name", "").lower():
                    job_url = e["job"].get("job_url", "")
                    break

        issues.append(Issue(
            check_id="SC-S1",
            severity="medium",
            title="Semantic title mismatch (Haiku review)",
            evidence=f"Title: \"{title}\" @ {company}\nReason: {reason}",
            ephemeral_fix={
                "description": f"Demote \"{title}\" @ {company} from Shortlisted → Review Needed",
                "job_url": job_url,
                "field": "status",
                "value": "Review Needed",
                "company": company,
            },
            systemic_fix={
                "file": "scripts/score_jobs.py",
                "description": (
                    f"Add a keyword from \"{title.lower()}\" to TITLE_REJECT_CONTAINS "
                    f"(search for 'TITLE_REJECT_CONTAINS' in score_jobs.py to find the list). "
                    f"Choose the shortest phrase that uniquely identifies this title type."
                ),
            },
        ))

    return issues


# ─────────────────────────────────────────────────────────────────────────────
# EPHEMERAL FIX APPLIER
# ─────────────────────────────────────────────────────────────────────────────

def apply_ephemeral_fixes(issues: list[Issue]) -> None:
    """Apply ephemeral fixes to job_tracker.json in-place."""
    fixable = [i for i in issues if i.ephemeral_fix and i.ephemeral_fix.get("field") in ("status", "apply_recommendation")]
    if not fixable:
        print("[eval_scout] No ephemeral fixes to apply.")
        return

    if not TRACKER_PATH.exists():
        print("[eval_scout] tracker not found — skipping ephemeral fixes")
        return

    raw = json.loads(TRACKER_PATH.read_text())
    apps = raw.get("applications", [])
    changed = 0
    today = date.today().isoformat()

    for issue in fixable:
        fix = issue.ephemeral_fix
        job_url = fix.get("job_url", "")
        company = fix.get("company", "")
        field   = fix.get("field", "")
        value   = fix.get("value", "")

        for app in apps:
            if (app.get("jd_url") == job_url) or \
               (company and company.lower() in (app.get("company") or "").lower()):
                if app.get(field) != value:
                    old = app.get(field)
                    app[field] = value
                    app.setdefault("status_history", []).append({
                        "status": value if field == "status" else app.get("status", ""),
                        "date": today,
                        "source": "eval_scout_ephemeral",
                        "reason": f"eval_scout {issue.check_id}: {issue.title}",
                    })
                    print(f"  [fix] {app.get('company')} / {app.get('role')}: {field} {old!r} → {value!r}")
                    changed += 1
                break

    raw["applications"] = apps
    TRACKER_PATH.write_text(json.dumps(raw, indent=2, ensure_ascii=False))
    print(f"[eval_scout] Applied {changed} ephemeral fix(es) to tracker.")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Scout run quality evaluator")
    parser.add_argument("--apply-ephemeral", action="store_true",
                        help="Apply ephemeral fixes to job_tracker.json")
    parser.add_argument("--skip-semantic", action="store_true",
                        help="Skip SC-S1 semantic Haiku call (zero API cost)")
    args = parser.parse_args()

    jobs    = _load_scored()
    tracker = _load_tracker()
    active  = _active_scored(jobs)

    if not jobs:
        print("[eval_scout] No scored_jobs.json found — run scout first.")
        sys.exit(0)

    print(f"[eval_scout] Evaluating {len(active)} active entries "
          f"({len([j for j in active if j.get('status')=='Shortlisted'])} shortlisted, "
          f"{len([j for j in active if j.get('status')=='Review Needed'])} review)")

    issues: list[Issue] = []
    issues += check_sc1_title_leakage(active)
    issues += check_sc2_role_focus(active)
    issues += check_sc3_visa_salary(active)
    issues += check_sc4_duplicates(active, tracker)

    if not args.skip_semantic:
        env = load_env()
        api_key = env.get("ANTHROPIC_API_KEY", "")
        if api_key:
            print("[eval_scout] SC-S1: running semantic title review (1 Haiku call)...")
            issues += check_sc_s1_semantic(active, api_key)
        else:
            print("[eval_scout] SC-S1: ANTHROPIC_API_KEY not found — skipping semantic check")

    print_report(
        issues,
        header=f"Scout Run {date.today().isoformat()}",
        checks_desc="SC1 (title leakage) · SC2 (role_focus) · SC3 (visa/salary) · SC4 (dedup) · SC-S1 (semantic)",
    )

    if issues and args.apply_ephemeral:
        apply_ephemeral_fixes(issues)

    # Exit 2 if any HIGH issues, 1 if only MEDIUM/LOW, 0 if clean
    if any(i.severity == "high" for i in issues):
        sys.exit(2)
    elif issues:
        sys.exit(1)
    else:
        print("[eval_scout] ✓ Scout eval passed — no issues found")
        sys.exit(0)


if __name__ == "__main__":
    main()
