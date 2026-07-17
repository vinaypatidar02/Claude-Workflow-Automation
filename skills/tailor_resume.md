# Skill: tailor_resume
# Stage 3 — LEGACY FALLBACK
#
# ============================================================
# NOTE: The main pipeline uses agents/application_prep.md +
# scripts/auto_prep.py instead of this skill directly.
# Use this skill only for ad-hoc one-off resume tailoring
# outside the standard application prep flow.
#
# Content source: data/content/experience_bank.md (not the PDF variants).
# The three PDF files (master_resume.pdf, product_resume.pdf,
# customer_resume.pdf) are reference documents — not pipeline
# inputs. Domain detection is done from JD text, not by reading
# a PDF. See auto_prep.py for the authoritative domain logic.
# ============================================================

# ── INPUT ────────────────────────────────────────────────────
# {
#   "job": <score_job.md output object>,
#   "jd_text": "<full job description text>"
# }

# ── STEP 1 — DETECT JD DOMAIN ────────────────────────────────
# Read the JD text and classify the domain:
#   product/growth/ecommerce/marketplace/SaaS → product domain
#   CRM/retention/lifecycle/customer/marketing → crm domain
#   commercial/pricing/operations             → pricing domain
#   BI/insights/reporting/general analytics   → bi domain
#   Ambiguous or thin JD                      → general domain (broad bullet pool)
# This drives bullet priority (Step 1b) and expertise selection (Step 1c).
# Do NOT open or read any PDF file — the PDF variants are reference documents only.

# ── STEP 1b — READ EXPERIENCE BANK (PRIMARY CONTENT SOURCE) ──
# Read data/content/experience_bank.md using the Read tool.
# This file contains ALL bullet points across all roles, tagged by domain.
# Use it as the PRIMARY source of bullet content — it supersedes the PDF variant
# for bullet selection. The PDF variant signals which bullets were previously
# emphasised in that domain, but the bank is the full set to choose from.
#
# BULLET SELECTION RULES (apply after reading the bank):
#   1. ALWAYS INCLUDE — AI Engineering Project (pinned first, all 3 bullets, never trim)
#   2. ALWAYS INCLUDE — leadership bullets for Lead/Manager-level JDs
#      (team of 5 at BeepKart, team of 3 at DeHaat)
#   3. ALWAYS PRESERVE — all numeric metrics exactly (40%, 30%, 85%, 70/30)
#   4. DOMAIN PRIORITY — prioritise bullets whose tags match the JD domain:
#        Product/growth/SaaS JD    → [product] [growth] [experimentation] first
#        CRM/lifecycle/mktg JD     → [CRM] [growth] [pricing] first
#        Commercial/pricing/ops JD → [pricing] [bi] [leadership] first
#   5. SUPPORTING EVIDENCE — include cross-domain bullets if space allows (shows breadth)
#   6. 2-PAGE LIMIT — if content overflows, trim lower-priority bullets from the
#      earliest role (Coviam 2017–2020) first, then Quinbay, never drop a role
#      entirely (keep ≥1 bullet per role minimum)
#   NOTE: Bullets marked <!-- ADD: --> are placeholders the user will fill in —
#   skip empty placeholder lines, include any real bullets the user has added

# ── STEP 1c — SELECT CORE EXPERTISE & SKILLS ─────────────────
# Apply these rules BEFORE writing the output JSON. Do not leave either field
# to free-form judgment — select from the defined pools only.
#
# ── CORE EXPERTISE ────────────────────────────────────────────
# Always produce 8–10 items. Lead with domain-primary items, end with pinned.
#
# PINNED (always include, appear last in the list):
#   "AI Workflow Automation"
#   "Agentic Analytics Engineering"
#
# DOMAIN-PRIORITISED (pick from pools below to fill the remaining 6–8 slots):
#
#   Product / growth / ecommerce / SaaS JD:
#     Primary (pick all 3): Product Analytics, Growth Analytics,
#                           Experimentation & Incrementality Testing
#     Secondary (pick 2–3): Behavioral Analytics, Conversion Optimization,
#                           Stakeholder Management
#
#   CRM / lifecycle / retention / marketing JD:
#     Primary (pick all 3): CRM Analytics, Customer Lifecycle Analytics,
#                           Retention Analytics
#     Secondary (pick 2–3): Customer Segmentation, Propensity Modeling,
#                           Growth Analytics
#
#   Commercial / pricing / operations JD:
#     Primary (pick all 3): Pricing & Commercial Optimization,
#                           Strategic Decision-Making, Analytics Transformation
#     Secondary (pick 2–3): KPI Strategy & Business Intelligence,
#                           Stakeholder Management
#
#   BI / insights / general analytics JD:
#     Primary (pick all 2): KPI Strategy & Business Intelligence,
#                           Analytics Transformation
#     Secondary (pick 3–4): Stakeholder Management, Strategic Decision-Making,
#                           Product Analytics, Growth Analytics
#
#   Leadership emphasis (Lead/Manager-level JD, any domain):
#     ALWAYS ADD if not already in primary: Stakeholder Management,
#     Strategic Decision-Making
#
#   Mixed-domain JD: take Primary items from the 2 best-matching domains
#   (max 5 from domain pools so total stays ≤ 10 with pinned + secondary)
#
# ── SKILLS ────────────────────────────────────────────────────
# Compose the skills list from these three groups in order:
#
#   CORE — always include (fundamental tools, all roles):
#     SQL, Python, Tableau, BigQuery, Amazon Redshift, Looker Studio
#
#   CONDITIONAL — include only if JD explicitly mentions the associated keywords:
#     XGBoost          → JD mentions: ML models, machine learning, classification,
#                        prediction, XGBoost, gradient boosting
#     Prompt Engineering → JD mentions: AI, LLM, GenAI, prompts, language models,
#                          generative AI
#     MCP Servers      → JD mentions: AI tooling, agentic, workflow automation,
#                        MCP, orchestration
#
#   PINNED — always include, always at end of list (RULE B):
#     Claude Code, Anthropic API
#
#   NOT IN POOL — never list these regardless of JD:
#     Google Analytics, Firebase, Apache Airflow
#     (real profile history but too long since last use to defend in interview)
#
#   NEVER add tools outside this pool (e.g. Snowflake, dbt, Power BI, Spark —
#   not in Vinay's profile; fabricating tools is disqualifying)

# ── STEP 2 — EXTRACT JD KEYWORDS ─────────────────────────────
# From the JD text, identify:
#   a. Technical skills explicitly mentioned (SQL, Python, Tableau etc.)
#   b. Domain keywords (experimentation, CRM, pricing, segmentation etc.)
#   c. Seniority signals (lead, manager, head of, cross-functional etc.)
#   d. Industry context (fintech, ecommerce, marketplace, SaaS etc.)
#   e. Specific tools or platforms named
# These keywords will be used to prioritise and rephrase bullet points.

# ── STEP 3 — TAILOR BULLET POINTS ────────────────────────────
# For each role in the base resume:
#   - Move bullets that match JD keywords to the TOP of that role
#   - Rephrase bullets to align terminology with the JD where natural
#     (e.g. "customer propensity models" → "propensity modelling" if that is the JD term)
#     Only change the label of an existing concept — never add ideas or metrics not in the original bullet
#   - Preserve ALL original metrics exactly (40%, 30%, 85%, 70/30)
#   - Do NOT fabricate any experience, metric, or skill
#   - Do NOT remove bullets — only reorder and rephrase
#
# SPECIAL FRAMING RULES (from CLAUDE.md Section 2):
#
#   AI fluency — ALWAYS surface, regardless of domain or JD keywords.
#     It is a mandatory differentiator for every application in 2026.
#     Surface: "Hands-on AI workflow automation — built end-to-end production
#     agentic pipeline using Claude Code, MCP servers, agents and hooks (2026)"
#     Surface: Claude 101 and Claude Code 101 certifications (always list first)
#
#   Agile / Jira — if JD mentions Agile, Scrum, sprint, delivery:
#     Surface Jira, Confluence, Agile ceremonies from BeepKart experience
#
#   MMM / MTA / attribution — if JD mentions these:
#     NEVER claim direct MMM/MTA delivery. Never fabricate.
#     Frame as: "strong incrementality testing foundation (Flipkart) +
#     active study of MMM/MTA frameworks (Robyn, Meridian, Shapley)"
#     NEVER claim direct MMM/MTA delivery
#
#   Team leadership — if JD mentions managing/mentoring/people mgmt:
#     Lead with team sizes: "team of 5 (BeepKart)", "team of 3 (DeHaat)"
#     Mention Agile delivery structure and capability building

# ── STEP 4 — SUMMARY LINE ────────────────────────────────────
# Rewrite the resume summary (top paragraph) to:
#   - Open with Vinay's professional seniority level — NOT the job posting title.
#     Use the same label as title_lines Line 1 (e.g. "Lead Analytics Professional",
#     "Analytics Manager", "Analytics Lead & Manager"). This is especially important
#     when applying to Tier 2 roles (Senior DA/BA, Senior Product Analyst etc.):
#     the summary must still open at Lead/Manager level, not Senior IC level.
#     Reference STEP 5: "Customer analytics leader" is correct; "CRM Analytics Manager" is not.
#   - RULE (S1): Industries mentioned after "8+ years" MUST come from Vinay's actual
#     employer sectors (ecommerce, marketplace, agri-tech, automotive). DO NOT use the
#     target company's industry. Forbidden: fintech, insurance, SaaS, travel.
#   - S2-S3: describe 1-2 of Vinay's actual domain strengths using the work_history
#     bullets already in the resume — do NOT copy JD phrases verbatim.
#     Show relevance by describing what he genuinely did, in his own language.
#     RULE: no phrase in S2-S3 may be lifted word-for-word from the JD.
#   - Keep to 3-4 sentences max
#   - Do not change seniority level or fabricate experience
#   - ALWAYS close with one sentence surfacing AI fluency when the role is in
#     tech / product / fintech / SaaS / ecommerce (i.e. almost all target roles).
#     This is a genuine differentiator in 2026's competitive market and should
#     appear even when the JD does not use AI keywords.
#     Example closing sentence: "Also brings current hands-on AI workflow automation
#     experience, having built a production agentic pipeline using Claude Code,
#     MCP servers, and Anthropic APIs."
# Example full summary: if JD is "CRM Analytics Manager":
#   "Customer analytics leader with 8+ years driving CRM strategy,
#    lifecycle experimentation, and retention analytics across ecommerce
#    and technology businesses... Also brings hands-on AI workflow
#    automation experience using Claude Code and agentic systems."

# ── STEP 5 — PROFESSION LINE ─────────────────────────────────
# Generate exactly 2 title_lines for the sidebar under the name.
# These are Vinay's professional identity — they must NOT mirror the job
# posting title and must NOT include company- or role-specific language from the JD.
#
#   Line 1: Seniority descriptor — choose ONE of:
#     "Lead Analytics Professional"   ← Lead / Principal-level roles
#     "Analytics Lead & Manager"      ← roles emphasising both leadership and people mgmt
#     "Analytics Manager"             ← roles titled Analytics Manager or equivalent
#     Max 30 characters. NEVER use the job title from the posting.
#     NEVER use "Senior Analytics Manager" — Vinay has not held that title.
#
#   Line 2: 2–3 of Vinay's strongest specialties for this JD domain, " · " separated.
#     Max 32 characters. Pick from:
#     Product Analytics, Experimentation, CRM & Lifecycle, Pricing & Commercial,
#     Growth Analytics, AI & Automation, Stakeholder Strategy, Commercial Insights
#
#   Examples:
#     Product/SaaS/eComm → ["Lead Analytics Professional", "Product · Experimentation · AI"]
#     CRM/lifecycle      → ["Lead Analytics Professional", "CRM · Lifecycle · Growth"]
#     Commercial/pricing → ["Analytics Lead & Manager",   "Pricing · Commercial · Growth"]
#     Insight/generic    → ["Lead Analytics Professional", "Insights · Stakeholder Strategy · AI"]
#
#   FORBIDDEN:
#     ✗ Echoing the job title ("Senior Insight Lead", "Data Analytics Manager")
#     ✗ Adding company-specific nouns (marketplace, gaming, SaaS, digital)
#     ✗ Lines longer than 32 characters

# ── OUTPUT ───────────────────────────────────────────────────
# Return a JSON object ready to pass to scripts/pdf_renderer.py:
# {
#   "name": "Vinay Patidar",
#   "title_lines": ["<Line 1>", "<Line 2>"],
#   "contact": {
#     "email": "vinay_patidar02@yahoo.com",
#     "phone": "+91 XXXXX XXXXX",
#     "address": "Bengaluru, Karnataka 560035",
#     "linkedin": "linkedin.com/in/vinay-patidar-vp02"
#   },
#   "core_expertise": [<8–10 items — apply STEP 1c domain priority table>],
#   "skills": [<core + conditional (if JD-triggered) + pinned — apply STEP 1c pool rules>],
#   "summary": "<tailored summary paragraph>",
#   "work_history": [
#     {
#       "company": "<name>",
#       "location": "Bengaluru",
#       "role": "<role title>",
#       "dates": "<YYYY-MM-YYYY-MM>",   ← pdf_renderer converts to "Apr 2025 – Mar 2026"
#       "bullets": ["<bullet 1>", ...]  ← JD-relevant bullets first
#     }, ...
#   ],
#   "education": [{
#     "degree": "Mining Engineering, B.Tech",
#     "institution": "IIT (BHU), Varanasi",
#     "dates": "2012 – 2016",
#     "gpa": "7.67/10"
#   }],
#   "certifications": [
#     "Claude 101 — Anthropic Skilljar (2026)",
#     "Claude Code 101 — Anthropic Skilljar (2026)",
#     "Data Science using SAS and R — Analytix Labs",
#     "Managing Big Data with MySQL — Coursera",
#     "Data Visualization with Tableau — Coursera",
#     "Mastering Data Analysis in Excel — Coursera"
#   ]
# }
# NOTE: Claude certs ALWAYS listed first per CLAUDE.md Section 5.

# ── CONSTRAINTS ───────────────────────────────────────────────
# NEVER change any metric (40%, 30%, 85%, 70/30).
# NEVER add a skill or tool not in the candidate profile. Claude Code IS in the profile — always include it.
# NEVER remove a role from work history — only reorder bullets within roles.
# NEVER claim direct MMM/MTA delivery experience.
# Keep resume to 2 pages maximum when rendered — trim lower-priority
# bullets from older roles (Coviam 2017-2020) if content overflows.
#
# AI PROJECT FRAMING RULE:
# NEVER use terms that reveal the application domain of the AI Engineering Project:
# no "job application", "job search", "resume tailoring", "cover letter", or
# "job search automation". Use neutral technical terms only:
#   "data ingestion" (not "scraping job listings")
#   "document generation" (not "resume tailoring")
#   "event-driven email processing" (not "email tracking")
# The project's value is the agentic engineering capability — not its specific domain.

# ── PINNED RULES — AI PROJECT & SIDEBAR (override JD prioritisation) ──────
#
# RULE A — AI Project entry (ALWAYS include as first work_history entry):
#   company:  "Independent / Personal Project"
#   location: "Bengaluru"
#   role:     "Analytics & AI Engineering (Self-directed)"
#   dates:    "2026-03-present"
#   bullets (use text from data/content/experience_bank.md — never omit or compress):
#     - "Built production-grade end-to-end agentic AI pipeline: automated data ingestion
#        → LLM-based scoring and classification → document generation → PDF rendering
#        → event-driven email processing; fully operational in production"
#     - "Engineered 5-stage agentic workflow using Claude Code, MCP servers, hooks
#        and orchestration — event-driven pipelines triggered without human intervention"
#     - "Applied Anthropic API and prompt engineering across 8 interconnected skills,
#        agents, and hook definitions; completed Claude 101 + Claude Code 101 (Anthropic, 2026)"
#
# RULE B — Sidebar AI items (PINNED — never drop to make room for other items):
#   core_expertise must ALWAYS include:
#     "AI Workflow Automation"
#     "Agentic Analytics Engineering"
#   skills list must ALWAYS include:
#     "Claude Code"
#     "Anthropic API"
