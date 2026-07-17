#!/usr/bin/env python3
"""
validate_prep.py — Pre-render validation for resume + cover letter JSON.

Run BEFORE pdf_renderer.py to catch LLM-written field issues before they
reach the PDF. Exits 0 if all checks pass, 1 if any fail.

Usage:
  python3 scripts/validate_prep.py \
      --resume /tmp/final_resume_<job_id>.json \
      --cover  /tmp/final_cover_<job_id>.json \
      --jd     /tmp/jd_<job_id>.txt \
      --company "<Company Name>"

  --jd and --company are optional but enable V4 and V5 checks.
"""

import argparse
import json
import re
import sys
from pathlib import Path

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


# ── V1 — AI fluency sentence ──────────────────────────────────────────────────
# Keywords derived from experience_bank.md AI Engineering bullets:
#   Bullet 2: "Claude Code", "MCP servers", "agentic workflow"
#   Bullet 3: "Anthropic API", "prompt engineering"
# Check: "claude" (mandatory anchor) + at least 1 of ["mcp", "anthropic", "agentic"]
AI_FLUENCY_ANCHOR  = "claude"
AI_FLUENCY_SUPPORT = ["mcp", "anthropic", "agentic"]

def check_v1_ai_fluency(resume: dict):
    summary = resume.get("summary", "").lower()
    if AI_FLUENCY_ANCHOR in summary and any(kw in summary for kw in AI_FLUENCY_SUPPORT):
        _pass("V1 AI fluency sentence")
    else:
        _fail(
            "V1 AI fluency sentence",
            "Summary missing AI fluency signal: needs 'claude' + at least one of "
            f"{AI_FLUENCY_SUPPORT}.",
            "S4 must name Claude + one of: MCP, Anthropic, or agentic pipeline."
        )


# ── V2 — Excluded tools not in expertise/skills/summary/cover ────────────────
EXCLUDED_TOOLS = ["google analytics", "firebase", "apache airflow"]

def check_v2_excluded_tools(resume: dict, cover: dict):
    issues = []

    # Fields to scan (NOT work_history bullets — those show historical exposure accurately)
    expertise_str = " ".join(resume.get("core_expertise", [])).lower()
    skills_str    = " ".join(resume.get("skills", [])).lower()
    summary_str   = resume.get("summary", "").lower()
    cover_paras   = cover.get("paragraphs", [])

    for tool in EXCLUDED_TOOLS:
        if tool in expertise_str:
            issues.append(f"'{tool}' found in core_expertise. Remove — it may stay in work history bullets only.")
        if tool in skills_str:
            issues.append(f"'{tool}' found in skills. Remove — it may stay in work history bullets only.")
        if tool in summary_str:
            issues.append(f"'{tool}' found in resume summary. Remove — it may stay in work history bullets only.")
        for i, para in enumerate(cover_paras):
            if tool in para.lower():
                issues.append(
                    f"'{tool}' found in cover letter para {i+1}. "
                    f"Remove — it may stay in work history bullets only."
                )

    if issues:
        for issue in issues:
            _fail("V2 Excluded tools", issue)
    else:
        _pass("V2 Excluded tools", f"{len(EXCLUDED_TOOLS)} tools checked")


# ── V3 — Cover letter word count (350–450 per CLAUDE.md Section 5) ───────────
COVER_MIN = 350
COVER_MAX = 450

def check_v3_word_count(cover: dict):
    paras = cover.get("paragraphs", [])
    words = sum(len(p.split()) for p in paras)
    if COVER_MIN <= words <= COVER_MAX:
        _pass("V3 Cover word count", f"{words} words")
    elif words < COVER_MIN:
        _fail(
            "V3 Cover word count",
            f"Cover letter is {words} words (min {COVER_MIN}).",
            "Expand Para 4 to reach target."
        )
    else:
        _fail(
            "V3 Cover word count",
            f"Cover letter is {words} words (max {COVER_MAX}).",
            "Trim Para 3 first, then Para 4. Never trim Para 1 or Para 2."
        )


# ── V4 — Company name in cover letter Para 1 ─────────────────────────────────
def check_v4_company_name(cover: dict, company: str):
    if not company:
        _pass("V4 Company in Para 1", "skipped (no --company arg)")
        return
    import re as _re
    # Strip parenthetical qualifiers e.g. "(anonymised)", "(via Harnham)" before checking
    company_clean = _re.sub(r'\s*\([^)]*\)\s*', ' ', company).strip()
    paras = cover.get("paragraphs", [])
    para1 = paras[0].lower() if paras else ""
    if company_clean.lower() in para1 or company.lower() in para1:
        _pass("V4 Company in Para 1")
    else:
        _fail(
            "V4 Company in Para 1",
            f"'{company_clean}' not found in Para 1.",
            "Para 1 must name the company. Generic opener will hurt response rate."
        )


# ── V5 — MMM/attribution framing guard (JD-conditional) ─────────────────────
MMM_JD_SIGNALS = [
    "mmm", "media mix", "marketing mix", "attribution model",
    "multi-touch attribution", "mta"
]
MMM_CLAIM_PHRASES = [
    "delivered mmm", "built mmm", "mmm model", "attribution model experience",
    "direct experience with mmm"
]
MMM_SAFE_WORDS = ["adjacent", "upskill", "learning", "robyn", "meridian", "active study"]

def check_v5_mmm_framing(resume: dict, cover: dict, jd_text: str):
    if not jd_text:
        _pass("V5 MMM framing guard", "skipped (no --jd arg)")
        return

    jd_lower = jd_text.lower()
    if not any(sig in jd_lower for sig in MMM_JD_SIGNALS):
        _pass("V5 MMM framing guard", "JD does not mention MMM/attribution")
        return

    all_text = (resume.get("summary", "") + " " +
                " ".join(cover.get("paragraphs", []))).lower()

    for phrase in MMM_CLAIM_PHRASES:
        if phrase in all_text:
            _fail(
                "V5 MMM framing guard",
                f"JD mentions MMM/attribution and text contains claim phrase: '{phrase}'.",
                "Framing must be 'adjacent + upskilling' only. NEVER claim direct MMM delivery."
            )
            return

    if not any(safe in all_text for safe in MMM_SAFE_WORDS):
        _fail(
            "V5 MMM framing guard",
            "JD mentions MMM/attribution but no safe framing word found in summary or cover.",
            f"Add one of: {', '.join(MMM_SAFE_WORDS)} to show framing is 'adjacent + active study'."
        )
        return

    _pass("V5 MMM framing guard", "JD has MMM signal; safe framing present")


# ── V6 — No FILL_ME placeholders ─────────────────────────────────────────────
def check_v6_no_fill_me(resume: dict, cover: dict):
    resume_str = json.dumps(resume)
    cover_str  = json.dumps(cover)
    issues = []
    if "FILL_ME" in resume_str:
        issues.append("FILL_ME placeholder found in resume JSON. LLM step incomplete.")
    if "FILL_ME" in cover_str:
        issues.append("FILL_ME placeholder found in cover letter JSON. LLM step incomplete.")
    if issues:
        for issue in issues:
            _fail("V6 No FILL_ME placeholders", issue)
    else:
        _pass("V6 No FILL_ME placeholders")


# ── V7 — Canonical metrics preservation ──────────────────────────────────────
# Metrics: 40% = Flipkart grocery visits, 30% = BeepKart procurement / DeHaat overdue,
#          85% = XGBoost accuracy, 70% = propensity model reach,
#          35% = BeepKart vehicle inspection time reduction
CANONICAL_METRICS   = ["40%", "30%", "85%", "70%", "35%"]
METRICS_MIN_PRESENT = 3   # distinct canonical metrics required across bullets+cover
METRICS_BULLETS_MIN = 4   # bullets that must each contain ≥1 canonical metric

def check_v7_metrics(resume: dict, cover: dict):
    # Scope: work_history bullets + cover paragraphs only.
    # Summary is positioning text (no raw % metrics expected there).
    all_bullets = [b for r in resume.get("work_history", []) for b in r.get("bullets", [])]
    combined    = (" ".join(all_bullets) + " " + " ".join(cover.get("paragraphs", []))).lower()

    present = [m for m in CANONICAL_METRICS if m in combined]
    missing = [m for m in CANONICAL_METRICS if m not in combined]

    # Sub-A: how many distinct bullets each contain ≥1 canonical metric
    metric_bullets = [b for b in all_bullets if any(m in b.lower() for m in CANONICAL_METRICS)]
    bullet_count   = len(metric_bullets)

    sub_a_pass = bullet_count >= METRICS_BULLETS_MIN
    sub_b_pass = len(present) >= METRICS_MIN_PRESENT

    if sub_a_pass and sub_b_pass:
        _pass("V7 Canonical metrics",
              f"{len(present)}/{len(CANONICAL_METRICS)} distinct metrics · {bullet_count} metric-bearing bullets")
    else:
        reasons = []
        if not sub_b_pass:
            reasons.append(
                f"only {len(present)}/{len(CANONICAL_METRICS)} distinct metrics "
                f"(need {METRICS_MIN_PRESENT}); missing: {', '.join(missing)}"
            )
        if not sub_a_pass:
            reasons.append(
                f"only {bullet_count} bullets contain a canonical metric "
                f"(need {METRICS_BULLETS_MIN})"
            )
        _fail("V7 Canonical metrics", "; ".join(reasons),
              f"Restore bullets with: {', '.join(CANONICAL_METRICS)}")


# ── V8 — Agile/Jira signal (JD-conditional) ──────────────────────────────────
AGILE_JD_SIGNALS = [
    "agile", "scrum", "sprint", "jira", "kanban",
    "sprint planning", "sprint delivery", "agile delivery",
]
AGILE_RESUME_SIGNALS = ["jira", "confluence", "agile", "scrum", "sprint"]

def check_v8_agile_jira(resume: dict, jd_text: str):
    if not jd_text:
        _pass("V8 Agile/Jira signal", "skipped (no --jd arg)")
        return

    jd_lower = jd_text.lower()
    if not any(sig in jd_lower for sig in AGILE_JD_SIGNALS):
        _pass("V8 Agile/Jira signal", "JD does not mention Agile/Scrum")
        return

    # Scope: work_history bullets only.
    # Summary is positioning text — Agile/Jira is delivery evidence, not positioning.
    all_bullets = [b for r in resume.get("work_history", []) for b in r.get("bullets", [])]
    all_text = " ".join(all_bullets).lower()

    if any(sig in all_text for sig in AGILE_RESUME_SIGNALS):
        _pass("V8 Agile/Jira signal", "JD has Agile signal; Jira/Confluence present in bullets")
    else:
        _fail(
            "V8 Agile/Jira signal",
            "JD mentions Agile/Scrum/sprint but no Jira or Confluence signal in work history bullets.",
            "Check DeHaat and BeepKart bullets tagged [leadership] — they carry Jira/Confluence. "
            "Ensure at least one is selected, or add a clean bullet to experience_bank.md."
        )


# ── V9 — Git/GitHub version control signal (JD-conditional) ─────────────────
GIT_JD_SIGNALS = [
    "version control", "git", "github", "code review", "peer review",
    "pull request", "collaborative development", "engineering best practices",
    "version-controlled", "code repository",
]
GIT_RESUME_SIGNALS = [
    "git", "github", "version control", "pull request", "code review", "peer review",
]

def check_v9_git_version_control(resume: dict, jd_text: str):
    if not jd_text:
        _pass("V9 Git/version control signal", "skipped (no --jd arg)")
        return

    jd_lower = jd_text.lower()
    if not any(sig in jd_lower for sig in GIT_JD_SIGNALS):
        _pass("V9 Git/version control signal", "JD does not mention version control/GitHub")
        return

    # Scope: work_history bullets + skills list + core_expertise.
    # Summary is positioning — Git belongs in the technical inventory (skills) or delivery bullets.
    all_bullets = [b for r in resume.get("work_history", []) for b in r.get("bullets", [])]
    skills_str = " ".join(resume.get("skills", []) + resume.get("core_expertise", []))
    all_text = (" ".join(all_bullets) + " " + skills_str).lower()

    if any(sig in all_text for sig in GIT_RESUME_SIGNALS):
        _pass("V9 Git/version control signal",
              "JD has Git/VC signal; Git/GitHub present in bullets/skills")
    else:
        _fail(
            "V9 Git/version control signal",
            "JD mentions version control/GitHub/code review but no Git signal found in "
            "bullets or skills list.",
            "Add Git/GitHub to skills list when JD mentions version control, or include a "
            "bullet referencing Git workflows in data/experience_bank.md.",
        )


# ── V10 — Investment/hedge fund domain: Mutual Fund experience in summary or cover ──
INVESTMENT_JD_SIGNALS = [
    "hedge fund", "asset management", "investment management", "asset manager",
    "quant finance", "fund manager", "wealth management", "private equity", "investment bank",
    "portfolio management", "quantitative finance", "trading strategy", "fixed income", "equities",
]
MUTUAL_FUND_SIGNALS = ["mutual fund", "fund investor", "portfolio management experience"]

def check_v10_investment_domain(resume: dict, cover: dict, jd_text: str):
    if not jd_text:
        _pass("V10 Investment domain MF check", "skipped (no --jd arg)")
        return

    jd_lower = jd_text.lower()
    if not any(sig in jd_lower for sig in INVESTMENT_JD_SIGNALS):
        _pass("V10 Investment domain MF check", "Not an investment-domain JD — skip")
        return

    summary    = resume.get("summary", "").lower()
    cover_text = " ".join(cover.get("paragraphs", [])).lower()
    combined   = summary + " " + cover_text

    if any(sig in combined for sig in MUTUAL_FUND_SIGNALS):
        _pass("V10 Investment domain MF check",
              "Investment JD detected; Mutual Fund experience present in summary/cover")
    else:
        _fail(
            "V10 Investment domain MF check",
            "Investment/finance domain JD detected but Mutual Fund experience missing from "
            "resume summary and cover letter.",
            "STEP 3: add closing sentence to summary about 7+ years personal Mutual Fund "
            "investment experience. STEP 5: add 1-2 sentences in Para 3 about domain fluency."
        )


# ── V11 — Per-role proportional quantification check (Hard Fail) ─────────────
# Bank ratios derived from strict impact-only classification of experience_bank.md:
#   Only %, outcome deltas (from X to Y), and accuracy figures count.
#   Team headcounts, stage counts, component counts = descriptors, NOT impact.
ROLE_BANK_RATIOS = {
    "independent":       (0, 3),   # skip — no impact bullets in bank
    "flipkart":          (4, 10),  # 40% — Extrasaver 95%/90% reclassified as operational descriptor
    "beepkart":          (4, 7),   # 57%
    "dehaat":            (1, 6),   # 17% — only the "30% reduction" bullet qualifies
    "quinbay":           (0, 6),   # skip — no impact bullets in bank
    "coviam technology": (1, 3),   # 33% — only the "85% accuracy" XGBoost bullet
}
IMPACT_RE = re.compile(r'\d+%|from \d+ to \d+|\d+x\b', re.IGNORECASE)


def _bank_ratio(company_name: str):
    key = re.sub(r'[^a-z ]', '', company_name.lower()).strip()
    for k, v in ROLE_BANK_RATIOS.items():
        if k in key or key in k:
            return v
    return None  # unknown company — skip


def check_v11_role_quantification(resume: dict):
    failures = []
    for role in resume.get("work_history", []):
        ratio_pair = _bank_ratio(role.get("company", ""))
        if ratio_pair is None:
            continue
        bank_q, bank_total = ratio_pair
        if bank_q == 0:
            continue  # Independent / Quinbay — no impact bullets in bank, skip
        bullets = role.get("bullets", [])
        n = len(bullets)
        expected_min = max(1, round(n * bank_q / bank_total))
        actual = sum(1 for b in bullets if IMPACT_RE.search(b))
        if actual < expected_min:
            failures.append(
                f"{role.get('company')} / {role.get('role')}: "
                f"{actual}/{n} impact bullets (expected ≥{expected_min} "
                f"based on {bank_q}/{bank_total} bank ratio)"
            )
    if not failures:
        _pass("V11 Per-role quantification", "All roles meet proportional impact-bullet threshold")
    else:
        _fail("V11 Per-role quantification",
              "\n     ".join(failures),
              "Swap in at least one impact bullet (with %, delta, or outcome metric) per failing role")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Pre-render prep validation")
    parser.add_argument("--resume",    required=True, help="Path to final resume JSON")
    parser.add_argument("--cover",     required=True, help="Path to final cover letter JSON")
    parser.add_argument("--jd",        default="",    help="Path to JD text file (optional)")
    parser.add_argument("--company",   default="",    help="Company name (optional, for V4)")
    parser.add_argument("--skip-v10",  action="store_true",
                        help="Skip V10 investment domain check (use when JD is investment-adjacent "
                             "but MF hook correctly omitted due to framing judgement)")
    args = parser.parse_args()

    resume_path = Path(args.resume)
    cover_path  = Path(args.cover)

    if not resume_path.exists():
        print(f"ERROR: resume file not found: {resume_path}")
        sys.exit(1)
    if not cover_path.exists():
        print(f"ERROR: cover file not found: {cover_path}")
        sys.exit(1)

    try:
        resume = json.loads(resume_path.read_text())
    except json.JSONDecodeError as e:
        print(f"ERROR: invalid JSON in resume: {e}")
        sys.exit(1)
    try:
        cover = json.loads(cover_path.read_text())
    except json.JSONDecodeError as e:
        print(f"ERROR: invalid JSON in cover: {e}")
        sys.exit(1)

    jd_text = ""
    if args.jd and Path(args.jd).exists():
        jd_text = Path(args.jd).read_text()

    company = args.company.strip()

    print(f"\n[validate_prep] Pre-render Validation\n")
    check_v1_ai_fluency(resume)
    check_v2_excluded_tools(resume, cover)
    check_v3_word_count(cover)
    check_v4_company_name(cover, company)
    check_v5_mmm_framing(resume, cover, jd_text)
    check_v6_no_fill_me(resume, cover)
    check_v7_metrics(resume, cover)
    check_v8_agile_jira(resume, jd_text)
    check_v9_git_version_control(resume, jd_text)
    if args.skip_v10:
        _pass("V10 Investment domain MF check", "skipped via --skip-v10")
    else:
        check_v10_investment_domain(resume, cover, jd_text)
    check_v11_role_quantification(resume)

    total = len(PASSED) + len(FAILED)
    print()
    if not FAILED:
        print(f"  ✓ All {total} checks passed — safe to render PDFs")
    else:
        print(f"  ✗ {len(FAILED)} of {total} checks FAILED — fix before rendering")
    print()
    sys.exit(0 if not FAILED else 1)


if __name__ == "__main__":
    main()
