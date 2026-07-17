#!/usr/bin/env python3
"""
auto_prep.py — Mechanical pre-fill for the job prep step.

Applies all deterministic rules from experience_bank.md and cover_letter_bank.md
before the LLM runs. The LLM then only needs to write:
  - summary (4 sentences, JD-specific)
  - Para 1 company hook (1-2 sentences naming company + role)
  - Para 4 (70 words, company mission + call to action)

Usage:
  python3 scripts/auto_prep.py --job_id <id> --jd_file <path/to/jd.txt>
  python3 scripts/auto_prep.py --job_id <id> --jd_file <path> --domain product
  python3 scripts/auto_prep.py --job_id <id> --jd_file <path> --company "Acme Ltd"

Outputs written to data/prep_tmp/:
  data/prep_tmp/auto_resume_<job_id>.json  — full resume JSON; summary = "FILL_ME"
  data/prep_tmp/auto_cover_<job_id>.json   — cover JSON; paragraphs[0] ends with hook placeholder,
                                             paragraphs[3] = "FILL_ME"
"""

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent


# ─────────────────────────────────────────────────────────────────────────────
# STATIC PROFILE DATA
# ─────────────────────────────────────────────────────────────────────────────

CONTACT = {
    "email":    "vinay_patidar02@yahoo.com",
    "phone":    "+91 XXXXX XXXXX",
    "linkedin": "linkedin.com/in/vinay-patidar-vp02",
    "github":   "github.com/vinaypatidar02",
    "address":  "Bengaluru, Karnataka 560035",
}

EDUCATION = [{
    "degree": "Mining Engineering, B.Tech",
    "institution": "IIT (BHU), Varanasi",
    "dates": "2012 – 2016",
    "gpa": "7.67/10",
}]

CERTIFICATIONS = [
    "Claude 101 — Anthropic Skilljar (2026)",
    "Claude Code 101 — Anthropic Skilljar (2026)",
    "Data Science using SAS and R — Analytix Labs",
    "Managing Big Data with MySQL — Coursera",
    "Data Visualization with Tableau — Coursera",
    "Mastering Data Analysis in Excel — Coursera",
]

# Fixed role metadata in chronological order.
# bank_key must substring-match the ## heading in experience_bank.md.
ROLE_METADATA = [
    {
        "company": "Independent / Personal Project",
        "location": "Bengaluru",
        "role": "Analytics & AI Engineering (Self-directed)",
        "dates": "2026-03-present",
        "bank_key": "Analytics & AI Engineering",
        "pinned": True,   # always include ALL bullets, no trimming
        "max_bullets": 3,
    },
    {
        "company": "Flipkart",
        "location": "Bengaluru",
        "role": "Lead Business Analyst",
        "dates": "2025-04-2026-03",
        "bank_key": "Flipkart",
        "max_bullets": 6,
    },
    {
        "company": "BeepKart",
        "location": "Bengaluru",
        "role": "Analytics Manager",
        "dates": "2023-07-2024-11",
        "bank_key": "BeepKart",
        "max_bullets": 5,
        "featured_link": {
            "text": "Dynamic Pricing Algorithm — Published by BeepKart COO on Medium",
            "url":  "https://medium.com/@abhisheksaraf_44597/the-art-and-science-of-dynamic-pricing-how-we-built-an-algorithm-for-450-vehicle-models-d691a09361d2",
        },
    },
    {
        "company": "DeHaat",
        "location": "Bengaluru",
        "role": "Lead Business Analyst",
        "dates": "2021-07-2023-07",
        "bank_key": "DeHaat",
        "max_bullets": 5,
    },
    {
        "company": "Quinbay / Coviam Technology",
        "location": "Bengaluru",
        "role": "Senior Data Analyst",
        "dates": "2020-10-2021-07",
        "bank_key": "Quinbay",
        "max_bullets": 3,
    },
    {
        "company": "Coviam Technology",
        "location": "Bengaluru",
        "role": "Data Analyst",
        "dates": "2017-05-2020-09",
        "bank_key": "Coviam Technology",
        "max_bullets": 2,
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# DOMAIN DETECTION
# ─────────────────────────────────────────────────────────────────────────────

DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "product": [
        "product analytics", "growth analytics", "experimentation", "a/b test",
        "ecommerce", "conversion", "saas", "funnel", "activation", "marketplace",
        "user behaviour", "user behavior", "feature adoption", "product metrics",
        "retention", "engagement", "dau", "mau",
    ],
    "crm": [
        "crm", "lifecycle", "propensity", "segmentation", "campaign analytics",
        "customer analytics", "churn", "reactivation", "loyalty", "customer growth",
        "customer journey", "lifecycle management", "lifecycle analytics", "lapsed",
        "customer retention",
    ],
    "pricing": [
        "pricing", "commercial analytics", "procurement", "margin analysis",
        "inventory optimisation", "p&l", "revenue analytics", "financial modelling",
        "commercial strategy", "revenue operations",
    ],
    "bi": [
        "business intelligence", "bi ", "kpi framework", "kpi strategy",
        "insight", "self-serve analytics", "data visualisation", "data visualization",
        "semantic layer", "certified metrics", "data literacy", "dashboard",
        "reporting infrastructure", "data governance",
    ],
}

LEADERSHIP_KEYWORDS: list[str] = [
    "people management", "manage a team", "managing a team", "line manage",
    "build a team", "head of analytics", "head of data", "mentoring analyst",
    "people manager", "team of analyst", "lead a team", "leadership of",
    "manage analyst", "direct reports", "line reports", "reporting to you",
    "lead, coach", "coach and develop", "leading a team", "building a team",
    "team of data scientist", "team of analyst", "grow a team",
]


INVESTMENT_KEYWORDS: list[str] = [
    "hedge fund", "asset management", "investment management", "quant",
    "portfolio management", "private equity", "investment bank",
    "wealth management", "fixed income", "equities", "trading",
    "fund manager", "asset manager", "quantitative finance",
]


def detect_domain(jd: str) -> str:
    jd_lower = jd.lower()
    scores: dict[str, int] = {d: 0 for d in DOMAIN_KEYWORDS}
    for domain, keywords in DOMAIN_KEYWORDS.items():
        scores[domain] = sum(1 for kw in keywords if kw in jd_lower)
    top = max(scores, key=scores.get)
    if scores[top] == 0:
        print("[auto_prep] ⚠ WARNING: No domain keywords matched in JD — "
              "defaulting to 'general'. Pass --domain to override.")
        return "general"
    return top


def is_leadership_jd(jd: str) -> bool:
    jd_lower = jd.lower()
    return any(kw in jd_lower for kw in LEADERSHIP_KEYWORDS)


def is_investment_jd_fallback(jd: str) -> bool:
    """Keyword fallback for investment domain — used when tracker flag is absent."""
    jd_lower = jd.lower()
    return any(kw in jd_lower for kw in INVESTMENT_KEYWORDS)


# ─────────────────────────────────────────────────────────────────────────────
# EXPERTISE SELECTION  (STEP 1c rules)
# ─────────────────────────────────────────────────────────────────────────────

DOMAIN_EXPERTISE: dict[str, dict[str, list[str]]] = {
    "product": {
        "primary":   ["Product Analytics", "Growth Analytics",
                      "Experimentation & Incrementality Testing"],
        "secondary": ["Behavioral Analytics", "Conversion Optimization",
                      "Stakeholder Management"],
    },
    "crm": {
        "primary":   ["CRM Analytics", "Customer Lifecycle Analytics", "Retention Analytics"],
        "secondary": ["Customer Segmentation", "Propensity Modeling", "Growth Analytics"],
    },
    "pricing": {
        "primary":   ["Pricing & Commercial Optimization", "Strategic Decision-Making",
                      "Analytics Transformation"],
        "secondary": ["KPI Strategy & Business Intelligence", "Stakeholder Management"],
    },
    "bi": {
        "primary":   ["KPI Strategy & Business Intelligence", "Analytics Transformation"],
        "secondary": ["Stakeholder Management", "Strategic Decision-Making",
                      "Product Analytics", "Growth Analytics"],
    },
    "general": {
        "primary":   ["Strategic Decision-Making", "Product Analytics",
                      "Growth Analytics", "Pricing & Commercial Optimization"],
        "secondary": ["KPI Strategy & Business Intelligence", "Analytics Transformation",
                      "Stakeholder Management"],
    },
}
PINNED_EXPERTISE = ["AI Workflow Automation", "Agentic Analytics Engineering"]
LEADERSHIP_EXPERTISE = ["Stakeholder Management", "Strategic Decision-Making"]


def select_expertise(domain: str, is_leadership: bool) -> list[str]:
    pools = DOMAIN_EXPERTISE.get(domain, DOMAIN_EXPERTISE["bi"])
    selected: list[str] = list(pools["primary"])

    if is_leadership:
        for item in LEADERSHIP_EXPERTISE:
            if item not in selected:
                selected.append(item)

    # Fill with secondary up to 8 domain items (leaving room for 2 pinned)
    for item in pools["secondary"]:
        if len(selected) >= 8:
            break
        if item not in selected:
            selected.append(item)

    for item in PINNED_EXPERTISE:
        if item not in selected:
            selected.append(item)

    return selected[:10]


# ─────────────────────────────────────────────────────────────────────────────
# SKILLS SELECTION  (STEP 1c rules)
# ─────────────────────────────────────────────────────────────────────────────

CORE_SKILLS = ["SQL", "Python", "Tableau", "BigQuery", "Amazon Redshift", "Looker Studio"]
PINNED_SKILLS = ["Claude Code", "Anthropic API"]
CONDITIONAL_SKILLS: dict[str, list[str]] = {
    "XGBoost": [
        "ml model", "machine learning", "classification", "prediction model",
        "xgboost", "gradient boosting",
    ],
    "Prompt Engineering": [
        "llm", "genai", "generative ai", "prompt", "language model",
        "ai tool", "large language", "natural language",
    ],
    "MCP Servers": [
        "mcp", "agentic", "workflow automation", "ai tooling", "orchestration",
    ],
    "Git / GitHub": [
        "git", "github", "version control", "code review", "peer review",
        "pull request", "collaborative development", "version-controlled",
    ],
}
EXCLUDED_MENTIONS = ["google analytics", "firebase", "apache airflow"]

# "bi" and "data-eng" deliberately excluded — they contribute 0 to diversity gen_s.
# A bi/data-eng bullet can only rank up in the diversity pool via JD keyword matches.
ALL_ANALYTICS_TAGS = {"product", "growth", "experimentation", "CRM", "pricing", "ai"}


def detect_conditional_skills(jd: str) -> list[str]:
    jd_lower = jd.lower()
    return [s for s, kws in CONDITIONAL_SKILLS.items() if any(kw in jd_lower for kw in kws)]


def select_skills(conditional: list[str]) -> list[str]:
    return CORE_SKILLS + conditional + PINNED_SKILLS


# ─────────────────────────────────────────────────────────────────────────────
# TITLE LINES
# ─────────────────────────────────────────────────────────────────────────────

TITLE_LINES: dict[tuple, list[str]] = {
    ("product",  False): ["Lead Analytics Professional", "Product · Growth · Experimentation"],
    ("product",  True):  ["Analytics Lead & Manager",   "Product · Experimentation · AI"],
    ("crm",      False): ["Lead Analytics Professional", "CRM · Retention · Lifecycle"],
    ("crm",      True):  ["Analytics Lead & Manager",   "CRM · Lifecycle · AI"],
    ("pricing",  False): ["Lead Analytics Professional", "Pricing · Commercial · Analytics"],
    ("pricing",  True):  ["Analytics Lead & Manager",   "Commercial Analytics · Leadership · AI"],
    ("bi",       False): ["Lead Analytics Professional", "Insights · BI · AI"],
    ("bi",       True):  ["Analytics Lead & Manager",   "Insights · Leadership · AI"],
    ("general",  False): ["Lead Analytics Professional", "Analytics · Strategy · AI"],
    ("general",  True):  ["Analytics Lead & Manager",   "Analytics · Leadership · AI"],
}


def select_title_lines(domain: str, is_leadership: bool) -> list[str]:
    return TITLE_LINES.get((domain, is_leadership), TITLE_LINES[("product", False)])


# ─────────────────────────────────────────────────────────────────────────────
# EXPERIENCE BANK PARSING
# ─────────────────────────────────────────────────────────────────────────────

# Bullet tags that map to each detected domain (for scoring)
DOMAIN_BULLET_TAGS: dict[str, set[str]] = {
    "product":  {"product", "growth", "experimentation"},
    "crm":      {"CRM", "growth", "product"},
    "pricing":  {"pricing", "bi"},
    "bi":       {"bi", "data-eng"},
    "general":  {"product", "growth", "CRM", "pricing", "experimentation", "leadership"},
}


def parse_bullet_line(line: str) -> tuple[set, str] | None:
    """Parse '- [tag1] [tag2] bullet text' → (tags, text). Returns None if not a bullet."""
    line = line.strip()
    if not line.startswith("- ["):
        return None
    body = line[2:].strip()   # strip leading "- "
    tags: set[str] = set()
    while body.startswith("["):
        close = body.find("]")
        if close < 0:
            break
        tag = body[1:close]
        if re.match(r"^[\w-]+$", tag):
            tags.add(tag)
        body = body[close + 1:].strip()
    if not body:
        return None
    return tags, body


def mentions_excluded(text: str) -> bool:
    t = text.lower()
    return any(e in t for e in EXCLUDED_MENTIONS)


IMPACT_RE_AUTO = re.compile(r'\d+%|from \d+ to \d+|\d+x\b', re.IGNORECASE)

# (bank_impact_bullets, bank_total) — mirrors ROLE_BANK_RATIOS in validate_prep.py
ROLE_IMPACT_FLOOR = {
    "independent":       (0, 3),   # skip — no impact bullets in bank
    "flipkart":          (4, 10),  # Extrasaver 95%/90% reclassified as operational descriptor
    "beepkart":          (4, 7),
    "dehaat":            (1, 6),
    "quinbay":           (0, 6),   # skip — no impact bullets in bank
    "coviam technology": (1, 3),
}


def score_bullet(tags: set, domain_tags: set, is_leadership: bool) -> int:
    s = len(tags & domain_tags)
    if is_leadership and "leadership" in tags:
        s += 4
    return s


def parse_experience_bank(path: Path) -> dict[str, list[dict]]:
    """Returns {bank_key: [{tags, text}, ...]} for each role section."""
    content = path.read_text(encoding="utf-8")
    # Split on role section headings
    parts = re.split(r"\n## ", "\n" + content)
    result: dict[str, list[dict]] = {}

    for part in parts[1:]:
        lines = part.split("\n")
        header = lines[0].strip()

        matched_key: str | None = None
        for meta in ROLE_METADATA:
            if meta["bank_key"] in header:
                matched_key = meta["bank_key"]
                break
        if not matched_key:
            continue

        bullets = []
        for line in lines[1:]:
            parsed = parse_bullet_line(line)
            if parsed:
                tags, text = parsed
                bullets.append({"tags": tags, "text": text})

        result[matched_key] = bullets

    return result


def select_bullets_for_role(
    bank_bullets: list[dict],
    max_n: int,
    domain_tags: set,
    is_leadership: bool,
    pinned: bool = False,
    jd_text: str = "",
    company_key: str = "",
) -> tuple[list[str], list[dict]]:
    """Returns (selected_bullets, score_export).
    score_export is a list of all non-excluded bullets with their scores,
    sorted descending, for use by eval_prep.py D2 check.
    """
    if pinned:
        texts = [b["text"] for b in bank_bullets if not mentions_excluded(b["text"])]
        return texts, []

    # JD keyword set for combined scoring (5+ char words)
    jd_words = {w.lower() for w in re.findall(r'\b[a-z]{5,}\b', jd_text.lower())} if jd_text else set()

    def _jd_kw(text: str) -> int:
        return sum(1 for w in jd_words if w in text.lower())

    domain_pool: list[tuple] = []   # domain score >= 1
    other_pool:  list[tuple] = []   # domain score == 0, not excluded

    for b in bank_bullets:
        if mentions_excluded(b["text"]):
            continue
        s = score_bullet(b["tags"], domain_tags, is_leadership)
        kw = _jd_kw(b["text"])
        if s >= 1:
            domain_pool.append((s, kw, b["text"], b["tags"]))
        else:
            gen_s = len(b["tags"] & ALL_ANALYTICS_TAGS)
            if is_leadership and "leadership" in b["tags"]:
                gen_s += 2
            other_pool.append((gen_s, kw, b["text"], b["tags"]))

    # Primary: domain score desc, jd_kw breaks ties
    domain_pool.sort(key=lambda x: (x[0], x[1]), reverse=True)
    # Diversity: combined gen_s + jd_kw — lets JD words overcome bi/data-eng disadvantage
    other_pool.sort(key=lambda x: x[0] + x[1], reverse=True)

    diversity_n = max_n // 2
    primary_n   = max_n - diversity_n

    # Pass 1 — primary (domain-relevant); track as (text, score) tuples
    primary_scored = [(t, s) for s, _, t, _ in domain_pool[:primary_n]]

    # Pass 2 — diversity (breadth); expands if primary ran short
    actual_div = diversity_n + (primary_n - len(primary_scored))
    diversity_scored = [(t, gen_s) for gen_s, _, t, _ in other_pool[:actual_div]]

    # Pass 3 — fill any remaining gap from leftover domain pool
    all_selected = {t for t, _ in primary_scored + diversity_scored}
    for s, _, t, _ in domain_pool[primary_n:]:
        if len(primary_scored) + len(diversity_scored) >= max_n:
            break
        if t not in all_selected:
            diversity_scored.append((t, s))
            all_selected.add(t)

    selected_scored: list[tuple[str, int]] = primary_scored + diversity_scored

    # Pass 4 — Impact floor: ensure proportional representation of impact bullets
    ck = company_key.lower().strip()
    floor_pair = ROLE_IMPACT_FLOOR.get(ck)
    if floor_pair and floor_pair[0] > 0:
        bank_q, bank_total = floor_pair
        n = len(selected_scored)
        expected_impact = max(1, round(n * bank_q / bank_total))
        actual_impact   = sum(1 for t, _ in selected_scored if IMPACT_RE_AUTO.search(t))

        if actual_impact < expected_impact:
            sel_texts = {t for t, _ in selected_scored}
            # Highest-scoring unselected impact bullets (descending)
            remaining_impact = sorted(
                [b for b in bank_bullets
                 if b["text"] not in sel_texts and IMPACT_RE_AUTO.search(b["text"])
                 and not mentions_excluded(b["text"])],
                key=lambda b: score_bullet(b["tags"], domain_tags, is_leadership),
                reverse=True,
            )
            # Lowest-scoring non-impact selected bullets (ascending by score)
            non_impact = sorted(
                [(t, s) for t, s in selected_scored if not IMPACT_RE_AUTO.search(t)],
                key=lambda x: x[1],
            )
            for cand, victim in zip(remaining_impact, non_impact):
                if actual_impact >= expected_impact:
                    break
                selected_scored.remove(victim)
                selected_scored.append(
                    (cand["text"], score_bullet(cand["tags"], domain_tags, is_leadership))
                )
                actual_impact += 1

    selected = [t for t, _ in selected_scored]
    selected_set = set(selected)

    if not selected and bank_bullets:
        print(f"[auto_prep] ⚠ WARNING: all bullets for this role mention excluded tools "
              f"(Google Analytics / Firebase / Apache Airflow). Role will have 0 bullets. "
              f"Add clean bullets to experience_bank.md for this role.")

    # Build score_export for eval_prep.py D2 check
    score_export = []
    for b in bank_bullets:
        if mentions_excluded(b["text"]):
            continue
        s = score_bullet(b["tags"], domain_tags, is_leadership)
        score_export.append({
            "score":    s,
            "text":     b["text"],
            "tags":     sorted(b["tags"]),
            "selected": b["text"] in selected_set,
        })
    score_export.sort(key=lambda x: x["score"], reverse=True)

    return selected, score_export


def build_work_history(
    bank: dict[str, list[dict]], domain: str, is_leadership: bool, jd_text: str = ""
) -> tuple[list[dict], str, dict, list[str]]:
    """Returns (work_history, primary_company, bullet_scores_map, jd_keywords).
    bullet_scores_map: {bank_key: [{score, text, tags, selected}]} for eval_prep.py D2.
    jd_keywords: sorted list of 5+ char JD words used as tiebreakers in bullet selection.
    """
    domain_tags = DOMAIN_BULLET_TAGS.get(domain, {"product", "growth"})
    work_history = []
    primary_score = -1
    primary_company = "flipkart"
    bullet_scores_map: dict[str, list[dict]] = {}

    # Compute jd_keywords (mirrors select_bullets_for_role tiebreaker logic)
    jd_words = (
        {w.lower() for w in re.findall(r'\b[a-z]{5,}\b', jd_text.lower())}
        if jd_text else set()
    )

    for meta in ROLE_METADATA:
        key = meta["bank_key"]
        bank_bullets = bank.get(key, [])
        pinned = meta.get("pinned", False)

        bullets, score_export = select_bullets_for_role(
            bank_bullets, meta["max_bullets"], domain_tags, is_leadership, pinned,
            jd_text=jd_text,
            company_key=key,
        )
        if not bullets:
            bullets = [f"[Add bullets for {meta['company']} to experience_bank.md]"]

        if not pinned and score_export:
            bullet_scores_map[key] = score_export

        # Track which non-pinned role has the strongest first bullet
        if not pinned and bullets and bank_bullets:
            for b in bank_bullets:
                if b["text"] == bullets[0]:
                    top_s = score_bullet(b["tags"], domain_tags, is_leadership)
                    if top_s > primary_score:
                        primary_score = top_s
                        primary_company = meta["company"].split("/")[0].strip().lower()
                    break

        entry = {
            "company": meta["company"],
            "location": meta["location"],
            "role": meta["role"],
            "dates": meta["dates"],
            "bullets": bullets,
        }
        if meta.get("featured_link"):
            entry["featured_link"] = meta["featured_link"]
        work_history.append(entry)

    return work_history, primary_company, bullet_scores_map, sorted(jd_words)


# ─────────────────────────────────────────────────────────────────────────────
# COVER LETTER BANK PARSING
# ─────────────────────────────────────────────────────────────────────────────



def _extract_narrative_from_lines(lines: list[str]) -> str:
    """Collect paragraph text up to next heading or comment marker."""
    paras = []
    current: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("<!--") or stripped.startswith("##") or stripped.startswith("# ─"):
            break
        if stripped == "" and current:
            paras.append(" ".join(current))
            current = []
        elif stripped:
            current.append(stripped)
    if current:
        paras.append(" ".join(current))
    return " ".join(paras).strip()


def parse_cover_letter_bank(path: Path) -> dict[str, dict[str, str]]:
    """Returns {"section1": {...}, "section2": {...}, "section3": {...}}."""
    content = path.read_text(encoding="utf-8")

    # Locate the three section boundaries
    s1 = re.search(r"# ─+\n# SECTION 1", content)
    s2 = re.search(r"# ─+\n# SECTION 2", content)
    s3 = re.search(r"# ─+\n# SECTION 3", content)

    if not (s1 and s2 and s3):
        print("[auto_prep] WARNING: Could not parse cover_letter_bank.md — sections missing",
              file=sys.stderr)
        return {"section1": {}, "section2": {}, "section3": {}}

    s1_text = content[s1.start(): s2.start()]
    s2_text = content[s2.start(): s3.start()]
    s3_text = content[s3.start():]

    return {
        "section1": _parse_section1(s1_text),
        "section2": _parse_section2(s2_text),
        "section3": _parse_section3(s3_text),
    }


def _parse_section1(text: str) -> dict[str, str]:
    """Parse Para 2 narratives. Keys: '<company>_<tag>' and '<company>'."""
    result: dict[str, str] = {}
    parts = re.split(r"\n## ", text)
    for part in parts[1:]:
        lines = part.split("\n")
        header = lines[0].strip()
        # "Company: Flipkart | [growth][experimentation][CRM] Grocery SOT ..."
        m = re.match(r"Company:\s*(\w+)\s*\|", header)
        if not m:
            continue
        company = m.group(1).lower()
        tags = {t.lower() for t in re.findall(r"\[(\w[\w-]*)\]", header)}
        narrative = _extract_narrative_from_lines(lines[1:])
        if not narrative:
            continue
        for tag in tags:
            result.setdefault(f"{company}_{tag}", narrative)
        result.setdefault(company, narrative)
    return result


def _parse_section2(text: str) -> dict[str, str]:
    """Parse Para 3 themes. Keys: 'theme_<tag>'."""
    result: dict[str, str] = {}
    parts = re.split(r"\n## ", text)
    for part in parts[1:]:
        lines = part.split("\n")
        header = lines[0].strip()
        tags = {t.lower() for t in re.findall(r"\[(\w[\w-]*)\]", header)}
        narrative = _extract_narrative_from_lines(lines[1:])
        if not narrative:
            continue
        for tag in tags:
            result.setdefault(f"theme_{tag}", narrative)
    return result


def _parse_section3(text: str) -> dict[str, str]:
    """Parse Para 1 openers. Keys: domain name from primary tag."""
    result: dict[str, str] = {}
    parts = re.split(r"\n## ", text)
    for part in parts[1:]:
        lines = part.split("\n")
        header = lines[0].strip()
        tags = {t.lower() for t in re.findall(r"\[(\w[\w-]*)\]", header)}
        opener = _extract_narrative_from_lines(lines[1:])
        if not opener:
            continue
        for tag in tags:
            result[tag] = opener
    return result




def select_para1_opener(s3: dict[str, str], domain: str) -> str:
    return (
        s3.get(domain)
        or s3.get("product")
        or next(iter(s3.values()), "[Para 1 opener — write fresh]")
    )


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Mechanical pre-fill for job prep step")
    parser.add_argument("--job_id", required=True, help="Job tracker ID (e.g. app_042)")
    parser.add_argument("--jd_file", required=True, help="Path to file containing JD text")
    parser.add_argument("--domain", default=None,
                        help="Override domain detection: product|crm|pricing|bi|general")
    parser.add_argument("--company", default=None,
                        help="Company display name for cover letter recipient line")
    args = parser.parse_args()

    jd_path = Path(args.jd_file)
    if not jd_path.exists():
        print(f"[auto_prep] ERROR: JD file not found: {jd_path}", file=sys.stderr)
        sys.exit(1)
    jd_text = jd_path.read_text(encoding="utf-8")

    # ── Domain analysis ───────────────────────────────────────────────────────
    domain = args.domain or detect_domain(jd_text)
    is_leadership = is_leadership_jd(jd_text)
    conditional = detect_conditional_skills(jd_text)

    print(f"[auto_prep] job_id={args.job_id}  domain={domain}  "
          f"leadership={is_leadership}  conditional_skills={conditional}")

    # ── Load banks ────────────────────────────────────────────────────────────
    exp_bank_path = BASE_DIR / "data" / "content" / "experience_bank.md"
    cl_bank_path  = BASE_DIR / "data" / "content" / "cover_letter_bank.md"

    if not exp_bank_path.exists():
        print(f"[auto_prep] ERROR: {exp_bank_path} not found", file=sys.stderr)
        sys.exit(1)
    if not cl_bank_path.exists():
        print(f"[auto_prep] ERROR: {cl_bank_path} not found", file=sys.stderr)
        sys.exit(1)

    exp_bank = parse_experience_bank(exp_bank_path)
    cl_bank  = parse_cover_letter_bank(cl_bank_path)

    # ── Build components ──────────────────────────────────────────────────────
    work_history, primary_company, bullet_scores_map, jd_keywords = build_work_history(
        exp_bank, domain, is_leadership, jd_text=jd_text
    )
    expertise    = select_expertise(domain, is_leadership)
    skills       = select_skills(conditional)
    title_lines  = select_title_lines(domain, is_leadership)

    para1_opener = select_para1_opener(cl_bank["section3"], domain)
    para2        = "FILL_ME"   # LLM synthesizes from work_history bullets in STEP 5
    para3        = "FILL_ME"   # LLM synthesizes secondary breadth + AI closer in STEP 5

    company_label = args.company or "Company"

    # Derive city from job tracker entry (first segment of location field)
    # LinkedIn often returns verbose variants — normalise to clean city names
    _CITY_NORM_UK = {
        "greater london":     "London",
        "london area":        "London",
        "greater manchester": "Manchester",
        "west midlands":      "Birmingham",
        "west yorkshire":     "Leeds",
    }
    _CITY_NORM_NL = {
        "amsterdam": "Amsterdam",
        "rotterdam": "Rotterdam",
        "the hague": "The Hague",
        "den haag":  "The Hague",
        "utrecht":   "Utrecht",
    }
    _CITY_NORM_SE = {
        "stockholm":  "Stockholm",
        "gothenburg": "Gothenburg",
        "göteborg":   "Gothenburg",
        "malmö":      "Malmö",
        "malmo":      "Malmö",
    }
    _CITY_DEFAULTS = {"uk": "London", "nl": "Amsterdam", "se": "Stockholm"}
    market = "uk"
    city   = "London"
    is_investment = False
    try:
        with open(BASE_DIR / "data" / "job_tracker.json") as _f:
            _tracker = json.load(_f)
        _found = False
        for _app in _tracker.get("applications", []):
            if _app.get("id") == args.job_id or str(_app.get("job_id", "")) == args.job_id:
                _found = True
                market = _app.get("market", "uk")
                _loc = _app.get("location", "")
                if _loc:
                    _raw = _loc.split(",")[0].strip().lower()
                    _norm = (_CITY_NORM_UK if market == "uk"
                             else _CITY_NORM_NL if market == "nl"
                             else _CITY_NORM_SE)
                    city = _norm.get(_raw, _raw.title() if _raw else _CITY_DEFAULTS[market])
                else:
                    city = _CITY_DEFAULTS[market]
                # Read investment flag from tracker; fall back to keyword check
                if "is_investment_domain" in _app:
                    is_investment = bool(_app["is_investment_domain"])
                else:
                    is_investment = is_investment_jd_fallback(jd_text)
                break
        if not _found:
            # job_id not in tracker (manually-added) — use keyword fallback
            is_investment = is_investment_jd_fallback(jd_text)
    except Exception:
        is_investment = is_investment_jd_fallback(jd_text)

    if is_investment:
        print("[auto_prep] investment domain detected — S5 MF hook will be added to summary")
    today = datetime.date.today().strftime("%Y-%m-%d")

    bullet_counts = ", ".join(
        f"{r['company'].split('/')[0].strip()[:8]}:{len(r['bullets'])}"
        for r in work_history
    )
    print(f"[auto_prep] primary_company={primary_company}  bullets=[{bullet_counts}]")
    print(f"[auto_prep] expertise ({len(expertise)}): {expertise}")
    print(f"[auto_prep] skills: {skills}")

    # ── Resume JSON ───────────────────────────────────────────────────────────
    resume_json: dict = {
        "name": "Vinay Patidar",
        "title_lines": title_lines,
        "contact": CONTACT,
        "core_expertise": expertise,
        "skills": skills,
        "summary": (
            "Write exactly 4 sentences (5 if is_investment is True in _auto_prep_meta):\n"
            "S1: [Role + 8+ years + 2-3 industries — no metrics]\n"
            "S2-S3: [Domain strengths — no % metrics]\n"
            "S4 (verbatim): 'Also brings current hands-on AI workflow automation experience, "
            "having built a production agentic pipeline using Claude Code, MCP servers, and Anthropic APIs.'\n"
            "S5 if investment (verbatim): 'Brings additional domain fluency from 7+ years of "
            "active personal Mutual Fund investment across equity, debt, and hybrid fund categories.'\n"
            "Target: ≤90w (4-sentence) / ≤110w (5-sentence). FILL_ME"
        ),
        "work_history": work_history,
        "education": EDUCATION,
        "certifications": CERTIFICATIONS,
        "_auto_prep_meta": {
            "domain": domain,
            "is_leadership": is_leadership,
            "is_investment": is_investment,
            "primary_company": primary_company,
            "conditional_skills": conditional,
            "bullet_scores": bullet_scores_map,
            "jd_keywords": jd_keywords,
        },
    }

    # ── Cover letter JSON ─────────────────────────────────────────────────────
    hook_placeholder = (
        "[COMPANY_HOOK: 1-2 sentences — name the company + exact role title + "
        "one specific thing about the company's mission/product. FILL_ME]"
    )
    cover_json: dict = {
        "name": "Vinay Patidar",
        "title_lines": title_lines,
        "contact": CONTACT,
        "core_expertise": [],
        "skills": [],
        "date": f"{city}, {today}",
        "market": market,
        "recipient": f"{company_label} Hiring Team",
        "salutation": "Dear Hiring Team,",
        "paragraphs": [
            para1_opener + " " + hook_placeholder,
            para2,
            para3,
            "FILL_ME",
        ],
        "closing": "Kind regards,",
        "_auto_prep_meta": {
            "domain": domain,
            "is_leadership": is_leadership,
            "is_investment": is_investment,
            "primary_company": primary_company,
            "para1_source": "cover_letter_bank Section 3",
            "para2_source": "CV work_history bullets — LLM-synthesized (impactful + JD-relevant)",
            "para3_source": "CV work_history bullets — secondary breadth + mandatory AI closer",
            "ai_closer": (
                "Beyond this, I bring current hands-on AI engineering capability — having built "
                "a production-grade end-to-end agentic automation system using Claude Code, MCP "
                "servers, and the Anthropic API, fully operational in production."
            ),
        },
    }

    # ── Write outputs ─────────────────────────────────────────────────────────
    prep_tmp = BASE_DIR / "data" / "prep_tmp"
    prep_tmp.mkdir(parents=True, exist_ok=True)
    out_resume = prep_tmp / f"auto_resume_{args.job_id}.json"
    out_cover  = prep_tmp / f"auto_cover_{args.job_id}.json"

    out_resume.write_text(json.dumps(resume_json, indent=2, ensure_ascii=False), encoding="utf-8")
    out_cover.write_text(json.dumps(cover_json, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"[auto_prep] ✓ Resume pre-fill → {out_resume}")
    print(f"[auto_prep] ✓ Cover pre-fill  → {out_cover}")
    print("[auto_prep] LLM tasks: fill 'summary' (4 sentences), "
          "replace '[COMPANY_HOOK: ... FILL_ME]' in paragraphs[0], "
          "synthesize paragraphs[1] (Para 2, 120-150w from CV bullets + JD relevance), "
          "synthesize paragraphs[2] (Para 3, 80-100w: secondary breadth + ai_closer), "
          "write paragraphs[3] (Para 4, ~70 words). "
          "Remove _auto_prep_meta before rendering.")


if __name__ == "__main__":
    main()
