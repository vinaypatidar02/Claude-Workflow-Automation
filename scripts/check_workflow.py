#!/usr/bin/env python3
"""
check_workflow.py — Full pipeline integrity check.

Run after ANY enhancement to scripts, agents, skills, hooks, or CLAUDE.md:
  python3 scripts/check_workflow.py          # all 8 checks
  python3 scripts/check_workflow.py --quick  # C1–C6 only (no subprocess, <2s)

Exit 0 if all checks pass, 1 if any fail.
"""

import json
import py_compile
import re
import subprocess
import sys
from pathlib import Path

ROOT   = Path(__file__).parent.parent
QUICK  = "--quick" in sys.argv
PASSED = []
FAILED = []


def _pass(label, detail=""):
    PASSED.append(label)
    suffix = f" ({detail})" if detail else ""
    print(f"  {label:<42} PASS{suffix}")


def _fail(label, reason, hint=""):
    FAILED.append(label)
    print(f"  {label:<42} FAIL")
    print(f"     ✗ {reason}")
    if hint:
        print(f"       → {hint}")


# ─────────────────────────────────────────────────────────────────────────────
# C1 — Required files exist
# ─────────────────────────────────────────────────────────────────────────────
def check_c1_files():
    required = [
        # data files
        "data/resumes/master_resume.pdf",
        "data/resumes/product_resume.pdf",
        "data/resumes/customer_resume.pdf",
        "data/job_tracker.json",
        "data/auto_rejected.json",
        "data/content/experience_bank.md",
        "data/content/cover_letter_bank.md",
        "data/processed_email_ids.json",
        # scripts
        "scripts/adzuna_scraper.py",
        "scripts/apify_cache.py",
        "scripts/auto_prep.py",
        "scripts/enrich_jobs.py",
        "scripts/validate_prep.py",
        "scripts/fetch_jd.py",
        "scripts/gmail_backfill.py",
        "scripts/pdf_renderer.py",
        "scripts/run_scout.py",
        "scripts/score_jobs.py",
        "scripts/sheets_sync.py",
        "scripts/test_email_tracker.py",
        # agents / skills / hooks
        "agents/job_scout.md",
        "agents/application_prep.md",
        "agents/tracker.md",
        "skills/score_job.md",
        "skills/tailor_resume.md",
        "skills/draft_cover_letter.md",
        "hooks/on_job_approved.md",
        "hooks/on_email_received.md",
        # root
        "CLAUDE.md",
        "mcp.json",
        ".env",
        ".claude/settings.json",
    ]
    missing = [p for p in required if not (ROOT / p).exists()]
    if missing:
        _fail("C1 Required files", f"{len(missing)} missing: {', '.join(missing[:3])}{'...' if len(missing) > 3 else ''}",
              "Ensure all pipeline files are present before running")
    else:
        _pass("C1 Required files", f"{len(required)}/{len(required)}")


# ─────────────────────────────────────────────────────────────────────────────
# C2 — Python syntax (compile check)
# ─────────────────────────────────────────────────────────────────────────────
def check_c2_syntax():
    scripts = sorted((ROOT / "scripts").glob("*.py"))
    errors = []
    for path in scripts:
        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as e:
            errors.append(f"{path.name}: {e}")
    if errors:
        for e in errors:
            _fail("C2 Python syntax", e, "Fix syntax error before any other work")
    else:
        _pass("C2 Python syntax", f"{len(scripts)}/{len(scripts)} scripts")


# ─────────────────────────────────────────────────────────────────────────────
# C3 — job_tracker.json schema
# ─────────────────────────────────────────────────────────────────────────────
def check_c3_tracker():
    path = ROOT / "data" / "job_tracker.json"
    if not path.exists():
        _fail("C3 job_tracker schema", "File missing — C1 should have caught this")
        return
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        _fail("C3 job_tracker schema", f"Invalid JSON: {e}", "Fix JSON syntax in job_tracker.json")
        return

    issues = []

    # Top-level structure
    if "_meta" not in data:
        issues.append("missing '_meta' key")
    if "applications" not in data:
        issues.append("missing 'applications' key")
    if issues:
        _fail("C3 job_tracker schema", "; ".join(issues))
        return

    meta  = data.get("_meta", {})
    apps  = data.get("applications", [])
    valid = set(meta.get("valid_statuses", []))

    if not valid:
        issues.append("_meta.valid_statuses is empty")

    # Per-entry checks
    seen_ids   = set()
    bad_status = []
    missing_fields = []
    dup_ids    = []

    for a in apps:
        for field in ("id", "company", "role", "status"):
            if not a.get(field):
                missing_fields.append(f"{a.get('id','?')}.{field}")
        aid = a.get("id", "")
        if aid in seen_ids:
            dup_ids.append(aid)
        seen_ids.add(aid)
        st = a.get("status", "")
        if valid and st and st not in valid:
            bad_status.append(f"{a.get('id','?')}: '{st}'")

    if missing_fields:
        issues.append(f"{len(missing_fields)} entries missing required fields: {', '.join(missing_fields[:3])}{'...' if len(missing_fields)>3 else ''}")
    if bad_status:
        issues.append(f"{len(bad_status)} invalid status values: {', '.join(bad_status[:3])}{'...' if len(bad_status)>3 else ''}")
    if dup_ids:
        issues.append(f"duplicate IDs: {', '.join(dup_ids[:3])}")

    if issues:
        for issue in issues:
            _fail("C3 job_tracker schema", issue)
    else:
        _pass("C3 job_tracker schema", f"{len(apps)} entries, all valid")


# ─────────────────────────────────────────────────────────────────────────────
# C4 — auto_rejected.json schema (key must be "auto_rejected", not "jobs")
# ─────────────────────────────────────────────────────────────────────────────
def check_c4_auto_rejected():
    path = ROOT / "data" / "auto_rejected.json"
    if not path.exists():
        _fail("C4 auto_rejected schema", "File missing")
        return
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        _fail("C4 auto_rejected schema", f"Invalid JSON: {e}")
        return

    if not isinstance(data, dict) or "auto_rejected" not in data:
        top_keys = list(data.keys())[:4] if isinstance(data, dict) else type(data).__name__
        _fail("C4 auto_rejected schema",
              f"Top-level key must be 'auto_rejected', found: {top_keys}",
              "Fix: wrap entries under {\"auto_rejected\": [...]} — wrong key caused today's incident")
        return

    entries = data["auto_rejected"]
    bad = [e.get("id", "?") for e in entries if not e.get("company") or not e.get("role")]
    if bad:
        _fail("C4 auto_rejected schema", f"{len(bad)} entries missing company/role: {bad[:3]}")
    else:
        _pass("C4 auto_rejected schema", f"{len(entries)} entries, correct key")


# ─────────────────────────────────────────────────────────────────────────────
# C5 — Sheets sync consistency
# ─────────────────────────────────────────────────────────────────────────────
def check_c5_sheets_sync():
    issues = []

    # 5a — ARCHIVED_STATUSES in sheets_sync.py must match tracker valid_statuses
    sync_src = (ROOT / "scripts" / "sheets_sync.py").read_text()
    m = re.search(r'ARCHIVED_STATUSES\s*=\s*\{([^}]+)\}', sync_src)
    if not m:
        issues.append("ARCHIVED_STATUSES constant not found in sheets_sync.py")
    else:
        raw = m.group(1)
        archived_in_code = {s.strip().strip('"').strip("'") for s in raw.split(",") if s.strip()}
        tracker_path = ROOT / "data" / "job_tracker.json"
        if tracker_path.exists():
            meta = json.loads(tracker_path.read_text()).get("_meta", {})
            valid = set(meta.get("valid_statuses", []))
            not_in_valid = archived_in_code - valid
            if not_in_valid:
                issues.append(
                    f"ARCHIVED_STATUSES values not in valid_statuses: {not_in_valid} "
                    f"— add them to _meta.valid_statuses"
                )

    # 5b — last_push_snapshot must exist (push was run at least once)
    tracker_path = ROOT / "data" / "job_tracker.json"
    if tracker_path.exists():
        meta = json.loads(tracker_path.read_text()).get("_meta", {})
        if not meta.get("last_push_snapshot"):
            issues.append("_meta.last_push_snapshot missing — run: python3 scripts/sheets_sync.py push")

    if issues:
        for issue in issues:
            _fail("C5 Sheets sync consistency", issue)
    else:
        _pass("C5 Sheets sync consistency")


# ─────────────────────────────────────────────────────────────────────────────
# C6 — settings.json integrity
# ─────────────────────────────────────────────────────────────────────────────
def check_c6_settings():
    path = ROOT / ".claude" / "settings.json"
    if not path.exists():
        _fail("C6 settings.json", "File missing")
        return
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        _fail("C6 settings.json", f"Invalid JSON: {e}", "Fix JSON syntax in .claude/settings.json")
        return

    issues = []
    hooks = data.get("hooks", {})
    if "PostToolUse" not in hooks:
        issues.append("hooks.PostToolUse missing")
    else:
        for entry in hooks["PostToolUse"]:
            for h in entry.get("hooks", []):
                cmd = h.get("command", "")
                if "job_tracker.json" in cmd and not (ROOT / "data" / "job_tracker.json").exists():
                    issues.append("hook references data/job_tracker.json which does not exist")

    if issues:
        for issue in issues:
            _fail("C6 settings.json", issue)
    else:
        _pass("C6 settings.json")


# ─────────────────────────────────────────────────────────────────────────────
# C7 — pdf_renderer self-test  (skipped with --quick)
# ─────────────────────────────────────────────────────────────────────────────
def check_c7_pdf_renderer():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "pdf_renderer.py"), "test"],
        capture_output=True, text=True, cwd=str(ROOT)
    )
    if result.returncode != 0:
        _fail("C7 pdf_renderer self-test",
              result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "non-zero exit",
              "Check reportlab install: pip install reportlab")
    else:
        _pass("C7 pdf_renderer self-test")


# ─────────────────────────────────────────────────────────────────────────────
# C8 — email tracker dry run  (skipped with --quick)
# ─────────────────────────────────────────────────────────────────────────────
def check_c8_email_tracker():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "test_email_tracker.py"), "--dry"],
        capture_output=True, text=True, cwd=str(ROOT)
    )
    if result.returncode != 0:
        _fail("C8 email tracker dry run",
              result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "non-zero exit",
              "Check data/job_tracker.json is readable and well-formed")
    else:
        _pass("C8 email tracker dry run")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def main():
    mode = "QUICK (C1–C6)" if QUICK else "FULL (C1–C8)"
    print(f"\n[check_workflow] Workflow Integrity Check — {mode}\n")

    check_c1_files()
    check_c2_syntax()
    check_c3_tracker()
    check_c4_auto_rejected()
    check_c5_sheets_sync()
    check_c6_settings()

    if not QUICK:
        check_c7_pdf_renderer()
        check_c8_email_tracker()

    total = len(PASSED) + len(FAILED)
    print()
    if not FAILED:
        print(f"  ✓ All {total} checks passed — workflow is intact")
    else:
        print(f"  ✗ {len(FAILED)} of {total} checks FAILED — resolve before closing this task")
    print()
    sys.exit(0 if not FAILED else 1)


if __name__ == "__main__":
    main()
