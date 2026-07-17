#!/usr/bin/env python3
"""
monitor_scout.py — Scout health monitoring
==========================================
Runs at the end of each scout (called by run_scout.py).
Collects data from 5 risk areas and appends to data/monitoring/*.json.
Prints a one-page health table per run.

Files written (all appended, never overwritten):
  data/monitoring/scout_runs.json    ← one entry per completed run
  data/monitoring/apify_budget.json  ← Apify monthly usage snapshot
  data/monitoring/actions_runs.json  ← GitHub Actions run history
  data/monitoring/url_health.json    ← written by apify_cache.py during scrape

Usage:
  python3 scripts/monitor_scout.py [--triggered-by github_actions|local_manual]
  Called automatically by run_scout.py after each successful run.
"""

import json, sys, os
from datetime import datetime, date, timedelta
from pathlib import Path
from statistics import mean
from urllib import request, error

ROOT         = Path(__file__).parent.parent
MONITOR_DIR  = ROOT / "data" / "monitoring"
TRACKER_PATH = ROOT / "data" / "job_tracker.json"
ENV_FILE     = ROOT / ".env"

MONITOR_DIR.mkdir(parents=True, exist_ok=True)

# ── Env helpers ───────────────────────────────────────────────────────────────
def _env(key: str) -> str:
    # .env file takes precedence over environment (avoids shell-injected tokens
    # with limited scope overriding the project PAT for API calls).
    # Exception: GITHUB_ACTIONS env var is set by the Actions runner itself.
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    return os.environ.get(key, "")

# ── JSON helpers ──────────────────────────────────────────────────────────────
def _read(path: Path) -> list:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return []
    return []

def _append(path: Path, record: dict):
    records = _read(path)
    records.append(record)
    path.write_text(json.dumps(records, indent=2))

# ── Apify budget ──────────────────────────────────────────────────────────────
def _fetch_apify_budget() -> dict:
    token = _env("APIFY_TOKEN")
    if not token:
        return {}
    try:
        req = request.Request(
            f"https://api.apify.com/v2/users/me/usage/monthly?token={token}",
            headers={"Content-Type": "application/json"},
        )
        with request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
        cycle = d["data"]["usageCycle"]
        services = d["data"]["monthlyServiceUsage"]
        used = sum(v.get("amountAfterVolumeDiscountUsd", 0) for v in services.values())
        plan_limit = 29.0  # Starter plan
        return {
            "cycle_start":       cycle["startAt"][:10],
            "cycle_end":         cycle["endAt"][:10],
            "used_usd":          round(used, 4),
            "plan_limit_usd":    plan_limit,
            "remaining_usd":     round(plan_limit - used, 4),
            "paid_actors_usd":   round(
                services.get("PAID_ACTORS_PER_EVENT", {}).get("amountAfterVolumeDiscountUsd", 0), 4
            ),
        }
    except Exception as e:
        print(f"  [monitor] Apify budget fetch failed: {e}")
        return {}

# ── GitHub Actions runs ───────────────────────────────────────────────────────
def _fetch_actions_runs() -> list:
    token = _env("GITHUB_TOKEN")
    if not token:
        return []
    try:
        req = request.Request(
            "https://api.github.com/repos/vinaypatidar02/job-automation/actions/runs?per_page=5",
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
        )
        with request.urlopen(req, timeout=15) as r:
            runs = json.loads(r.read()).get("workflow_runs", [])
        results = []
        for run in runs:
            started = run.get("run_started_at") or run.get("created_at", "")
            updated = run.get("updated_at", "")
            dur_min = None
            if started and updated:
                try:
                    s = datetime.fromisoformat(started.replace("Z", "+00:00"))
                    u = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                    dur_min = round((u - s).total_seconds() / 60, 1)
                except Exception:
                    pass
            results.append({
                "run_id":       run.get("id"),
                "status":       run.get("status"),
                "conclusion":   run.get("conclusion"),
                "created_at":   run.get("created_at", "")[:19],
                "duration_min": dur_min,
                "trigger":      run.get("event"),
            })
        return results
    except Exception as e:
        print(f"  [monitor] GitHub Actions fetch failed: {e}")
        return []

# ── Scout run stats from tracker ──────────────────────────────────────────────
def _scout_stats_from_tracker(since_date: str) -> dict:
    if not TRACKER_PATH.exists():
        return {}
    try:
        raw = json.loads(TRACKER_PATH.read_text())
        apps = raw.get("applications", raw) if isinstance(raw, dict) else raw
        entries = list(apps.values()) if isinstance(apps, dict) else apps
        today_entries = [
            e for e in entries
            if (e.get("status_history") and
                (e["status_history"][-1].get("date", "") or "")[:10] == since_date)
        ]
        shortlists = sum(1 for e in today_entries if e.get("status") == "Shortlisted")
        reviews    = sum(1 for e in today_entries if e.get("status") == "Review Needed")
        return {"new_shortlists": shortlists, "new_reviews": reviews,
                "new_tracker_entries": len(today_entries)}
    except Exception:
        return {}

# ── Gap detection ─────────────────────────────────────────────────────────────
def _hours_since_last_success() -> float | None:
    path = MONITOR_DIR / "scout_runs.json"
    records = _read(path)
    if not records:
        return None
    last = records[-1]
    ts = last.get("run_timestamp") or last.get("date", "")
    if not ts:
        return None
    try:
        if "T" in ts:
            last_dt = datetime.fromisoformat(ts)
        else:
            last_dt = datetime.strptime(ts, "%Y-%m-%d")
        return round((datetime.now() - last_dt).total_seconds() / 3600, 1)
    except Exception:
        return None

# ── URL health summary for today ──────────────────────────────────────────────
def _url_health_today() -> dict:
    path = MONITOR_DIR / "url_health.json"
    records = _read(path)
    today = date.today().isoformat()
    today_records = [r for r in records if r.get("date") == today]
    if not today_records:
        return {}
    items_list = [r["items"] for r in today_records]
    usd_list   = [r["usd"]   for r in today_records]
    zeros = [r["keyword"] for r in today_records if r["items"] == 0]
    return {
        "urls_this_run":    len(today_records),
        "total_items":      sum(items_list),
        "total_usd":        round(sum(usd_list), 4),
        "min_items":        min(items_list),
        "max_items":        max(items_list),
        "avg_items":        round(sum(items_list) / len(items_list), 1),
        "zero_result_urls": zeros,
    }

# ── Scoring run stats for THIS run ───────────────────────────────────────────
def _read_scoring_run_today() -> dict:
    path = MONITOR_DIR / "scoring_run.json"
    records = _read(path)
    today = date.today().isoformat()
    today_records = [r for r in records if r.get("date") == today]
    if not today_records:
        return {}
    return today_records[-1]  # latest entry for today

# ── Rolling averages from scout_runs.json + scoring_run.json ──────────────────
def _compute_averages(window: int = 7) -> dict:
    runs   = _read(MONITOR_DIR / "scout_runs.json")
    scores = _read(MONITOR_DIR / "scoring_run.json")
    if len(runs) < 2:
        return {}
    recent_runs   = runs[-window:]
    recent_scores = scores[-window:] if scores else []
    return {
        "n":              len(recent_runs),
        "avg_items":      round(mean(r.get("total_items_scraped", 0) for r in recent_runs), 1),
        "avg_apify_usd":  round(mean(
            r.get("apify_cost_this_run") if r.get("apify_cost_this_run") is not None
            else r.get("total_usd", 0)
            for r in recent_runs
        ), 4),
        "avg_shortlists": round(mean(r.get("new_shortlists", 0) for r in recent_runs), 1),
        "avg_reviews":    round(mean(r.get("new_reviews", 0) for r in recent_runs), 1),
        "avg_claude_usd": round(mean(s.get("cost_usd", 0) for s in recent_scores), 4)
                          if recent_scores else None,
    }

# ── 7-day burn rate from apify_budget.json ────────────────────────────────────
def _burn_rate_7d() -> float | None:
    path = MONITOR_DIR / "apify_budget.json"
    records = _read(path)
    if len(records) < 2:
        return None
    cutoff = (datetime.now() - timedelta(days=7)).date().isoformat()
    recent = [r for r in records if r.get("date", "") >= cutoff]
    if len(recent) < 2:
        return None
    used_list = [r.get("used_usd", 0) for r in recent]
    return round((max(used_list) - min(used_list)) / len(recent), 4)

# ── Print health table ────────────────────────────────────────────────────────
def _print_health(budget: dict, url_health: dict, gap_hours, actions_runs: list,
                  tracker_stats: dict, scoring_today: dict, averages: dict):
    w = 60
    print()
    print("═" * w)
    print(f"  SCOUT HEALTH — {date.today().isoformat()}")
    print("═" * w)

    # Apify budget
    if budget:
        used   = budget.get("used_usd", 0)
        limit  = budget.get("plan_limit_usd", 29)
        remain = budget.get("remaining_usd", 0)
        pct    = round(used / limit * 100) if limit else 0
        status = "⚠ LOW" if remain < 5 else "OK"
        print(f"  [APIFY BUDGET]  ${used:.2f} / ${limit:.0f} ({pct}%)  {status}")
        print(f"    Remaining: ${remain:.2f}  |  Cycle ends: {budget.get('cycle_end','?')}")
    else:
        print("  [APIFY BUDGET]  (fetch failed)")

    print()

    # This run — costs and outcomes
    apify_items = url_health.get("total_items", 0)
    apify_cost  = url_health.get("total_usd", 0.0)
    claude_cost = scoring_today.get("cost_usd", 0.0)
    total_cost  = apify_cost + claude_cost
    shortlists  = tracker_stats.get("new_shortlists", 0)
    reviews     = tracker_stats.get("new_reviews", 0)
    new_entries = tracker_stats.get("new_tracker_entries", 0)
    jobs_p2     = scoring_today.get("jobs_p2", 0)
    in_tok      = scoring_today.get("input_tokens", 0)
    out_tok     = scoring_today.get("output_tokens", 0)

    print(f"  [THIS RUN]")
    print(f"    Scraped:   {apify_items} items  →  Apify ${apify_cost:.4f}")
    if scoring_today:
        print(f"    Scored:    {jobs_p2} jobs  ({in_tok:,} in / {out_tok:,} out tok)  →  Claude ${claude_cost:.4f}")
    else:
        print(f"    Scored:    (scoring data not available for this run)")
    print(f"    Total API: ${total_cost:.4f}")
    print(f"    Tracker:   +{new_entries} entries  |  {shortlists} shortlisted  |  {reviews} review needed")

    print()

    # Rolling averages
    if averages:
        n = averages["n"]
        avg_apify  = averages["avg_apify_usd"]
        avg_claude = averages.get("avg_claude_usd")
        avg_total  = avg_apify + (avg_claude or 0.0)
        print(f"  [{n}-RUN AVG]")
        print(f"    Items/run:      {averages['avg_items']}")
        print(f"    Apify/run:      ${avg_apify:.4f}")
        if avg_claude is not None:
            print(f"    Claude/run:     ${avg_claude:.4f}")
            print(f"    Total/run:      ${avg_total:.4f}")
        print(f"    Shortlists/run: {averages['avg_shortlists']}"
              f"  |  Reviews/run: {averages['avg_reviews']}")
    else:
        print("  [AVG]           Not enough runs yet (need ≥2 completed runs)")

    print()

    # URL health
    if url_health:
        zeros = url_health["zero_result_urls"]
        print(f"  [URL HEALTH]    {url_health['urls_this_run']} URLs  "
              f"|  range {url_health['min_items']}–{url_health['max_items']}  "
              f"|  avg {url_health['avg_items']}/URL")
        if zeros:
            print(f"    ⚠ ZERO-RESULT URLs: {', '.join(zeros)}")
        else:
            print("    No zero-result URLs")
    else:
        print("  [URL HEALTH]    No live Apify calls this run (cache hits or no data)")

    print()

    # Gap detection
    if gap_hours is None:
        print("  [GAP]           First recorded run — baseline set")
    elif gap_hours > 26:
        print(f"  [GAP]           ⚠ MISSED RUN — {gap_hours:.1f}h since last (expected ≤26h)")
    else:
        print(f"  [GAP]           {gap_hours:.1f}h since last success  OK")

    print()

    # GitHub Actions
    if actions_runs:
        latest = actions_runs[0]
        dur    = f"{latest['duration_min']}m" if latest['duration_min'] else "?"
        conc   = latest.get("conclusion") or latest.get("status") or "?"
        print(f"  [ACTIONS]       Latest: {conc} in {dur}  ({latest.get('created_at','?')[:10]})")
        if latest["duration_min"] and latest["duration_min"] > 80:
            print("    ⚠ Duration >80 min — approaching 90 min timeout")
    else:
        print("  [ACTIONS]       (fetch failed or no runs yet)")

    print("═" * w)
    print()

# ── Main ──────────────────────────────────────────────────────────────────────
def run(triggered_by: str = "local_manual"):
    today = date.today().isoformat()

    print("\n[monitor_scout] Collecting health data...")

    # 1. Apify budget
    budget = _fetch_apify_budget()
    if budget:
        budget_record = {"date": today, "triggered_by": triggered_by, **budget}
        _append(MONITOR_DIR / "apify_budget.json", budget_record)

    # 2. URL health (already written by apify_cache.py during scrape)
    url_health = _url_health_today()

    # 3. GitHub Actions runs
    actions_runs = _fetch_actions_runs()
    if actions_runs:
        # Append only the latest run if not already recorded
        existing_actions = _read(MONITOR_DIR / "actions_runs.json")
        existing_ids = {r.get("run_id") for r in existing_actions}
        for run in actions_runs:
            if run.get("run_id") not in existing_ids:
                _append(MONITOR_DIR / "actions_runs.json", {"date": today, **run})

    # 4. Gap detection (read BEFORE appending this run)
    gap_hours = _hours_since_last_success()

    # 5. Tracker stats
    tracker_stats = _scout_stats_from_tracker(today)

    # 6. Scoring stats (written by score_jobs.py during this pipeline run)
    scoring_today = _read_scoring_run_today()

    # 7. Apify cost delta: post - pre snapshot
    apify_cost_this_run = None
    pre_snap_path = MONITOR_DIR / "pre_run_snapshot.json"
    if pre_snap_path.exists() and budget:
        try:
            pre = json.loads(pre_snap_path.read_text())
            apify_cost_this_run = round(budget["used_usd"] - pre["apify_used_usd_before"], 4)
            pre_snap_path.unlink()  # transient — delete after reading
        except Exception:
            pass

    # 8. Append scout run summary
    run_record = {
        "date":                     today,
        "run_timestamp":            datetime.now().isoformat(timespec="seconds"),
        "triggered_by":             triggered_by,
        "total_items_scraped":      url_health.get("total_items", 0),
        "total_usd":                url_health.get("total_usd", 0),
        "apify_cost_this_run":      apify_cost_this_run,
        "urls_scraped":             url_health.get("urls_this_run", 0),
        "zero_result_urls":         url_health.get("zero_result_urls", []),
        "new_shortlists":           tracker_stats.get("new_shortlists", 0),
        "new_reviews":              tracker_stats.get("new_reviews", 0),
        "new_tracker_entries":      tracker_stats.get("new_tracker_entries", 0),
        "apify_used_usd":           budget.get("used_usd"),
        "apify_remaining_usd":      budget.get("remaining_usd"),
        "claude_cost_usd":          scoring_today.get("cost_usd"),
        "gap_hours_since_last":     gap_hours,
    }
    _append(MONITOR_DIR / "scout_runs.json", run_record)

    # 9. Rolling averages
    averages = _compute_averages()

    # 10. Print health table
    _print_health(budget, url_health, gap_hours, actions_runs, tracker_stats,
                  scoring_today, averages)


if __name__ == "__main__":
    triggered_by = "local_manual"
    for i, arg in enumerate(sys.argv[1:]):
        if arg == "--triggered-by" and i + 1 < len(sys.argv) - 1:
            triggered_by = sys.argv[i + 2]
        elif arg.startswith("--triggered-by="):
            triggered_by = arg.split("=", 1)[1]
    run(triggered_by=triggered_by)
