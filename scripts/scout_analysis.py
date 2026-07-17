#!/usr/bin/env python3
"""
scout_analysis.py — Post-run analysis report for job scout output.
Called automatically by run_scout.py after every scout run.
Also runnable standalone: python3 scripts/scout_analysis.py [--source apify|adzuna|auto]

Reports:
  - Ceiling check + title filter table per source
  - Keyword overlap matrix (% URL overlap between search labels)
  - Pipeline funnel: Apify | Adzuna | Total side-by-side
  - Shortlisted & Review Needed lists
  - Issues section

Always exits 0 — issues are warnings, never errors.
"""
import json, sys
from datetime import date
from pathlib import Path

ROOT  = Path(__file__).parent.parent
TODAY = date.today().isoformat()
sys.path.insert(0, str(ROOT))


# ── Lazy blocklist imports ────────────────────────────────────────────────────

def _get_apify_blocklist_fns():
    try:
        from scripts.apify_cache import _title_is_blocked, _title_is_blocked_for_label
        return _title_is_blocked, _title_is_blocked_for_label
    except Exception:
        return (lambda t: False), (lambda t, l: False)

def _get_adzuna_filter_fn():
    try:
        from scripts.adzuna_scraper import _title_is_relevant
        return _title_is_relevant
    except Exception:
        return lambda t: True


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None

def _title_tier_label(role_title_pts) -> str:
    if role_title_pts is None: return "Tier?"
    if role_title_pts >= 20:   return "Tier1"
    if role_title_pts >= 15:   return "Tier2"
    if role_title_pts >= 10:   return "Tier3"
    if role_title_pts >= 5:    return "Tier4"
    return "Tier5"

def _abbrev(labels: list[str]) -> dict[str, str]:
    """Generate unique 3-char abbreviations for a list of labels."""
    codes: dict[str, str] = {}
    used: set[str] = set()
    for label in labels:
        words = label.split()
        # Try first-letter of each word
        cand = "".join(w[0] for w in words).upper()[:4]
        if cand not in used:
            codes[label] = cand
            used.add(cand)
            continue
        # Fallback: first 3 chars of first word
        cand = label[:3].upper()
        i = 0
        base = cand
        while cand in used:
            i += 1
            cand = base[:2] + str(i)
        codes[label] = cand
        used.add(cand)
    return codes


def _divider(width: int = 80):
    print("  " + "─" * width)


def _table_row(cols: list, widths: list, sep: str = "  ") -> str:
    return sep + sep.join(str(c).ljust(w) if i == 0 else str(c).rjust(w)
                          for i, (c, w) in enumerate(zip(cols, widths)))


# ── Overlap matrix ────────────────────────────────────────────────────────────

def _overlap_matrix(label_urls: dict, issues: list, source_tag: str,
                    high_pct: int = 20):
    """
    Print keyword overlap matrix + unique-contribution summary.
    label_urls: {label: set_of_job_urls}
    """
    active = {k: v for k, v in label_urls.items() if v}
    if len(active) < 2:
        if active:
            only = list(active.keys())[0]
            n = len(active[only])
            print(f"  Only one keyword returned results ({only}: {n} jobs) — no overlap to compute.")
        else:
            print("  No keywords returned results.")
        return

    labels = list(active.keys())
    codes  = _abbrev(labels)
    col_w  = 10  # width per data column

    # Header row
    hdr_label_w = 32
    header = "  " + f"{'Keyword':<{hdr_label_w}}"
    for l in labels:
        header += f"  {codes[l]:>{col_w}}"
    header += f"  {'Unique-only':>14}"
    print(header)
    _divider(hdr_label_w + (col_w + 2) * len(labels) + 18)

    high_pairs = []
    for row_label in labels:
        row_urls = active[row_label]
        row_n    = len(row_urls)
        row      = f"  {row_label:<{hdr_label_w}}"
        for col_label in labels:
            if row_label == col_label:
                row += f"  {'—':>{col_w}}"
            else:
                col_urls = active[col_label]
                n_shared = len(row_urls & col_urls)
                pct      = n_shared / row_n * 100 if row_n > 0 else 0
                flag     = "⚠" if pct >= high_pct else " "
                cell     = f"{flag}{pct:>3.0f}% ({n_shared})"
                row     += f"  {cell:>{col_w}}"
                if pct >= high_pct and row_label < col_label:
                    high_pairs.append((row_label, col_label, n_shared, pct))

        other_urls = set().union(*(active[l] for l in labels if l != row_label))
        unique_n   = len(row_urls - other_urls)
        uniq_pct   = unique_n / row_n * 100 if row_n > 0 else 0
        flag       = "⚠" if uniq_pct < 50 and row_n > 0 else " "
        row       += f"  {flag}{unique_n:>3}/{row_n:<3} ({uniq_pct:>3.0f}%)"
        print(row)

    _divider(hdr_label_w + (col_w + 2) * len(labels) + 18)

    # Legend
    legend_items = [f"{codes[l]}={l}" for l in labels]
    legend_line  = "  "
    for item in legend_items:
        if len(legend_line) + len(item) + 2 > 100:
            print(legend_line)
            legend_line = "  "
        legend_line += item + "  "
    if legend_line.strip():
        print(legend_line)

    for r, c, n, pct in high_pairs:
        issues.append(
            f"[{source_tag}] HIGH OVERLAP: {r!r} ↔ {c!r}: {pct:.0f}% ({n} shared jobs)"
        )


# ── Apify section ─────────────────────────────────────────────────────────────

def analyze_apify(issues: list) -> tuple[int, dict]:
    """Returns (total_raw_count, {label: set_of_urls})."""
    cache_dir = ROOT / "data" / "apify_cache"
    if not cache_dir.exists():
        print("\n  [APIFY] No cache directory found.")
        return 0, {}

    today_files = []
    for f in sorted(cache_dir.glob("*.json")):
        try:
            d = json.loads(f.read_text())
            if (d.get("fetched_at") or "")[:10] == TODAY:
                today_files.append(d)
        except Exception:
            pass

    if not today_files:
        print("\n  [APIFY] No cache files from today.")
        return 0, {}

    _title_is_blocked, _title_is_blocked_for_label = _get_apify_blocklist_fns()

    # ── Ceiling check + title filter table ────────────────────────────────────
    print("\n┌─ APIFY ─────────────────────────────────────────────────────────────────┐")
    print("│  Ceiling Check & Title Filter                                           │")
    print("└─────────────────────────────────────────────────────────────────────────┘")

    col_kw = 28
    print(f"  {'Keyword':<{col_kw}}  {'Raw':>5}  {'Cap':>5}  {'Cap%':>5}  "
          f"{'Blkd':>5}  {'Blk%':>5}  {'Pass':>5}  Status")
    _divider(78)

    total_raw = 0
    total_blk = 0
    label_urls: dict[str, set] = {}

    for d in today_files:
        label   = d.get("label", "?")
        results = d.get("results", [])
        raw     = len(results)
        req_max = d.get("requested_max") or 0
        total_raw += raw

        label_urls[label] = {j.get("job_url") for j in results if j.get("job_url")}

        blocked = sum(
            1 for j in results
            if (_title_is_blocked(j.get("job_title", ""))
                or _title_is_blocked_for_label(j.get("job_title", ""), label))
        )
        total_blk += blocked
        passed    = raw - blocked
        cap_pct   = raw / req_max * 100 if req_max else 0
        blk_pct   = blocked / raw * 100 if raw else 0

        if raw == 0:
            status = "⚠ ZERO"
            issues.append(f"[Apify] ZERO RESULTS: {label} — check Apify token/credits")
        elif req_max and raw >= req_max:
            status = "⚠ AT CAP"
            issues.append(f"[Apify] CAP HIT: {label} {raw}/{req_max} — increase cap or add 2nd URL")
        elif blk_pct > 50:
            status = "⚠ HIGH BLK"
            issues.append(f"[Apify] HIGH BLOCK: {label} {blk_pct:.0f}% blocked")
        else:
            status = "✓"

        cap_str = f"{req_max:>5}" if req_max else "    —"
        cap_pct_str = f"{cap_pct:>4.0f}%" if req_max and raw > 0 else "    —"
        blk_pct_str = f"{blk_pct:>4.0f}%" if raw > 0 else "    —"
        print(f"  {label:<{col_kw}}  {raw:>5}  {cap_str}  {cap_pct_str}  "
              f"  {blocked:>4}  {blk_pct_str}  {passed:>5}  {status}")

    _divider(78)
    total_pass = total_raw - total_blk
    total_blk_pct = total_blk / total_raw * 100 if total_raw else 0
    print(f"  {'TOTAL':<{col_kw}}  {total_raw:>5}  {'':>5}  {'':>5}  "
          f"  {total_blk:>4}  {total_blk_pct:>4.0f}%  {total_pass:>5}")

    # ── Overlap matrix ─────────────────────────────────────────────────────────
    print()
    print("  Keyword Overlap  (% of row-keyword URLs also present in column-keyword)")
    _divider(78)
    _overlap_matrix(label_urls, issues, "Apify", high_pct=20)

    return total_raw, label_urls


# ── Adzuna section ────────────────────────────────────────────────────────────

def analyze_adzuna(issues: list) -> tuple[int, dict]:
    """Returns (total_raw_count, {keyword: set_of_urls})."""
    cache_dir = ROOT / "data" / "adzuna_cache"
    if not cache_dir.exists():
        print("\n  [ADZUNA] No cache directory found.")
        return 0, {}

    today_files = []
    for f in sorted(cache_dir.glob("*.json")):
        try:
            d = json.loads(f.read_text())
            if (d.get("fetched_at") or "")[:10] == TODAY:
                today_files.append(d)
        except Exception:
            pass

    if not today_files:
        print("\n  [ADZUNA] No cache files from today.")
        return 0, {}

    _title_is_relevant = _get_adzuna_filter_fn()

    # ── Ceiling check + title filter table ────────────────────────────────────
    print("\n┌─ ADZUNA ────────────────────────────────────────────────────────────────┐")
    print("│  Ceiling Check & Title Filter                                           │")
    print("└─────────────────────────────────────────────────────────────────────────┘")

    col_kw = 30
    print(f"  {'Keyword':<{col_kw}}  {'Raw':>5}  {'Cap':>5}  {'Cap%':>5}  "
          f"{'Filt':>5}  {'Flt%':>5}  {'Pass':>5}  Status")
    _divider(78)

    total_raw  = 0
    total_filt = 0
    label_urls: dict[str, set] = {}

    for d in today_files:
        keyword = d.get("keyword", "?")
        results = d.get("results", [])
        raw     = len(results)
        req_max = d.get("requested_max") or 0
        total_raw += raw

        label_urls[keyword] = {
            j.get("redirect_url") or j.get("job_url", "")
            for j in results if j.get("redirect_url") or j.get("job_url")
        }

        filtered = sum(1 for j in results if not _title_is_relevant(j.get("job_title", "")))
        total_filt += filtered
        passed   = raw - filtered
        cap_pct  = raw / req_max * 100 if req_max else 0
        flt_pct  = filtered / raw * 100 if raw else 0

        if raw == 0:
            status = "⚠ ZERO"
            issues.append(f"[Adzuna] ZERO RESULTS: {keyword!r}")
        elif req_max and raw >= req_max:
            status = "⚠ AT CAP"
            issues.append(f"[Adzuna] CAP HIT: {keyword!r} {raw}/{req_max} — increase max_jobs")
        elif flt_pct > 50:
            status = "⚠ HIGH FILT"
            issues.append(f"[Adzuna] HIGH FILTER: {keyword!r} {flt_pct:.0f}% filtered — verify allowlist")
        else:
            status = "✓"

        cap_str     = f"{req_max:>5}" if req_max else "    —"
        cap_pct_str = f"{cap_pct:>4.0f}%" if req_max and raw > 0 else "    —"
        flt_pct_str = f"{flt_pct:>4.0f}%" if raw > 0 else "    —"
        print(f"  {keyword:<{col_kw}}  {raw:>5}  {cap_str}  {cap_pct_str}  "
              f"  {filtered:>4}  {flt_pct_str}  {passed:>5}  {status}")

    _divider(78)
    total_pass = total_raw - total_filt
    total_flt_pct = total_filt / total_raw * 100 if total_raw else 0
    print(f"  {'TOTAL':<{col_kw}}  {total_raw:>5}  {'':>5}  {'':>5}  "
          f"  {total_filt:>4}  {total_flt_pct:>4.0f}%  {total_pass:>5}")

    # ── Overlap matrix (only keywords with results) ───────────────────────────
    active = {k: v for k, v in label_urls.items() if v}
    if len(active) >= 2:
        print()
        print("  Keyword Overlap  (% of row-keyword URLs also present in column-keyword)")
        _divider(78)
        _overlap_matrix(active, issues, "Adzuna", high_pct=20)

    return total_raw, label_urls


# ── Pipeline funnel ───────────────────────────────────────────────────────────

def analyze_pipeline(source: str, issues: list):
    """3-column funnel table (Apify | Adzuna | Total) + results lists."""
    scored_path = ROOT / "data" / "pipeline" / "scored_jobs.json"
    scored_data = _load_json(scored_path)
    if scored_data is None:
        print("\n  [PIPELINE] scored_jobs.json not found — run score_jobs.py first")
        return

    jobs           = scored_data.get("jobs", [])
    apify_upgrades = scored_data.get("apify_upgrades", [])
    run_stats      = scored_data.get("_run_stats") or {}

    def _src(j):
        return (j.get("job") or {}).get("_source", "unknown")

    apify_jobs  = [j for j in jobs if _src(j) == "apify"]
    adzuna_jobs = [j for j in jobs if _src(j) == "adzuna"]

    def _split(job_list):
        return {
            "shortlisted":   [j for j in job_list if j.get("status") == "Shortlisted"],
            "review_needed": [j for j in job_list if j.get("status") == "Review Needed"],
            "stale":         [j for j in job_list if j.get("status") == "Stale"],
            "new_entries":   [j for j in job_list
                              if j.get("_match_decision") in ("no_match", "new_entry")],
            "rescored":      [j for j in job_list
                              if j.get("_match_decision") == "update_in_place"],
        }

    ac = _split(apify_jobs)
    dc = _split(adzuna_jobs)
    tc = _split(jobs)

    # Aggregate totals from run_stats (authoritative for dedup/P1/P2)
    if run_stats:
        raw_total  = run_stats.get("input_jobs", 0)
        dedup      = run_stats.get("duplicates", 0)
        p1_n       = run_stats.get("p1_rejected", 0)
        p1_stale_n = run_stats.get("p1_stale", 0)
        p2_scored  = run_stats.get("p1_passed", 0)
        p2_rej_n   = run_stats.get("p2_reject", 0)
        api_cost   = p2_scored * ((15000 * 0.25 + 1000 * 1.25) / 1_000_000)
    else:
        rej_data   = _load_json(ROOT / "data" / "auto_rejected.json")
        all_today  = [r for r in (rej_data or {}).get("auto_rejected", [])
                      if (r.get("scout_run_date") or "")[:10] == TODAY] if rej_data else []
        p1_recs    = [r for r in all_today if r.get("source") == "pass1"]
        p2_recs    = [r for r in all_today if r.get("source") == "pass2"]
        raw_data   = _load_json(ROOT / "data" / "pipeline" / "raw_scrape_output.json")
        raw_total  = (len(raw_data) if isinstance(raw_data, list) else
                      len((raw_data or {}).get("jobs", (raw_data or {}).get("results", []))))
        p1_n       = len(p1_recs)
        p2_rej_n   = len(p2_recs)
        p1_stale_n = len(tc["stale"])
        p2_scored  = len(tc["shortlisted"]) + len(tc["review_needed"]) + p2_rej_n
        dedup      = max(0, raw_total - p1_n - p1_stale_n - p2_scored)
        api_cost   = p2_scored * ((15000 * 0.25 + 1000 * 1.25) / 1_000_000)

    # Raw per source from raw_scrape_output
    raw_data    = _load_json(ROOT / "data" / "pipeline" / "raw_scrape_output.json")
    raw_list    = (raw_data if isinstance(raw_data, list) else
                   (raw_data or {}).get("jobs", (raw_data or {}).get("results", [])))
    raw_apify   = sum(1 for j in raw_list if j.get("_source") == "apify")
    raw_adzuna  = sum(1 for j in raw_list if j.get("_source") == "adzuna")

    # P2 reject — split proportionally (can't split exactly without extra metadata)
    total_scored   = len(apify_jobs) + len(adzuna_jobs)
    apify_share    = len(apify_jobs) / total_scored if total_scored else 0
    p2_rej_apify   = round(p2_rej_n * apify_share)
    p2_rej_adzuna  = p2_rej_n - p2_rej_apify

    # ── 3-column funnel table ─────────────────────────────────────────────────
    print("\n┌─ PIPELINE FUNNEL ───────────────────────────────────────────────────────┐")
    print("│  Dedup / P1 / P2 split is combined-only (can't split by source in P1)  │")
    print("└─────────────────────────────────────────────────────────────────────────┘")

    lw, cw = 30, 9  # label width, column width

    def _hdr():
        print(f"  {'Stage':<{lw}}  {'Apify':>{cw}}  {'Adzuna':>{cw}}  {'Total':>{cw}}")
        _divider(lw + cw * 3 + 8)

    def _row(label, a, d, t, note=""):
        a_s = f"{a:>{cw}}" if a != "—" else f"{'—':>{cw}}"
        d_s = f"{d:>{cw}}" if d != "—" else f"{'—':>{cw}}"
        t_s = f"{t:>{cw}}" if t != "—" else f"{'—':>{cw}}"
        suffix = f"  {note}" if note else ""
        print(f"  {label:<{lw}}  {a_s}  {d_s}  {t_s}{suffix}")

    _hdr()
    _row("Raw scraped",          raw_apify, raw_adzuna, raw_total)
    _row("Duplicates skipped",   "—", "—", f"-{dedup}",   f"({dedup/raw_total*100:.0f}% dedup rate)" if raw_total else "")
    _row("P1 native rejected",   "—", "—", f"-{p1_n}")
    _row("P1 stale (>3d old)",   "—", "—", f"-{p1_stale_n}" if p1_stale_n else "0")
    _row("Claude API scored",    "—", "—", p2_scored,    f"~${api_cost:.4f}")
    _divider(lw + cw * 3 + 8)
    _row("✓  Shortlisted",       len(ac["shortlisted"]),   len(dc["shortlisted"]),   len(tc["shortlisted"]))
    _row("⚠  Review Needed",     len(ac["review_needed"]), len(dc["review_needed"]), len(tc["review_needed"]))
    _row("✗  API Rejected",      p2_rej_apify,             p2_rej_adzuna,            p2_rej_n,     "(approx split)")
    if tc["stale"]:
        _row("~  Stale",         len(ac["stale"]),         len(dc["stale"]),         len(tc["stale"]))
    _divider(lw + cw * 3 + 8)
    _row("New entries added",    len(ac["new_entries"]),   len(dc["new_entries"]),   len(tc["new_entries"]))
    _row("Re-scored existing",   len(ac["rescored"]),      len(dc["rescored"]),      len(tc["rescored"]))
    if apify_upgrades:
        _row("Apify field upgrades", len(apify_upgrades), "—", len(apify_upgrades))

    if len(tc["shortlisted"]) == 0 and len(tc["review_needed"]) == 0:
        issues.append("ZERO SHORTLIST: 0 actionable results today — check scoring thresholds")

    # ── Role focus breakdown (from auto_rejected.json — today's Pass 2 rejects only) ──
    rej_data   = _load_json(ROOT / "data" / "auto_rejected.json")
    rej_today  = [r for r in (rej_data or {}).get("auto_rejected", [])
                  if (r.get("scout_run_date") or "")[:10] == TODAY
                  and r.get("source") == "pass2"
                  and r.get("role_focus")]
    if rej_today:
        focus_counts: dict[str, int] = {}
        for r in rej_today:
            f = r.get("role_focus", "")
            if f:
                focus_counts[f] = focus_counts.get(f, 0) + 1
        blocking = {k: v for k, v in focus_counts.items()
                    if k in ("bi_reporting", "analytics_engineering", "management_consulting")}
        if blocking:
            print(f"\n  Role Focus Rejections (Gate 6 — Pass 2):")
            _divider(50)
            for focus, count in sorted(blocking.items(), key=lambda x: -x[1]):
                print(f"  {focus:<30}  {count:>3} rejected")
            _divider(50)

    # ── Shortlisted ───────────────────────────────────────────────────────────
    shortlisted   = tc["shortlisted"]
    review_needed = tc["review_needed"]

    def _sort_key(j):
        score_obj = j.get("score") or {}
        bd        = score_obj.get("fit_score_breakdown") or {}
        return (-(bd.get("role_title") or 0), -(score_obj.get("fit_score") or 0))

    shortlisted.sort(key=_sort_key)
    review_needed.sort(key=_sort_key)

    def _results_table(job_list, header):
        if not job_list:
            print(f"\n  {header}: (none)")
            return
        print(f"\n  {header}  ({len(job_list)})")
        _divider(90)
        print(f"  {'Score':>5}  {'Tier':<6}  {'Src':<5}  {'Company':<28}  Role")
        _divider(90)

        def _print_rows(sub_list):
            for j in sub_list:
                score_obj = j.get("score") or {}
                bd        = score_obj.get("fit_score_breakdown") or {}
                fit       = score_obj.get("fit_score") or 0
                tier      = _title_tier_label(bd.get("role_title"))
                company   = (j.get("_resolved_company")
                             or (j.get("job") or {}).get("company_name", "?"))
                role      = (j.get("job") or {}).get("job_title", "?")
                src       = (j.get("job") or {}).get("_source", "?")[:3].upper()
                company_t = company[:27] + "…" if len(company) > 28 else company
                role_t    = role[:47] + "…" if len(role) > 48 else role
                print(f"  {fit:>5}  {tier:<6}  {src:<5}  {company_t:<28}  {role_t}")

        apify_list  = [j for j in job_list if (j.get("job") or {}).get("_source") == "apify"]
        adzuna_list = [j for j in job_list if (j.get("job") or {}).get("_source") == "adzuna"]
        other_list  = [j for j in job_list
                       if (j.get("job") or {}).get("_source") not in ("apify", "adzuna")]

        if apify_list:
            print(f"  ── Apify ({len(apify_list)}) {'─' * max(0, 77 - len(str(len(apify_list))))}")
            _print_rows(apify_list)
        if adzuna_list:
            print(f"  ── Adzuna ({len(adzuna_list)}) {'─' * max(0, 76 - len(str(len(adzuna_list))))}")
            _print_rows(adzuna_list)
        if other_list:
            print(f"  ── Other ({len(other_list)}) {'─' * max(0, 77 - len(str(len(other_list))))}")
            _print_rows(other_list)

        _divider(90)

    print()
    _results_table(shortlisted,   "SHORTLISTED")
    _results_table(review_needed, "REVIEW NEEDED")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    source = "auto"
    args   = sys.argv[1:]
    if "--source" in args:
        idx = args.index("--source")
        if idx + 1 < len(args):
            source = args[idx + 1]

    width = 80
    print(f"\n{'═' * width}")
    print(f"  POST-SCOUT ANALYSIS — {TODAY}  |  source: {source}")
    print(f"{'═' * width}")

    issues = []

    if source in ("apify", "auto"):
        analyze_apify(issues)

    if source in ("adzuna", "auto"):
        analyze_adzuna(issues)

    analyze_pipeline(source, issues)

    print(f"\n┌─ ISSUES {'─' * 69}┐")
    if issues:
        for issue in issues:
            print(f"  ⚠  {issue}")
    else:
        print("  ✓  No issues detected.")
    print(f"└{'─' * 79}┘\n")

    sys.exit(0)


if __name__ == "__main__":
    main()
