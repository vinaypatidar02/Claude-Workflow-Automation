#!/usr/bin/env python3
"""
classify_title.py — Reusable title tier classifier for retroactive batch checks.

NOT used in the live scout pipeline (Claude LLM handles scoring there).
Use this for: post-hoc audits, retroactive title corrections, one-off checks.

Returns (pts: int, reason: str) where pts maps to the 5-tier rubric:
  20 = Tier 1 — Manager / Lead / Head / Principal
  15 = Tier 2 — Senior non-Lead analytics role
  10 = Tier 3 — Senior Data Analyst or senior analytics role
   5 = Tier 4 — BA / DA / non-senior analyst
   0 = Tier 5 — Unrelated / unclear

CLI:
  python3 scripts/classify_title.py "Business Intelligence Manager"
  python3 scripts/classify_title.py --batch   (stdin, one title per line)
"""
import re
import sys


# ── Suppression guards ────────────────────────────────────────────────────────
# Roles that superficially look like Tier 1 but are above-target or non-analytics.

_VP_PATTERN  = re.compile(r'\b(vp|vice president|director|c-suite|chief|svp|evp|partner)\b')
_DATA_SCI_ENG = re.compile(
    r'\b(data scientist|data science|data engineer(?!ing)|ml engineer|'
    r'machine learning engineer|software engineer|devops|mlops)\b'
)

# ── Broadened analytics keyword set ──────────────────────────────────────────
# Covers full-form and abbreviated variants, UK/US spellings, adjacent domains.
_HAS_ANALYTICS = re.compile(
    r'\b(analytics|analyst|insights?|intelligence|growth|commercial|'
    r'performance|behavioural?|behavioral|reporting|crm)\b'
)

_HAS_LEAD_MGR = re.compile(r'\b(manager|lead|head|principal)\b')

# ── Tier 1 explicit patterns (titles that don't hit general has_analytics) ────
# "Head of Data" — "data" alone isn't in _HAS_ANALYTICS to avoid false positives
# (e.g. "Database Manager" would fire). Explicit patterns handle these edge cases.
_TIER1_EXPLICIT = [
    re.compile(r'\bhead of data\b'),         # Head of Data, Head of Data & Analytics
    re.compile(r'\bdata (team )?lead\b'),    # Data Lead, Data Team Lead
]

# ── Tier 2 explicit patterns (abbreviated AND full-form) ──────────────────────
_TIER2 = [
    re.compile(r'\bsenior (business|product|performance|insights?|bi|analytics|data quality) analyst\b'),
    re.compile(r'\bsenior analytics engineer\b'),
    re.compile(r'\b(bi|business intelligence) (manager|product lead|lead|analyst)\b'),
    re.compile(r'\bbusiness intelligence (manager|lead|head)\b'),
]

# ── Tier 4 explicit patterns ──────────────────────────────────────────────────
_TIER4 = [
    re.compile(
        r'\b(business|data|product|solutions?|credit|marketing|pricing|'
        r'commercial|operations?|risk|financial|hr|people) analyst\b'
    ),
    re.compile(r'\banalytics engineer\b'),   # plain (non-Senior) analytics engineer
    re.compile(r'\bstaff (data )?analyst\b'),
    re.compile(r'\bspecialist\b'),           # "Analytics Specialist", "Pricing and Analytics Specialist"
]

# ── AI Enablement role patterns ───────────────────────────────────────────────
# Roles where primary work is enabling AI workflows/practices org-wide.
# Must be checked BEFORE Tier-4 to prevent "specialist" keyword from blocking them.
_AI_KW = re.compile(r'\b(ai|artificial intelligence)\b')
_AI_ENABLEMENT_KW = re.compile(
    r'\b(specialist|accelerator|enabler|enablement|practitioner|'
    r'adoption|champion|advisor|practice)\b'
    r'|\bforward deployed\b'
)


def classify_title(title: str) -> tuple[int, str]:
    """Return (pts, reason) for the given job title."""
    t = title.lower()

    # Immediate reject: core non-analytics roles
    if _DATA_SCI_ENG.search(t):
        return 0, "Non-analytics role (Data Scientist / Data Engineer / ML Engineer)"

    is_vp_or_above = bool(_VP_PATTERN.search(t))
    is_architect   = "architect" in t
    is_governance  = "governance" in t

    has_analytics = bool(_HAS_ANALYTICS.search(t))
    has_lead_mgr  = bool(_HAS_LEAD_MGR.search(t))

    # Governance roles with data/AI context → always cap at Tier 4 (non-target leadership)
    if is_governance and (has_analytics or re.search(r'\b(data|ai)\b', t)):
        if has_lead_mgr:
            return 5, "Tier 4 (capped — Governance management role)"
        return 0, "Tier 5 — Governance (non-analytics)"

    # Explicit Tier 1 titles that don't hit the general has_analytics pattern
    for p in _TIER1_EXPLICIT:
        if p.search(t):
            return 20, "Tier 1 — Head of Data (explicit pattern)"

    # ── Product Manager suppression (before Tier 1) ───────────────────────────
    # "Senior Product Manager - Analytics", "AI Product Manager" are PM roles, not analytics leads.
    # "\bproduct manager\b" does NOT match "Product Analytics Manager" (word order differs — safe).
    if re.search(r'\bproduct manager\b', t):
        return 5, "Tier 4 (capped — Product Manager role, not analytics leadership)"

    # ── Tier 1: Manager / Lead / Head / Principal ─────────────────────────────
    if has_lead_mgr and has_analytics:
        if is_vp_or_above or is_architect or is_governance:
            # Cap: role has lead/mgr keyword but context is above-target or non-analytics
            if re.search(r'\bsenior\b', t):
                return 10, "Senior analytics (capped — VP/Architect/Governance context)"
            return 5, "Analyst (capped — VP/Architect/Governance context)"
        return 20, "Tier 1 — Manager / Lead / Head / Principal"

    # ── Tier 2: Senior non-Lead analytics roles ───────────────────────────────
    for p in _TIER2:
        if p.search(t):
            return 15, "Tier 2 — Senior non-Lead analytics role"

    # ── Tier 3: Senior Data Analyst or senior analytics without Lead/Mgr ──────
    if re.search(r'\bsenior data analyst\b', t):
        return 10, "Tier 3 — Senior Data Analyst"
    if re.search(r'\bsenior\b', t) and has_analytics:
        return 10, "Tier 3 — Senior analytics role"

    # ── AI Enablement Tier 2 ──────────────────────────────────────────────────
    # "AI & Automation Specialist", "Forward Deployed AI Accelerator", etc.
    # Must come before Tier-4 to intercept titles containing "specialist".
    if _AI_KW.search(t) and _AI_ENABLEMENT_KW.search(t):
        return 15, "Tier 2 — AI Enablement / AI Practice role"

    # ── Tier 4: Non-senior analyst / BA / DA ─────────────────────────────────
    for p in _TIER4:
        if p.search(t):
            return 5, "Tier 4 — Non-senior analyst / specialist"
    if re.search(r'\banalyst\b', t):
        return 5, "Tier 4 — Analyst (catch-all)"

    # ── Tier 5: Unrelated / unclear ───────────────────────────────────────────
    return 0, "Tier 5 — Unrelated / unclear"


def tier_label(pts: int) -> str:
    if pts >= 20: return "Tier1"
    if pts >= 15: return "Tier2"
    if pts >= 10: return "Tier3"
    if pts >= 5:  return "Tier4"
    return "Tier5"


def main():
    args = sys.argv[1:]

    if "--batch" in args:
        for line in sys.stdin:
            title = line.strip()
            if not title:
                continue
            pts, reason = classify_title(title)
            print(f"  {pts:>3}  {tier_label(pts)}  {title!r}  [{reason}]")
        return

    if args:
        title = " ".join(args)
        pts, reason = classify_title(title)
        print(f"\n  {pts:>3}  {tier_label(pts)}")
        print(f"  Title:  {title!r}")
        print(f"  Reason: {reason}\n")
        return

    # Default: run verification suite
    tests = [
        # Tier 1 — should all return 20
        ("Analytics Manager",                        20),
        ("Data Analytics Manager",                   20),
        ("Lead Data Analyst",                        20),
        ("Lead Business Analyst",                    20),
        ("Head of Data",                             20),
        ("Head of Analytics",                        20),
        ("Data Team Lead",                           20),
        ("Data Lead",                                20),
        ("Insights Manager",                         20),
        ("Commercial Growth and Insights Manager",   20),  # "insights" plural fix
        ("Growth Analytics Lead",                    20),
        ("CRM Analytics Manager",                    20),
        ("Reporting Manager",                        20),
        # Tier 2 — should return 15
        ("Business Intelligence Manager",            20),  # manager+intelligence → Tier 1 (correct)
        ("Senior Business Analyst",                  15),
        ("Senior Product Analyst",                   15),
        ("BI Manager",                               15),  # "bi" not in _HAS_ANALYTICS → Tier 2 explicit
        ("BI Lead",                                  15),  # "bi" not in _HAS_ANALYTICS → Tier 2 explicit
        ("Business Intelligence Lead",               20),  # lead+intelligence → Tier 1 (correct)
        ("Senior Analytics Engineer",                15),
        # Tier 3 — should return 10
        ("Senior Data Analyst",                      10),
        ("Senior Solutions Analyst",                 10),
        ("Senior Data & Analytics Consultant",       10),
        # Tier 4 — should return 5
        ("Analytics Engineer",                        5),
        ("Business Analyst",                          5),
        ("Data Analyst",                              5),
        ("AI Product Analyst",                        5),
        ("Staff Data Analyst",                        5),
        ("Pricing and Analytics Specialist",          5),
        # Tier 5 — should return 0
        ("Data Engineer",                             0),
        ("Data Scientist",                            0),
        ("Senior Business Solution Architect",        0),
        ("Director, Sales Analytics",                 0),  # VP/Director suppression
        # Suppressed (Governance / Architect / VP context)
        ("Data & AI Governance Manager",              5),  # capped at Tier 4
        ("SAP Data & Analytics Architect Senior Manager", 10),  # capped at Tier 3 (senior)
        ("CCOR Data Analytics - Product manager - Vice President", 5),  # capped at Tier 4
        # AI Enablement — Tier 2
        ("AI & Automation Specialist",               15),  # AI + specialist → Tier 2
        ("Forward Deployed AI Accelerator",          15),  # forward deployed → Tier 2
        ("AI Enablement Specialist",                 15),  # AI + enablement + specialist → Tier 2
        # AI roles that should NOT hit AI enablement path
        ("AI Product Analyst",                        5),  # no enablement keyword → Tier 4
        ("AI Analytics Manager",                     20),  # manager + analytics → Tier 1 (unchanged)
        # Product Manager suppression — should be Tier 4 regardless of analytics suffix
        ("Senior Product Manager - Analytics",        5),  # analytics suffix should not elevate to Tier 1
        ("AI Product Manager",                        5),  # "manager" alone insufficient without analytics
        ("Product Analytics Manager",                20),  # NOT "product manager" phrase — still Tier 1
        # Director suppression (TITLE_REJECT_CONTAINS catches these before classify_title,
        # but verify classify_title itself returns sensible values too)
        ("Analytics Director",                        0),  # VP_PATTERN suppresses → Tier 5
        ("Associate Director of Analytics",           0),  # VP_PATTERN suppresses → Tier 5
    ]

    passed = failed = 0
    print(f"\n{'─'*70}")
    print(f" classify_title.py — Verification Suite ({len(tests)} tests)")
    print(f"{'─'*70}")
    for title, expected in tests:
        pts, reason = classify_title(title)
        ok = pts == expected
        icon = "✓" if ok else "✗"
        if ok:
            passed += 1
        else:
            failed += 1
        print(f"  {icon}  [{pts:>3} exp={expected:>3}]  {tier_label(pts):<7}  {title!r}")
        if not ok:
            print(f"         → {reason}")
    print(f"{'─'*70}")
    print(f"  {passed}/{len(tests)} passed" + (f"  ({failed} FAILED)" if failed else " — all clear"))
    print(f"{'─'*70}\n")


if __name__ == "__main__":
    main()
