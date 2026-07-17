# Agent: application_prep
# Stage 4 — ACTIVE
#
# ============================================================
# LEARNING NOTE — Agent Handoffs via Shared State
# ============================================================
# This agent is triggered after sheets_sync.py pull has run
# and updated job_tracker.json with your Sheet edits. It does
# not communicate directly with job_scout — it reads the shared
# state file. This loose coupling means each agent is testable
# and replaceable independently.
# ============================================================

# ── GIT SYNC — run FIRST before anything else ─────────────────
# Pull latest tracker state (CCR cron may have added new shortlisted jobs):
#   git pull
# This ensures the triple-condition gate below sees all CCR-added entries.
# If pull has conflicts, accept remote: git checkout --theirs data/job_tracker.json
# ──────────────────────────────────────────────────────────────

# ── TRIPLE-CONDITION GATE (check before doing anything) ───────
# Only process entries where ALL THREE are true:
#   1. status = "Approved"
#   2. career_page_url is not null and not empty
#   3. resume_path is null (prep not already done)
#
# EASY APPLY HANDLING:
#   If career_page_url = "EASY_APPLY" (sentinel value you enter in Sheet):
#     → Process normally — generate resume + cover letter as usual
#     → Set is_easy_apply = true in meta.json
#     → Completion summary will say:
#       "Submit via LinkedIn Easy Apply button — use the generated
#        resume PDF as reference when filling the form fields"
#     → Do NOT skip or block — prep is still useful for Easy Apply
#
# HACKAJOB PASSIVE HANDLING:
#   If career_page_url contains "hackajob.com" OR ats_type = "hackajob_passive":
#     → Log: "PASSIVE PLATFORM (hackajob) — no direct application path.
#              Employer initiates contact. Prep proceeds — documents useful if contacted."
#     → Set is_hackajob_passive = true in meta.json
#     → Completion summary notes: "Passive platform — submit documents only if employer
#       reaches out via hackajob"
#     → Do NOT skip prep — resume and cover letter are still useful if employer contacts
#
# If status = "Approved" but career_page_url is null or empty:
#   Log: "SKIPPED [Company] / [Role] — career_page_url missing.
#         For ATS roles: paste URL in Sheet Col M then re-run pull.
#         For Easy Apply: enter 'EASY_APPLY' in Sheet Col M."
#   Do NOT process. Move to next entry.
#
# If resume_path is already set:
#   Log: "SKIPPED [Company] / [Role] — already prepped at [resume_path]"
#   Do NOT re-process. This makes the agent safely re-runnable.

# ── FOR EACH QUALIFYING JOB ──────────────────────────────────

# STEP 1 — FETCH JD TEXT (cache-first, live-fetch fallback)
#   Run: python3 scripts/fetch_jd.py --job_id <job_id> --jd_url <jd_url>
#
#   Interpret the JSON result:
#
#   source = "enriched" | "raw" | "apify_cache" | "adzuna_cache"
#     → Use description field directly. This is the preferred path.
#     → Log: "[prep] JD from <source> for [Company] / [Role]"
#
#   source = "not_found"
#     → job_id not in any local cache. Fall back to WebFetch tool on jd_url.
#       If WebFetch succeeds: use the returned text.
#       If WebFetch fails (404 / auth wall / redirect):
#         Log warning: "[prep] JD unavailable — proceeding with tracker metadata only"
#         Continue prep using company, role, location, salary from tracker entry.
#     → Log: "[prep] JD from live fetch for [Company] / [Role]"
#
#   source = "no_job_id"
#     → Job has no job_id (Excel import or manually added entry). Skip cache entirely.
#       Go directly to WebFetch jd_url. Apply same failure handling as above.
#     → Log: "[prep] No job_id — attempting live fetch for [Company] / [Role]"

# STEP 2 — MECHANICAL PRE-FILL  (domain detection + content assembly)
#           Previously "Step 2b" — Step 2 (PDF variant selection) removed.
#           Domain is detected automatically by auto_prep.py from JD text;
#           no PDF variant needs to be selected or opened.
#
#   Write the JD text to a temp file (use job_id to avoid collisions):
#     Write JD text to /tmp/jd_<job_id>.txt
#
#   Run the pre-fill script:
#     python3 scripts/auto_prep.py \
#       --job_id <job_id> \
#       --jd_file /tmp/jd_<job_id>.txt \
#       --company "<Company Name>"
#
#   If the detected domain in the script output looks wrong (e.g. "bi" for a clear
#   product/growth role), re-run with --domain <correct_domain> override.
#
#   Script outputs (load both):
#     /tmp/auto_resume_<job_id>.json  — full resume JSON; summary = "FILL_ME"
#     /tmp/auto_cover_<job_id>.json   — cover letter JSON with:
#         paragraphs[0] = domain-matched Para 1 opener + "[COMPANY_HOOK: FILL_ME]" placeholder
#         paragraphs[1] = Para 2 narrative selected from data/content/cover_letter_bank.md Section 1
#         paragraphs[2] = Para 3 theme from Section 2 (always AI/automation theme)
#         paragraphs[3] = "FILL_ME"
#
#   Log: "[prep] Auto-prep: domain=<domain>, primary_company=<company>"
#   Note: script prints expertise list, skills list, and per-role bullet counts in its log.

# STEP 2.5 — PREP QUALITY EVALUATION  (runs before LLM tailoring, zero API cost)
#
#   Run:
#     python3 scripts/eval_prep.py \
#         --resume /tmp/auto_resume_<job_id>.json \
#         --jd /tmp/jd_<job_id>.txt
#
#   Checks: D1 (leadership signal), D2 (bullet ordering), D3 (tag completeness)
#   Cost: zero Claude API calls.
#
#   IF exit code 0: log "[prep] ✓ Eval passed" and proceed to STEP 3.
#
#   IF exit code 1 or 2 (issues found):
#
#     PART A — Fix this run (automatic, no approval needed):
#       python3 scripts/eval_prep.py \
#           --resume /tmp/auto_resume_<job_id>.json \
#           --jd /tmp/jd_<job_id>.txt \
#           --apply-ephemeral
#       Log what was patched (title_lines corrected, bullets reordered, etc.)
#       D1 HIGH (wrong title_lines) MUST be fixed before STEP 3 —
#       the LLM will bake in whichever title_lines it sees.
#
#     PART B — Fix the root cause (one issue at a time, with approval):
#       For each issue that carries a systemic_fix in the report:
#         1. Show: "Root cause fix — <file>: <what changes>"
#         2. Ask: "Apply this fix to prevent recurrence? (Y/n)"
#         3. If Y: use Edit tool to make the exact change described.
#            If N: note it and move on.
#         4. After applying: run python3 scripts/check_workflow.py --quick
#
#     Proceed to STEP 3 after both parts are done.
#
#   Note: D2 requires bullet_scores in _auto_prep_meta (auto-populated by auto_prep.py).
#   If absent (old version), D2 is silently skipped.

# STEP 3 — TAILOR RESUME  (LLM task — narrative text only, not full JSON composition)
#
#   BEFORE STARTING: Read /tmp/jd_<job_id>.txt (the full JD text saved in STEP 1).
#   You MUST have the JD in context when writing the summary — "JD-specific" means
#   referencing actual language, requirements, and priorities from this JD, not just
#   the domain label. If /tmp/jd_<job_id>.txt is unavailable, use the tracker metadata
#   (company, role, location) as a fallback, but note it in the log.
#
#   Load /tmp/auto_resume_<job_id>.json — the pre-filled resume JSON.
#   The script has already handled: core_expertise, skills, work_history (bullets
#   scored and ordered by JD relevance), title_lines, education, certifications.
#
#   LLM tasks (ONLY these — do NOT recompose the full JSON from scratch):
#
#     1. Write summary field (follow the structured placeholder in auto_prep JSON):
#          - S1: Vinay's professional seniority level (NOT the job posting title) + "8+ years"
#            + 2-3 industries — no metrics. Use: "Lead Analytics Professional",
#            "Analytics Manager", or "Analytics Lead & Manager" — same as title_lines Line 1.
#            Even for Tier 2 roles (e.g. Senior Product Analyst), open at Lead/Manager level.
            RULE: Industries MUST come from Vinay's actual employer sectors (ecommerce,
            marketplace, agri-tech, automotive). DO NOT use the target company's sector.
            Forbidden: fintech, insurance, SaaS, travel. Target company's industry is
            irrelevant to S1 — it describes HIS background, not the employer's.
#          - S2-S3: 1-2 domain-specific strengths drawn from Vinay's ACTUAL work experience.
#            Reference the role bullets already present in the work_history of this resume
#            JSON — describe what he genuinely built or led, in his own language, in a way
#            that implies relevance to the JD's focus without copying JD phrases verbatim.
#            Example: if JD emphasises "analytics infrastructure", write "Built Tableau data
#            marts and led analytics stack migration across 50+ microservices, enabling
#            real-time leadership visibility" — specific to his real work, not JD echo.
#            RULE: no phrase in S2-S3 may be lifted verbatim from the JD text.
#            No raw % metrics in the summary.
#          - S4 (verbatim, mandatory): "Also brings current hands-on AI workflow automation
#            experience, having built a production agentic pipeline using Claude Code, MCP
#            servers, and Anthropic APIs."
#          AI PROJECT FRAMING RULE: NEVER use terms that reveal the application domain of
#          the AI Engineering Project in any document: no "job application", "job search",
#          "resume tailoring", "cover letter", or "job search automation". Use neutral
#          technical terms: "data ingestion", "document generation", "event-driven email
#          processing". The project demonstrates agentic engineering capability — frame
#          it as such, not as job search automation.
#          Word budget: ≤90 words for 4-sentence summary.
#          Replace the structured "FILL_ME" placeholder.
#
#     2. CHECK _auto_prep_meta.is_investment flag BEFORE closing the summary:
#          If is_investment = True:
#            → Add S5 (verbatim) after S4:
#              "Brings additional domain fluency from 7+ years of active personal Mutual Fund
#               investment across equity, debt, and hybrid fund categories."
#            → Word budget expands to ≤110 words for 5-sentence summary.
#            → Do NOT add this to work_history bullets — it is personal experience, not professional.
#          If is_investment = False: 4 sentences only. Do NOT add MF hook.
#
#     3. Review title_lines — if the pre-selected lines feel wrong for this specific JD,
#        override using STEP 5 rules in skills/tailor_resume.md. Otherwise keep as-is.
#
#     4. Spot-check bullets[0] for each non-AI role — should be the most JD-relevant.
#        If the top bullet is wrong (e.g. a BI bullet for a product JD), swap the order.
#        Do NOT rewrite bullets — only reorder within each role.
#
#     5. Remove the "_auto_prep_meta" key from the JSON before passing to the renderer.
#
#   Output: finalised resume JSON (same structure as before — no change to pdf_renderer.py)
#   Log: "[prep] Resume JSON ready — summary written (investment=<True/False>)"

# STEP 3b — VALIDATE RESUME + COVER LETTER (run BEFORE rendering)
#
#   python3 scripts/validate_prep.py \
#       --resume /tmp/final_resume_<job_id>.json \
#       --cover  /tmp/final_cover_<job_id>.json \
#       --jd     /tmp/jd_<job_id>.txt \
#       --company "<Company Name>"
#
#   If exit code 0: proceed to STEP 4.
#   If exit code 1: fix the flagged issues and re-run validate_prep.py.
#     Do NOT render until all checks pass.
#   Log: "[prep] validate_prep: PASS / FAIL (list failing checks)"

# STEP 4 — RENDER RESUME PDF
#   Create output folder:
#     outputs/applications/ready/[Company]_[RoleShortName]_[YYYYMMDD]/
#   Write tailored resume JSON to a temp file.
#   Call: python3 scripts/pdf_renderer.py resume <temp.json> <output_path>
#   DEFAULT LAYOUT: single-column (UK-standard, ATS-safe — omit 4th arg or pass "single")
#   TWO-COLUMN LAYOUT: pass "two_column" as 4th arg only when explicitly requested
#     python3 scripts/pdf_renderer.py resume <temp.json> <output_path> two_column
#   Output path: [folder]/Vinay_Patidar_CV.pdf
#   Confirm file was created.
#   Log: "[prep] Resume PDF → [path] (layout: single)"

# STEP 5 — DRAFT COVER LETTER  (LLM task — narrative text only, not full JSON composition)
#
#   BEFORE STARTING: Read /tmp/jd_<job_id>.txt (same JD text used in STEP 3).
#   The company hook and Para 4 MUST reference specific details from the JD — the role
#   scope, team, product, or stated priorities. Generic company-name-only writing is not
#   acceptable here.
#
#   Load /tmp/auto_cover_<job_id>.json — the pre-filled cover letter JSON.
#   The script has pre-filled: Para 1 opener (bank Section 3 + hook placeholder).
#   Para 2, Para 3, and Para 4 are all "FILL_ME" — the LLM writes all three.
#
#   Source for Para 2 + Para 3: work_history bullets from the tailored resume JSON
#   (already in context from STEP 3). Do NOT use pre-written bank narratives for Para 2/3.
#   Read data/content/cover_letter_bank.md Section 1 as a STYLE REFERENCE ONLY — tone, structure, quality.
#   Also read _auto_prep_meta.ai_closer from the cover JSON — use it verbatim in Para 3.
#
#   LLM tasks:
#
#     1. Write the company-specific hook (1-2 sentences) to REPLACE the
#        "[COMPANY_HOOK: FILL_ME]" placeholder in paragraphs[0]:
#          - Name the company + exact role title (from JD, not paraphrased)
#          - Reference one specific and genuine thing from the JD
#            (a stated team focus, product challenge, or mission — do NOT fabricate)
#          - Keep it tight: 1-2 sentences max
#        Result: paragraphs[0] = opener_text + " " + company_hook
#
#     2. Write Para 2 (paragraphs[1]) — 120-150 words:
#          Source: work_history bullets from the tailored resume JSON (STEP 3 output).
#          - Select the most impactful, JD-relevant bullets from work_history
#          - Prefer bullets with hard quantified metrics (%, £, concrete numbers)
#          - Prefer bullets whose content most closely matches the JD's stated priorities
#          - Typically 2-3 bullet anchors across 1-2 roles
#          - Synthesize into flowing prose: challenge → action → result
#          - Use data/content/cover_letter_bank.md Section 1 entries as STYLE REFERENCE only
#            (do NOT copy bank text — synthesize fresh from CV bullets)
#          - Always include at least one specific metric verbatim
#          - Never fabricate any metric or experience
#
#     3. Write Para 3 (paragraphs[2]) — 80-100 words total, TWO PARTS IN ORDER:
#          Part A — Secondary breadth (~50-60 words, conditional):
#            Source: remaining work_history bullets NOT already anchored in Para 2.
#            Select bullets that are JD-relevant and cover a different thematic angle
#            (e.g. if Para 2 covers growth/experimentation, Para 3 covers leadership or pricing).
#            Synthesize into ~50-60 words of breadth prose.
#            Skip Part A only if no remaining bullets have meaningful JD relevance.
#          Part B — AI closer (~25-30 words, ALWAYS mandatory, always last):
#            Append _auto_prep_meta.ai_closer verbatim as the final sentence(s) of Para 3.
#            NEVER omit this — it is a mandatory differentiator in every application.
#
#     4. Write Para 4 (paragraphs[3]) — always fresh, never from bank:
#          - What specifically excites you about THIS company based on the JD
#          - 1 concrete thing you would want to work on (use detail from the JD)
#          - Professional closing + invitation to discuss
#          - 60-80 words
#        Replace the "FILL_ME" placeholder.
#
#     5. Confirm word count: 350-450 words across all 4 paragraphs.
#        If over 450: trim Para 3 Part A first, then Para 4. Never trim Para 2 or Para 1.
#        If under 350: expand Para 4 (the company-specific closing paragraph).
#
#     6. Remove the "_auto_prep_meta" key before passing to the renderer.
#        NOTE: "date" is already auto-filled by auto_prep.py ("City, YYYY-MM-DD").
#        Do NOT override it. For NL jobs the city resolves to "Amsterdam"; for SE to "Stockholm".
#
#   MARKET FIELD — read from tracker_entry.get("market", "uk").
#     Pass as the "market" field in the cover letter JSON if writing cover letter via skill.
#     auto_prep.py derives the city from the location field automatically — no override needed.
#     NL (market="nl"): Para 4 must include kennismigrant relocation sentence (see draft_cover_letter.md).
#     SE (market="se"): Para 4 must include arbetstillstånd relocation sentence (see draft_cover_letter.md).
#
#   INVESTMENT/FINANCE JD RULE (check _auto_prep_meta.is_investment from auto_prep JSON):
#   If is_investment = True:
#     → In Para 3 (paragraphs[2]), add 1-2 sentences surfacing personal investment domain
#       context. Frame as genuine domain fluency, not professional history. Example:
#       "Beyond my professional analytics work, my 7+ years as an active Mutual Fund investor
#        has given me direct familiarity with [specific relevant aspect from this JD — fund
#        performance metrics / market cycle analysis / risk-adjusted returns / etc.], which
#        I look forward to applying in a more formal investment context."
#     → This extends Para 3. Budget Para 4 at ≈60-70 words (not 60-80) so total
#       remains within 350-450 word cover letter target.
#     → Keep Para 3 within word count limits — trim other Para 3 content if needed.
#     → Never claim professional fund management experience.
#
#   Output: finalised cover letter JSON (same structure — no change to pdf_renderer.py)
#   Log: "[prep] Cover letter JSON ready — Para 4 written"

# STEP 5b — (word count is checked by validate_prep.py in STEP 3b — no separate step needed)

# STEP 6 — RENDER COVER LETTER PDF
#   Write cover letter JSON to a temp file.
#   Call: python3 scripts/pdf_renderer.py cover <temp.json> <output_path>
#   Output path: [folder]/Vinay_Patidar_CoverLetter.pdf
#   Confirm file was created.
#   Log: "[prep] Cover letter PDF → [path]"

# STEP 7 — WRITE meta.json
#   Save to [folder]/meta.json:
#   {
#     "company":          "<company>",
#     "role":             "<role>",
#     "job_id":           "<job_id>",
#     "jd_url":           "<jd_url>",
#     "career_page_url":  "<career_page_url>",
#     "ats_type":         "<ats_type>",
#     "fit_score":        <score>,
#     "resume_variant":   "<product|customer|master>",
#     "prep_date":        "<YYYY-MM-DD>",
#     "notes":            "<any flags from tracker>"
#   }

# STEP 8 — UPDATE job_tracker.json
#   Read current file fresh (never use an in-memory copy from earlier in the run).
#   Find the entry by id.
#
#   TERMINAL STATUS GUARD — check BEFORE writing:
#     Re-read the entry's current status from the just-loaded file.
#     If current status is any of the following, do NOT update status — only
#     update resume_path and cover_letter_path:
#       Withdrawn, Rejected, Applied, Under Review,
#       Interview Scheduled, Assessment, Offer Received
#     Reason: the user may have marked the entry as Withdrawn (e.g. posting
#     closed) between when this run started and now. Never overwrite that.
#     Log: "[prep] SKIPPED status update for [Company]/[Role] —
#            current status is [status] (terminal, not Approved)"
#
#   If current status IS "Approved", update all three fields:
#     status:              "Prep Complete"
#     resume_path:         "<relative path to resume PDF>"
#     cover_letter_path:   "<relative path to cover letter PDF>"
#     status_history:      append { "status": "Prep Complete",
#                                   "date": "<today>",
#                                   "source": "application_prep_agent" }
#   Write back to data/job_tracker.json.
#   Log: "[prep] Tracker updated → Prep Complete"

# STEP 9 — SYNC TO GOOGLE SHEETS  (batch — once per run, not per job)
#
#   SINGLE-JOB RUN: run sheets_sync.py push after the one job completes.
#
#   MULTI-JOB RUN: do NOT run sheets_sync.py push after each individual job.
#   Process all qualifying jobs through STEPS 1-8 first, then run ONCE at the end:
#     python3 scripts/sheets_sync.py push
#   This avoids N redundant API calls for N-job batches.
#   Log: "[prep] Google Sheet synced — <N> jobs pushed"

# STEP 10 — GIT COMMIT & PUSH (after sheets sync)
#
#   Commit tracker updates so CCR cron picks up latest dedup history:
#     git add data/job_tracker.json
#     git diff --cached --quiet || git commit -m "local: prep <Company> $(date +%Y-%m-%d)"
#     git push
#   If push is rejected (CCR pushed since your pull):
#     git pull --rebase && git push
#   Log: "[prep] Tracker committed and pushed to git"

# ── COMPLETION SUMMARY ────────────────────────────────────────
# After processing all qualifying jobs, print:
#   ═══════════════════════════════════════
#    Application Prep — <date>
#   ═══════════════════════════════════════
#    Processed:   X jobs
#      ATS jobs:  N  → open career_page_url to submit
#      Easy Apply: M → use LinkedIn Easy Apply button
#    Skipped:     Y jobs (career_page_url missing — enter URL or EASY_APPLY)
#    Skipped:     Z jobs (already prepped)
#   ───────────────────────────────────────
#    Resume + cover letter PDFs in outputs/applications/ready/
#    For ATS roles: open career_page_url and submit the form
#    For Easy Apply: open jd_url → click Easy Apply → use
#      the generated resume PDF to fill in the form fields
#   ═══════════════════════════════════════
