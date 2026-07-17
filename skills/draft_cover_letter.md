# Skill: draft_cover_letter
# Stage 3 — ACTIVE
#
# ============================================================
# LEARNING NOTE — Skills that call other skills
# ============================================================
# This skill consumes the OUTPUT of tailor_resume.md (the
# tailored resume JSON) plus the JD text. It does NOT re-read
# the base resume PDFs — it works from what tailor_resume
# already selected and prioritised. This avoids duplication
# of work and keeps the cover letter consistent with the resume.
# ============================================================

# ── INPUT ────────────────────────────────────────────────────
# {
#   "job": <score_job.md output object>,
#   "jd_text": "<full job description text>",
#   "tailored_resume": <tailor_resume.md output JSON>,
#   "today": "<YYYY-MM-DD>",
#   "market": "uk" | "nl" | "se"   ← determines date-city and Para 4 relocation sentence
# }

# ── STEP 0 — READ COVER LETTER BANK & ANCHOR TO RESUME ───────
# Read data/content/cover_letter_bank.md using the Read tool BEFORE writing anything.
#
#   Step 0a: Read data/content/cover_letter_bank.md
#
#   Step 0b: Para 2 source — CV work_history bullets from tailored_resume (NOT bank narratives).
#     → Read all work_history bullets from tailored_resume
#     → Rank bullets by: (1) hard quantified metrics, (2) JD keyword overlap
#     → Pick 2-3 highest-impact, most JD-relevant bullets as Para 2 anchors
#     → Synthesize into flowing 120-150 word challenge→action→result narrative
#     → Use cover_letter_bank.md Section 1 as STYLE REFERENCE ONLY — tone and structure;
#       do NOT copy bank text; synthesize fresh from the actual CV bullets
#     → Always include at least one specific metric verbatim. Never fabricate.
#
#   Step 0c: Para 3 source — remaining CV bullets (NOT bank Section 2 themes).
#     → Take remaining work_history bullets NOT anchored in Para 2
#     → If JD-relevant bullets exist covering a different thematic angle from Para 2
#       (e.g. leadership, pricing breadth, experimentation depth), synthesize ~50-60 words
#     → Always close Para 3 with the AI closer sentence (~25-30 words, mandatory):
#       "Beyond this, I bring current hands-on AI engineering capability — having built
#        a production-grade end-to-end agentic automation system using Claude Code, MCP
#        servers, and the Anthropic API, fully operational in production."
#     → Skip the secondary breadth only if no remaining bullets have JD relevance;
#       the AI closer is NEVER skipped
#
#   Step 0d: Para 1 opener (Section 3 of bank):
#     → Match by JD domain (domain-level positioning — not company-specific)
#     → Append a fresh company-specific hook after the opener
#
#   Para 4: always written fresh — never from the bank
#
# RULES:
#   - NEVER change any metric (40%, 30%, 85%, 70/30, 7%, 35%, 37%, 95%)
#   - NEVER fabricate any experience, metric, or company fact
#   - NEVER copy bank Section 1 narratives verbatim into Para 2 — synthesize from CV

# ── INSTRUCTIONS ─────────────────────────────────────────────
# Write a 4-paragraph cover letter (350–450 words) following the
# eBay sample format from CLAUDE.md Section 5.
#
# PARAGRAPH 1 — Role excitement + company alignment + summary (80–100 words)
#   - Start with the matching Para 1 opener from Section 3 of the bank (domain-matched)
#   - Append a fresh, company-specific hook: name the company + exact role title +
#     something specific about the company (product, culture, mission — do not fabricate)
#   FORBIDDEN: "I am writing to apply", "I am excited to apply for this role",
#              any generic opener not naming the specific company
#
# PARAGRAPH 2 — Most relevant experience mapped to JD (120–150 words)
#   - Source: work_history bullets from tailored_resume (selected in Step 0b above)
#   - Synthesize the 2-3 highest-impact, most JD-relevant bullets into flowing prose
#   - Structure: challenge → action → result
#   - Always include at least one specific metric verbatim
#   - Do NOT copy from cover_letter_bank.md Section 1 — synthesize fresh from CV bullets
#   - Use Section 1 only as a style reference for tone and narrative flow
#
# PARAGRAPH 3 — Broader strategic value + AI closer (80–100 words total)
#   Part A — Secondary breadth (~50-60 words, conditional):
#     - Source: remaining work_history bullets NOT anchored in Para 2
#     - Select bullets with a different thematic angle from Para 2
#       (e.g. leadership, pricing, experimentation if Para 2 was CRM/growth)
#     - Synthesize into ~50-60 words showing career breadth
#     - Skip Part A only if no remaining bullets have meaningful JD relevance
#   Part B — AI closer (~25-30 words, ALWAYS mandatory, always last):
#     - "Beyond this, I bring current hands-on AI engineering capability — having built
#        a production-grade end-to-end agentic automation system using Claude Code, MCP
#        servers, and the Anthropic API, fully operational in production."
#     - NEVER omit — it is a mandatory differentiator in every application
#   - If JD mentions MMM/attribution → add in Part A: "strong incrementality foundation
#     + active MMM/MTA study (Robyn, Meridian)" as a single sentence; never fabricate delivery
#
# PARAGRAPH 4 — Forward-looking + call to action (60–80 words)
#   - Always written fresh — never from the bank
#   - What specifically excites you about THIS company's mission or product
#   - 1 concrete thing you would want to work on or improve
#   - Professional closing + invitation to discuss
#   - Do not be generic — reference something real about the company
#
# CLOSING FORMAT:
#   "Kind regards,"
#   [blank line]
#   "Vinay Patidar"
#   "+91 XXXXX XXXXX"
#   "vinay_patidar02@yahoo.com"

# ── OUTPUT ───────────────────────────────────────────────────
# Return a JSON object ready to pass to scripts/pdf_renderer.py:
# {
#   "name": "Vinay Patidar",
#   "title_lines": <same as tailored_resume.title_lines>,
#   "contact": <same as tailored_resume.contact>,
#   "core_expertise": [],   ← empty for cover letter sidebar
#   "skills": [],           ← empty for cover letter sidebar
#   "date": "<City>, <today YYYY-MM-DD>",
#           where City = "London" (market=uk) | "Amsterdam" (market=nl) | "Stockholm" (market=se)
#   "recipient": "<Company Name> Hiring Team",
#   "salutation": "Dear Hiring Team,",
#   "paragraphs": [
#     "<paragraph 1 text>",
#     "<paragraph 2 text>",
#     "<paragraph 3 text>",
#     "<paragraph 4 text>"
#   ],
#   "closing": "Kind regards,"
# }

# ── MARKET-SPECIFIC ADJUSTMENTS ─────────────────────────────
# Apply ONLY when market ≠ "uk". Read the market field from the input.
#
# NL (market="nl"):
#   Date line: "Amsterdam, YYYY-MM-DD"
#   Para 4:    Add one sentence after the forward-looking content (before closing):
#     "I am actively pursuing a Dutch Highly Skilled Migrant (kennismigrant) permit
#      and am excited to relocate to Amsterdam — this role meets the IND eligibility
#      criteria." (Adjust naturally to fit Para 4 flow, ~25 words)
#   Tone:      Dutch market values directness — outcome-first sentences.
#              Avoid overly deferential phrasing ("I would be honoured to...").
#   Spelling:  UK English throughout (organisation, optimisation, behaviour).
#
# SE (market="se"):
#   Date line: "Stockholm, YYYY-MM-DD"
#   Para 4:    Add one sentence after the forward-looking content (before closing):
#     "I am committed to relocating to Stockholm and plan to apply for a Swedish
#      work permit (arbetstillstånd) — the role meets the ILO salary requirements
#      for permit eligibility." (Adjust naturally to fit Para 4 flow, ~25 words)
#   Tone:      Swedish companies value collaborative achievement — where Para 2/3
#              reference leadership bullets, frame as "leading a team to achieve X"
#              rather than just "led a team". Consensus-building resonates.
#   Spelling:  UK English throughout.

# ── CONSTRAINTS ───────────────────────────────────────────────
# NEVER open with a generic sentence. Company name must appear in sentence 1.
# NEVER fabricate company facts — only reference what is verifiably known.
# NEVER claim direct MMM/MTA delivery experience.
# NEVER use the same opening verb/phrase as another cover letter — vary them.
# Use UK English spelling throughout (organisation, optimisation, behaviour).
# For NL/SE markets: NEVER reference "UK" as a target country, "right to work in the UK",
#   "Skilled Worker Visa", or any UK-market context. The candidate is targeting NL/SE only.
#
# AI PROJECT FRAMING RULE:
# NEVER use terms that reveal the application domain of the AI Engineering Project:
# no "job application", "job search", "resume tailoring", "cover letter", or
# "job search automation". Use neutral technical terms only:
#   "data ingestion" (not "scraping job listings")
#   "document generation" (not "resume tailoring")
#   "event-driven email processing" (not "email tracking")
# The project's value is the agentic engineering capability — not its specific domain.
#
# WORD LIMIT: 350–450 words across all 4 paragraphs combined. Count before outputting.
#   True constraint is 1 page — 350-450 is a calibrated proxy from the eBay sample (~420 words).
#   If over 450: trim Para 3 Part A first (secondary breadth), then Para 4. Never trim Para 2 or Para 1.
#   If under 350: expand Para 4 (the company-specific closing paragraph).
#   Remove filler phrases ("I am confident that", "I would be delighted to", etc.).
#   Every sentence must carry new information — no repetition of points already in the
#   resume summary.
