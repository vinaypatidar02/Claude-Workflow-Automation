# data/ — private state layer (excluded from this public repo)

In production this directory holds the pipeline's entire state:

| File | Purpose |
|---|---|
| `job_tracker.json` | Source of truth — every application, status history, fit scores |
| `auto_rejected.json` | Jobs rejected by the two-pass scoring gates |
| `unmatched_emails.json` | Recruiter emails the classifier could not match (manual review queue) |
| `content/experience_bank.md` | Fact-checked bullet bank — the only permitted source for CV/cover letter claims |
| `apify_cache/`, `adzuna_cache/` | 24h-TTL scrape caches (free same-day re-runs) |
| `sponsor_registers/` | UK Home Office + NL IND sponsor lists for Pass-1 visa gating |
| `prep_tmp/` | Intermediate resume/cover JSONs between generation and PDF render |

All of it is personal application data, so none of it ships here.
