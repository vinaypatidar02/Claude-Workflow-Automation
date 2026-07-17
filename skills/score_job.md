# Skill: score_job
# Stage 3 — ACTIVE
#
# ============================================================
# LEARNING NOTE — What is a Skill file?
# ============================================================
# A skill is a self-contained prompt template invoked by agents.
# It receives structured input, does one job, returns structured
# output. It has no side effects — it does not write files or
# update the tracker. The calling agent handles that.
# ============================================================

# ── INPUT ────────────────────────────────────────────────────
# A single enriched job object from enrich_jobs.py output.
# Key fields (nexgendata actor + enrichment):
#   job_title, company_name, location, job_url, job_id
#   salary (native, often empty), job_type, posted_date
#   description (full JD text)
#   compensation_extracted.display / .lower / .upper / .currency
#   experience_years.display / .min_yrs / .max_yrs
#   work_mode ("Remote" | "Hybrid (Xd/week)" | "On-site" | "Unknown")
#   ats_type, is_easy_apply, career_page_url (null unless in description)
#
# Jobs arrive pre-sorted newest-first from enrich_jobs.py.
# Process them in the order received — do not re-sort.

# ── INSTRUCTIONS ─────────────────────────────────────────────
# Step 1 — DUPLICATE CHECK — 3-signal check (before scoring, every time)
#   Read data/job_tracker.json. Check ALL three signals:
#     a. job_url exact match against any entry's jd_url
#     b. career_page_url exact match (if not null)
#     c. Fuzzy match: company_name + job_title within edit distance 2
#        against company + role fields of existing entries
#   If ANY signal matches → STOP. Return:
#     { "action": "skip", "reason": "duplicate",
#       "matched_id": "<existing id>", "matched_status": "<status>" }
#   Log: "DUPLICATE SKIPPED: [company] / [role] — already exists as [status]"
#
# Step 2 — SCORE the job using the rubric in CLAUDE.md Section 4.
#   Read each dimension carefully from the JD description text:
#
#   ROLE TITLE MATCH (0–20):
#     Compare job_title against target roles in CLAUDE.md Section 3.
#     20 = exact / near-exact match to listed target roles
#     10 = adjacent (Data Lead, Insights Manager, etc.)
#      0 = unrelated
#
#   DOMAIN MATCH (0–25):
#     Infer industry from company_name + description.
#     25 = product-led: tech, fintech, ecommerce, marketplace, SaaS
#     15 = other tech or data-heavy (media, logistics, healthtech)
#      5 = services / consulting / agency
#      0 = unrelated (manufacturing, public sector, etc.)
#     NOTE: If non-product company, only shortlist if score ≥ 88
#     overall AND company is well-known (FTSE 250 equivalent).
#
#   SKILLS MATCH (0–25):
#     Scan description for: SQL, Python, Tableau, BigQuery, Redshift,
#     Looker, Looker Studio, GA4, Firebase, Airflow, experimentation,
#     A/B testing, CRM analytics, pricing, segmentation, propensity.
#     25 = 7+ match  |  15 = 4–6 match  |  5 = 1–3 match
#
#   SENIORITY MATCH (0–15):
#     Use experience_years.min_yrs and description context.
#     15 = Lead/Manager level, 5–10 years expected
#     10 = slightly senior (Principal, Director-adjacent) but reachable
#      5 = slightly junior but interesting scope
#      0 = clear mismatch (entry-level or VP+)
#
#   LOCATION (0–10):
#     Match location field against CLAUDE.md Section 3 Tier list.
#     10 = London
#      8 = Manchester / Birmingham / Leeds
#      6 = Reading / Milton Keynes / Cambridge / Oxford /
#           Leicester / Coventry / Nottingham / Northampton
#      4 = Tier 3 city (Bristol, Brighton, Luton, Watford, Slough)
#      0 = outside all tiers → auto-reject regardless of other scores
#     If work_mode = "Remote" AND location is UK → score 8 minimum.
#
#   VISA SPONSORSHIP (0–5, or -10):
#      5 = explicitly stated in JD ("visa sponsorship available" etc.)
#      3 = company is a known UK Skilled Worker sponsor
#          (check well-known companies: HSBC, Monzo, Wise, Google etc.)
#      0 = no mention → flag "Sponsorship Unconfirmed", do not reject
#    -10 = explicitly states no sponsorship → TOTAL SCORE becomes 0,
#          status = "Auto-Rejected", reason = "No visa sponsorship"
#
# Step 3 — SALARY GATE: upper end > £85k = shortlist (apply after scoring)
#   Use compensation_extracted if available, else native salary field.
#     - Upper end > £85k → salary gate PASSED (even if lower < £85k)
#     - Both ends < £85k → salary gate FAILED → auto-reject
#     - Single figure < £85k → auto-reject
#     - Not stated / "Competitive" → flag "Salary TBC", do not reject
#
# Step 4 — DECIDE
#   TOTAL = sum of all 5 dimensions (visa can make total 0)
#   ≥ 75 AND visa not rejected AND salary gate passed/TBC → "Shortlisted"
#   60–74                                                 → "Review Needed"
#   < 60 OR visa rejected OR salary gate failed           → "Auto-Rejected"
#   Duplicate detected (Step 1)                           → "skip"

# ── OUTPUT ───────────────────────────────────────────────────
# Return a JSON object with this exact shape:
# {
#   "action": "shortlist" | "review" | "reject" | "skip",
#   "job_id": "<from input>",
#   "company": "<company_name>",
#   "role": "<job_title>",
#   "location": "<location>",
#   "jd_url": "<job_url>",
#   "fit_score": <0–100 integer>,
#   "fit_score_breakdown": {
#     "role_title": <0–20>,
#     "domain": <0–25>,
#     "skills": <0–25>,
#     "seniority": <0–15>,
#     "location": <0–10>,
#     "visa_sponsorship": <-10–5>
#   },
#   "visa_sponsorship_status": "Confirmed" | "Unconfirmed" | "Rejected",
#   "salary_stated": "<compensation_extracted.display or native salary or 'Not stated'>",
#   "salary_gate": "passed" | "failed" | "tbc",
#   "salary_meets_threshold": true | false | null,
#   "work_mode": "<from enrichment>",
#   "experience_req": "<experience_years.display>",
#   "ats_type": "<from enrichment>",
#   "is_easy_apply": <bool>,
#   "career_page_url": "<from enrichment or null>",
#   "posted_date": "<from input>",
#   "rejection_reason": "<if rejected: brief reason>" | null,
#   "flags": ["Salary TBC", "Sponsorship Unconfirmed", ...]
# }

# NOTE: After the calling agent writes to job_tracker.json,
# it must run: python3 scripts/sheets_sync.py push
# This skill does not do that directly — the agent does.

# ── CONSTRAINTS ───────────────────────────────────────────────
# NEVER shortlist a job where visa sponsorship is explicitly denied.
# NEVER shortlist a job outside the accepted location tiers.
# NEVER skip the duplicate check — run it for every single job.
# NEVER fabricate or assume skills not mentioned in the JD.
# When in doubt between "shortlist" and "review", choose "review".
# Always include reasoning in fit_score_breakdown, not just numbers.
