"""
organize_outputs.py — Keep output folders sorted into ready/ and done/.

  ready/ — Prep Complete applications awaiting submission
  done/  — Applied, Withdrawn, Rejected, Under Review, etc.

Reads job_tracker.json, moves each application folder to the correct
subfolder, updates resume_path and cover_letter_path in the tracker,
then writes the tracker back.

Also sweeps the outputs/applications/ root for any folders not tracked
(excluding _test_output) and moves them to done/ as a safety net.

Usage:
  python3 scripts/organize_outputs.py          # run standalone
  python3 scripts/organize_outputs.py --dry-run  # preview without moving

Importable:
  from scripts.organize_outputs import organize_outputs
  organize_outputs()
"""

import json
import shutil
import sys
from pathlib import Path

BASE_DIR   = Path(__file__).parent.parent
TRACKER    = BASE_DIR / "data" / "job_tracker.json"
APPS_DIR   = BASE_DIR / "outputs" / "applications"
READY_DIR  = APPS_DIR / "ready"
DONE_DIR   = APPS_DIR / "done"
SKIP_DIRS  = {"_test_output", "ready", "done"}

READY_STATUSES = {"Prep Complete", "Referral"}


def organize_outputs(dry_run: bool = False) -> dict:
    """
    Move application folders to ready/ or done/ based on tracker status.
    Returns summary dict: {"moved": int, "already_correct": int, "untracked": int}.
    """
    READY_DIR.mkdir(parents=True, exist_ok=True)
    DONE_DIR.mkdir(parents=True, exist_ok=True)

    tracker = json.loads(TRACKER.read_text())
    apps    = tracker["applications"]

    moved            = 0
    already_correct  = 0
    untracked        = 0
    tracker_modified = False

    # ── Move tracker-referenced folders ──────────────────────────────────────
    for entry in apps:
        resume_path = entry.get("resume_path")
        if not resume_path:
            continue

        resume_p = BASE_DIR / resume_path
        # Derive the application folder (parent of the PDF file)
        folder = resume_p.parent

        status     = entry.get("status", "")
        target_sub = READY_DIR if status in READY_STATUSES else DONE_DIR
        target_dir = target_sub / folder.name

        if folder == target_dir:
            already_correct += 1
            continue

        if not folder.exists():
            # Folder already moved or missing — update path if it now exists at target
            if target_dir.exists():
                already_correct += 1
                # Ensure tracker paths reflect the existing location
                _update_paths(entry, folder.name, target_sub, BASE_DIR)
                tracker_modified = True
            else:
                print(f"  ⚠ Folder not found: {folder} — skipping")
            continue

        print(f"  {'[DRY RUN] ' if dry_run else ''}{'→' if not dry_run else 'would move'} "
              f"{folder.name}  ({status})  →  {target_sub.name}/")

        if not dry_run:
            shutil.move(str(folder), str(target_dir))
            _update_paths(entry, folder.name, target_sub, BASE_DIR)
            tracker_modified = True

        moved += 1

    # ── Sweep root for untracked folders ────────────────────────────────────
    tracked_names = set()
    for entry in apps:
        rp = entry.get("resume_path", "") or ""
        if rp:
            tracked_names.add(Path(rp).parent.name)

    for child in APPS_DIR.iterdir():
        if not child.is_dir():
            continue
        if child.name in SKIP_DIRS:
            continue
        if child.name in tracked_names:
            continue
        # Untracked folder in root — move to done/ as safety net
        target = DONE_DIR / child.name
        print(f"  {'[DRY RUN] ' if dry_run else ''}untracked folder → done/: {child.name}")
        if not dry_run:
            shutil.move(str(child), str(target))
        untracked += 1

    # ── Write tracker if modified ─────────────────────────────────────────────
    if tracker_modified and not dry_run:
        TRACKER.write_text(json.dumps(tracker, indent=2, ensure_ascii=False))

    print(f"\n[organize_outputs] moved={moved}  already_correct={already_correct}  "
          f"untracked_swept={untracked}")

    return {"moved": moved, "already_correct": already_correct, "untracked": untracked}


def _update_paths(entry: dict, folder_name: str, target_sub: Path, base_dir: Path):
    """Rewrite resume_path and cover_letter_path to point to new subfolder."""
    for field in ("resume_path", "cover_letter_path"):
        old = entry.get(field)
        if not old:
            continue
        filename = Path(old).name
        new_path = target_sub / folder_name / filename
        entry[field] = str(new_path.relative_to(base_dir))


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("[organize_outputs] DRY RUN — no files will be moved\n")
    organize_outputs(dry_run=dry_run)
