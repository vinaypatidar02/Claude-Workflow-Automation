#!/usr/bin/env python3
"""
eval_base.py — Shared infrastructure for the self-evaluation loop.

Provides Issue type, the report formatter, and fuzzy-match helpers
used by eval_scout.py, eval_prep.py, and eval_track.py.

Flow:
  Each eval script prints a report.  The agent then:
    Part A (automatic): re-runs with --apply-ephemeral to patch this run's data.
    Part B (per issue):  shows the SYSTEMIC line to user, asks approval,
                         applies the named file edit with the Edit tool,
                         then runs check_workflow.py --quick to confirm.
  No pending_fixes.json queue — every issue is fully resolved during the run.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT     = Path(__file__).parent.parent
ENV_FILE = ROOT / ".env"


# ─────────────────────────────────────────────────────────────────────────────
# DATA TYPE
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Issue:
    check_id:      str          # "SC1", "D1", "T1", etc.
    severity:      str          # "high" | "medium" | "low"
    title:         str
    evidence:      str
    ephemeral_fix: dict | None  # in-place data patch, applied with --apply-ephemeral
    systemic_fix:  dict | None  # root-cause edit — agent applies inline with user approval
                                # must include: {"file": "...", "description": "..."}
                                # description should name the exact variable/function to edit


# ─────────────────────────────────────────────────────────────────────────────
# REPORT FORMATTER
# ─────────────────────────────────────────────────────────────────────────────

SEV_ORDER = {"high": 0, "medium": 1, "low": 2}
SEV_LABEL = {"high": "[HIGH]", "medium": "[MEDIUM]", "low": "[LOW]"}
WIDTH     = 60


def print_report(issues: list[Issue], header: str, checks_desc: str = "") -> None:
    """Print a boxed quality-evaluation report to stdout."""
    bar  = "═" * WIDTH
    dash = "─" * WIDTH

    high   = [i for i in issues if i.severity == "high"]
    medium = [i for i in issues if i.severity == "medium"]
    low    = [i for i in issues if i.severity == "low"]

    count_str = (
        f"{len(issues)} found  ({len(high)} high · {len(medium)} medium · {len(low)} low)"
        if issues else "none — all clear"
    )

    print(f"\n{bar}")
    print(f" Quality Evaluation — {header}")
    print(bar)
    if checks_desc:
        print(f" Checks: {checks_desc}")
    print(f" Issues: {count_str}")
    print(dash)

    if not issues:
        print(f"\n  ✓ All checks passed — proceeding automatically\n")
        print(bar)
        return

    sorted_issues = sorted(issues, key=lambda x: SEV_ORDER.get(x.severity, 9))
    for iss in sorted_issues:
        print()
        print(f"{SEV_LABEL[iss.severity]} {iss.check_id} — {iss.title}")
        for line in iss.evidence.splitlines():
            print(f"  {line}")
        if iss.ephemeral_fix:
            print(f"  EPHEMERAL: {iss.ephemeral_fix.get('description', '')}")
        if iss.systemic_fix:
            f = iss.systemic_fix
            print(f"  SYSTEMIC:  {f.get('file', '')} — {f.get('description', '')}")

    has_ephemeral = any(i.ephemeral_fix for i in issues)
    has_systemic  = any(i.systemic_fix  for i in issues)

    print()
    print(dash)
    if has_ephemeral:
        print(" EPHEMERAL: re-run with --apply-ephemeral to patch this run's data.")
    if has_systemic:
        print(" SYSTEMIC:  for each SYSTEMIC line above — agent asks approval, then")
        print("            uses Edit tool on the named file, then check_workflow --quick.")
    print(bar)


# ─────────────────────────────────────────────────────────────────────────────
# ENV LOADER
# ─────────────────────────────────────────────────────────────────────────────

def load_env(env_file: Path = ENV_FILE) -> dict:
    env = {}
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


# ─────────────────────────────────────────────────────────────────────────────
# FUZZY HELPERS  (shared by SC4 and T2)
# ─────────────────────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def _edit_distance(a: str, b: str) -> int:
    """Simple Levenshtein for short strings."""
    if abs(len(a) - len(b)) > 4:
        return 99
    m, n = len(a), len(b)
    dp = list(range(n + 1))
    for i in range(1, m + 1):
        prev = dp[:]
        dp[0] = i
        for j in range(1, n + 1):
            dp[j] = min(prev[j] + 1, dp[j - 1] + 1,
                        prev[j - 1] + (0 if a[i - 1] == b[j - 1] else 1))
    return dp[n]


def company_fuzzy_match(name_a: str, name_b: str) -> bool:
    a, b = _norm(name_a), _norm(name_b)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    ta = set(a.split()[:3])
    tb = set(b.split()[:3])
    if ta & tb and len(ta & tb) >= min(2, len(ta), len(tb)):
        return True
    return _edit_distance(a, b) <= 2


def role_word_overlap(role_a: str, role_b: str) -> int:
    stop = {"the", "and", "for", "with", "of", "in", "at", "to"}
    wa = {w for w in _norm(role_a).split() if w not in stop and len(w) > 2}
    wb = {w for w in _norm(role_b).split() if w not in stop and len(w) > 2}
    return len(wa & wb)
