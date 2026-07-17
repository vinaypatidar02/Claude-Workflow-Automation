#!/usr/bin/env python3
"""
eval_prep.py — Quality evaluator for auto_prep.py output (per job, zero API cost).

Runs after auto_prep.py (STEP 2), before LLM tailoring (STEP 3).
Catches leadership misclassification, bullet ordering anomalies, and tag gaps
while they're still easy to fix — before the LLM bakes in the wrong signal.

Checks (all deterministic, zero Claude API calls):
  D1 — Leadership signal consistency (is_leadership true/false mismatch)
  D2 — Bullet ordering quality (uses bullet_scores from _auto_prep_meta)
  D3 — Tag completeness (heuristic rules vs experience_bank.md)

Usage:
  python3 scripts/eval_prep.py --resume /tmp/auto_resume_<id>.json --jd /tmp/jd_<id>.txt
  python3 scripts/eval_prep.py --resume ... --jd ... --apply-ephemeral
"""

import argparse
import json
import re
import sys
from pathlib import Path

ROOT          = Path(__file__).parent.parent
EXP_BANK_PATH = ROOT / "data" / "content" / "experience_bank.md"

sys.path.insert(0, str(Path(__file__).parent))
from eval_base import (
    Issue, print_report,
)

# ─────────────────────────────────────────────────────────────────────────────
# D1 — LEADERSHIP SIGNAL CONSISTENCY
# Mirrors the logic in auto_prep.py so we can detect mismatches.
# ─────────────────────────────────────────────────────────────────────────────

# Contextual keywords: these require the next meaningful word to confirm
# the role IS an analytics leadership role, not just org-chart mention.
_CONTEXTUAL_KW = {"director of", "vp of", "head of", "chief"}

# Analytics-domain words that make contextual keywords genuine
_ANALYTICS_CONTEXT_WORDS = {
    "analytics", "data", "insight", "intelligence", "reporting",
    "bi", "product", "growth", "decision",
}

# Unambiguous people-management signals in JD text (should always → is_leadership=True)
_STRONG_LEADERSHIP_RE = [
    re.compile(r"\d+\s*[-–]\s*\d+\s*direct\s+reports", re.IGNORECASE),
    re.compile(r"lead[,.]?\s*coach", re.IGNORECASE),
    re.compile(r"coach\s+and\s+develop", re.IGNORECASE),
    re.compile(r"hire\s+and\s+grow", re.IGNORECASE),
    re.compile(r"grow\s+a\s+team", re.IGNORECASE),
    re.compile(r"line\s+manage", re.IGNORECASE),
    re.compile(r"manage\s+(?:a\s+)?team\s+of\s+\d+", re.IGNORECASE),
    re.compile(r"people\s+management", re.IGNORECASE),
    re.compile(r"direct\s+reports", re.IGNORECASE),
]

# Import the same LEADERSHIP_KEYWORDS used by auto_prep.py
def _load_leadership_keywords() -> list[str]:
    try:
        import importlib.util as _ilu
        spec = _ilu.spec_from_file_location("auto_prep", Path(__file__).parent / "auto_prep.py")
        mod  = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.LEADERSHIP_KEYWORDS
    except Exception:
        return []


_LEADERSHIP_KEYWORDS = _load_leadership_keywords()

# Import TITLE_LINES from auto_prep for checking the expected title
def _load_title_lines() -> dict:
    try:
        import importlib.util as _ilu
        spec = _ilu.spec_from_file_location("auto_prep", Path(__file__).parent / "auto_prep.py")
        mod  = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.TITLE_LINES
    except Exception:
        return {}


_TITLE_LINES = _load_title_lines()


def _jd_is_leadership(jd: str) -> tuple[bool, str]:
    """Return (is_leadership, matched_phrase)."""
    jd_lower = jd.lower()
    # Strong unambiguous patterns first
    for pattern in _STRONG_LEADERSHIP_RE:
        m = pattern.search(jd)
        if m:
            return True, m.group(0)
    # LEADERSHIP_KEYWORDS list (same as auto_prep.py)
    for kw in _LEADERSHIP_KEYWORDS:
        if kw in jd_lower:
            return True, kw
    return False, ""


def _jd_leadership_false_pos(jd: str) -> tuple[bool, str]:
    """Detect false positives: contextual keywords matching org-chart (not the role itself)."""
    jd_lower = jd.lower()
    for ctx in _CONTEXTUAL_KW:
        idx = jd_lower.find(ctx)
        while idx != -1:
            following = jd_lower[idx + len(ctx):idx + len(ctx) + 30].split()
            if following and following[0] not in _ANALYTICS_CONTEXT_WORDS:
                # e.g. "director of product" → following[0]="product" → not analytics context
                # BUT we matched LEADERSHIP_KEYWORDS which includes e.g. "head of analytics"
                # So only flag if the auto_prep match came ONLY from this contextual keyword
                snippet = jd_lower[idx:idx + len(ctx) + 30]
                return True, snippet.strip()
            idx = jd_lower.find(ctx, idx + 1)
    return False, ""


def check_d1_leadership(resume_json: dict, jd: str) -> list[Issue]:
    meta = resume_json.get("_auto_prep_meta", {})
    is_leadership_detected = meta.get("is_leadership", False)
    domain    = meta.get("domain", "product")
    title_lines = resume_json.get("title_lines", [])
    issues = []

    jd_is_lead, matched_phrase = _jd_is_leadership(jd)

    # False negative: auto_prep says False but JD clearly has leadership signals
    if not is_leadership_detected and jd_is_lead:
        expected_titles = _TITLE_LINES.get((domain, True), ["Analytics Lead & Manager", ""])
        issues.append(Issue(
            check_id="D1",
            severity="high",
            title="Leadership false negative — JD has people-management signals",
            evidence=(
                f"is_leadership=False but JD contains: \"{matched_phrase}\"\n"
                f"Current title_lines: {title_lines}\n"
                f"Expected: {expected_titles}"
            ),
            ephemeral_fix={
                "description": f"Override title_lines to {expected_titles}",
                "field": "title_lines",
                "value": expected_titles,
            },
            systemic_fix={
                "file": "scripts/auto_prep.py",
                "description": (
                    f"Add '{matched_phrase.strip()}' to the LEADERSHIP_KEYWORDS list "
                    f"(search for 'LEADERSHIP_KEYWORDS' in auto_prep.py to find it)."
                ),
            },
        ))

    # False positive: is_leadership=True but title_lines are the IC variant ("Lead Analytics Professional")
    if is_leadership_detected and title_lines and "lead & manager" not in title_lines[0].lower():
        # Might be correct if domain=general — check expected
        expected_lead_titles = _TITLE_LINES.get((domain, True), ["Analytics Lead & Manager", ""])
        expected_ic_titles   = _TITLE_LINES.get((domain, False), ["Lead Analytics Professional", ""])
        if title_lines == expected_ic_titles and title_lines != expected_lead_titles:
            issues.append(Issue(
                check_id="D1",
                severity="medium",
                title="Leadership inconsistency — is_leadership=True but title_lines are IC variant",
                evidence=(
                    f"is_leadership=True, domain={domain!r}\n"
                    f"title_lines: {title_lines} (looks like IC variant)\n"
                    f"Expected for leadership: {expected_lead_titles}"
                ),
                ephemeral_fix={
                    "description": f"Override title_lines to {expected_lead_titles}",
                    "field": "title_lines",
                    "value": expected_lead_titles,
                },
                systemic_fix=None,
            ))

    return issues


# ─────────────────────────────────────────────────────────────────────────────
# D2 — BULLET ORDERING QUALITY
# Uses bullet_scores from _auto_prep_meta (populated by Change A in auto_prep.py)
# ─────────────────────────────────────────────────────────────────────────────

def check_d2_bullet_ordering(resume_json: dict) -> list[Issue]:
    meta = resume_json.get("_auto_prep_meta", {})
    bullet_scores_map = meta.get("bullet_scores")

    if not bullet_scores_map:
        # bullet_scores not present — auto_prep.py hasn't been updated yet (Change A pending)
        return []

    work_history = resume_json.get("work_history", [])
    issues = []

    for role_entry in work_history:
        company  = role_entry.get("company", "")
        bullets  = role_entry.get("bullets", [])
        if not bullets:
            continue

        # Match role to bullet_scores by company name substring
        role_key = None
        for key in bullet_scores_map:
            if key.lower() in company.lower() or company.lower() in key.lower():
                role_key = key
                break
        if not role_key:
            continue

        score_list = bullet_scores_map[role_key]  # [{score, text, tags, selected}]
        selected_scores_map = {
            item["text"]: item["score"]
            for item in score_list if item.get("selected")
        }
        unselected_items = [item for item in score_list if not item.get("selected")]
        unselected_scores = [item["score"] for item in unselected_items]

        if not selected_scores_map or not bullets:
            continue

        # Check ordering: bullets[0] should be the highest-scoring selected bullet
        b0_score  = selected_scores_map.get(bullets[0], -1)
        max_sel   = max(selected_scores_map.values())
        max_unsel = max(unselected_scores) if unselected_scores else 0
        min_sel   = min(selected_scores_map.values())

        # Ordering anomaly: the first bullet is not the highest-scoring selected one
        # (threshold ≥ 2: ignore ties at 0/1 which are normal diversity-pool behaviour)
        ordering_problem  = b0_score < max_sel and max_sel >= 2 and b0_score < max_sel - 1
        # Selection anomaly: a meaningful domain bullet (score ≥ 2) was skipped while a
        # lower-scoring bullet was selected — indicates a real selection or tag bug.
        selection_problem = max_unsel >= 2 and max_unsel > min_sel

        if not ordering_problem and not selection_problem:
            continue

        # Top unselected bullet text — used in systemic_fix description
        top_unsel_item = max(unselected_items, key=lambda x: x["score"], default=None)
        top_unsel_text = top_unsel_item["text"] if top_unsel_item else ""

        # Build expected order (highest-scoring selected bullets first)
        expected_order = sorted(
            [b for b in bullets if b in selected_scores_map],
            key=lambda b: selected_scores_map.get(b, 0),
            reverse=True,
        )
        details = []
        if ordering_problem:
            details.append(
                f"Top bullet scores {b0_score}, but highest-scoring selected is {max_sel}"
            )
        if selection_problem:
            details.append(
                f"Highest unselected bullet scores {max_unsel} (domain score ≥ 2) > "
                f"lowest selected {min_sel} — possible missing tag or bonus gap"
            )

        # Only include expected_top if it actually differs from current top
        top_changed = expected_order and expected_order[0] != bullets[0]
        evidence_lines = "\n".join(details)
        if top_changed:
            evidence_lines += (
                f"\nCurrent top: \"{bullets[0][:80]}...\"\n"
                f"Expected top: \"{expected_order[0][:80]}...\""
            )

        issues.append(Issue(
            check_id="D2",
            severity="medium",
            title=f"Bullet ordering anomaly — {company}",
            evidence=evidence_lines,
            ephemeral_fix={
                "description": f"Reorder {company} bullets: put highest-scoring first",
                "field": "bullets",
                "company": company,
                "expected_order": expected_order,
            } if top_changed else None,
            systemic_fix={
                "file": "data/content/experience_bank.md",
                "description": (
                    f"Add a missing domain tag to the highest-scoring unselected bullet "
                    f"for {company} (scores {max_unsel} but was skipped). "
                    f"Search experience_bank.md for: "
                    f"\"{top_unsel_text[:60]}\" "
                    f"and add the appropriate domain tag (e.g. [product], [growth], [CRM]) "
                    f"at the start of the line."
                ),
            } if selection_problem else None,
        ))

    return issues


# ─────────────────────────────────────────────────────────────────────────────
# D3 — TAG COMPLETENESS
# ─────────────────────────────────────────────────────────────────────────────

# Heuristic rules: if text matches pattern → expect tag
_TAG_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bexperiment\b|\ba/b\b|\bab test\b", re.IGNORECASE), "experimentation"),
    (re.compile(r"\bteam of\s+\d+|\bmentored?\b|\bhired?\b|\bled\s+(?:a\s+)?team\b", re.IGNORECASE), "leadership"),
    (re.compile(r"\b\d+%\b.*\bgrowth\b|\bgrowth\b.*\b\d+%\b", re.IGNORECASE), "growth"),
    (re.compile(r"\bproduct\b.*\b(?:analytics?|metrics?|kpis?)\b", re.IGNORECASE), "product"),
    (re.compile(r"\bpropensity\b|\bsegmentation\b|\blifecycle\b|\bcrm\b", re.IGNORECASE), "CRM"),
    (re.compile(r"\bpric(?:ing|e)\b|\bcommercial\b|\bprocurement\b", re.IGNORECASE), "pricing"),
]

# Only flag missing tag if it's relevant to the active domain
_DOMAIN_BULLET_TAGS: dict[str, set] = {
    "product":  {"product", "growth", "experimentation"},
    "crm":      {"CRM", "growth", "product"},
    "pricing":  {"pricing", "bi"},
    "bi":       {"bi", "data-eng"},
    "general":  {"product", "growth", "CRM", "pricing", "experimentation", "leadership"},
}


def _parse_bank_tags(exp_bank_path: Path) -> dict[str, set]:
    """Returns {bullet_text: {tags}} from experience_bank.md."""
    if not exp_bank_path.exists():
        return {}
    result = {}
    for line in exp_bank_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("- ["):
            continue
        body = line[2:].strip()
        tags: set[str] = set()
        while body.startswith("["):
            close = body.find("]")
            if close < 0:
                break
            tag = body[1:close]
            if re.match(r"^[\w-]+$", tag):
                tags.add(tag)
            body = body[close + 1:].strip()
        if body:
            result[body] = tags
    return result


def check_d3_tag_completeness(resume_json: dict) -> list[Issue]:
    meta   = resume_json.get("_auto_prep_meta", {})
    domain = meta.get("domain", "product")
    domain_tags = _DOMAIN_BULLET_TAGS.get(domain, set())

    bank_tags = _parse_bank_tags(EXP_BANK_PATH)
    issues = []

    for role_entry in resume_json.get("work_history", []):
        company = role_entry.get("company", "")
        for bullet in role_entry.get("bullets", []):
            existing_tags = bank_tags.get(bullet, set())
            for pattern, expected_tag in _TAG_RULES:
                if expected_tag not in domain_tags:
                    continue  # tag not relevant for this domain
                if expected_tag in existing_tags:
                    continue  # already tagged
                if pattern.search(bullet):
                    issues.append(Issue(
                        check_id="D3",
                        severity="low",
                        title=f"Missing [{expected_tag}] tag on bullet",
                        evidence=(
                            f"Company: {company}\n"
                            f"Bullet:  \"{bullet[:100]}...\"\n"
                            f"Pattern matched [{expected_tag}] but tag is absent in bank.\n"
                            f"Current tags: {sorted(existing_tags)}"
                        ),
                        ephemeral_fix=None,
                        systemic_fix={
                            "file": "data/content/experience_bank.md",
                            "description": (
                                f"Search experience_bank.md for the line containing: "
                                f"\"{bullet[:70]}\" "
                                f"and prepend [{expected_tag}] to its existing tag list."
                            ),
                        },
                    ))
    return issues


# ─────────────────────────────────────────────────────────────────────────────
# EPHEMERAL FIX APPLIER
# ─────────────────────────────────────────────────────────────────────────────

def apply_ephemeral_fixes(issues: list[Issue], resume_path: Path) -> None:
    resume = json.loads(resume_path.read_text())

    for iss in issues:
        fix = iss.ephemeral_fix
        if not fix:
            continue

        field = fix.get("field", "")

        if field == "title_lines":
            old = resume.get("title_lines", [])
            resume["title_lines"] = fix["value"]
            # Also fix cover letter meta if present (it mirrors title_lines)
            if "_auto_prep_meta" in resume:
                resume["_auto_prep_meta"]["title_lines_override"] = fix["value"]
            print(f"  [fix] title_lines: {old} → {fix['value']}")

        elif field == "bullets":
            company_target = fix.get("company", "")
            expected_order = fix.get("expected_order", [])
            if not expected_order:
                continue
            for role_entry in resume.get("work_history", []):
                if company_target.lower() in role_entry.get("company", "").lower():
                    old_b0 = role_entry["bullets"][0] if role_entry["bullets"] else ""
                    # Fill in any bullets not in expected_order at the end
                    ordered = list(expected_order)
                    for b in role_entry["bullets"]:
                        if b not in ordered:
                            ordered.append(b)
                    role_entry["bullets"] = ordered[:len(role_entry["bullets"])]
                    print(f"  [fix] {company_target} bullets reordered: "
                          f"\"{old_b0[:50]}\" → \"{ordered[0][:50]}\"")
                    break

    resume_path.write_text(json.dumps(resume, indent=2, ensure_ascii=False))
    print(f"[eval_prep] Ephemeral fixes applied to {resume_path.name}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Prep quality evaluator (zero API cost)")
    parser.add_argument("--resume",           required=True, help="Path to auto_resume_<id>.json")
    parser.add_argument("--jd",               required=True, help="Path to jd_<id>.txt")
    parser.add_argument("--apply-ephemeral",  action="store_true",
                        help="Apply ephemeral fixes (title_lines override, bullet reorder)")
    args = parser.parse_args()

    resume_path = Path(args.resume)
    jd_path     = Path(args.jd)

    if not resume_path.exists():
        print(f"[eval_prep] ERROR: resume not found: {resume_path}")
        sys.exit(1)
    if not jd_path.exists():
        print(f"[eval_prep] WARNING: JD file not found: {jd_path} — D1 check limited")

    resume_json = json.loads(resume_path.read_text())
    jd_text     = jd_path.read_text(encoding="utf-8") if jd_path.exists() else ""

    meta    = resume_json.get("_auto_prep_meta", {})
    company = meta.get("primary_company", "Unknown")
    job_id  = resume_path.stem.replace("auto_resume_", "")

    print(f"[eval_prep] Evaluating: job_id={job_id}  "
          f"domain={meta.get('domain')}  is_leadership={meta.get('is_leadership')}")

    issues: list[Issue] = []
    issues += check_d1_leadership(resume_json, jd_text)
    issues += check_d2_bullet_ordering(resume_json)
    issues += check_d3_tag_completeness(resume_json)

    print_report(
        issues,
        header=f"Prep — {company} / {job_id}",
        checks_desc="D1 (leadership signal) · D2 (bullet ordering) · D3 (tag completeness)",
    )

    if issues and args.apply_ephemeral:
        apply_ephemeral_fixes(issues, resume_path)

    if any(i.severity == "high" for i in issues):
        sys.exit(2)
    elif issues:
        sys.exit(1)
    else:
        print("[eval_prep] ✓ Prep eval passed — no issues found")
        sys.exit(0)


if __name__ == "__main__":
    main()
