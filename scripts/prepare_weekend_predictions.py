"""Refresh every server input before producing a short-lived, hash-bound receipt."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
INPUTS = ("schedule-live.json", "rosters-live.json", "match-context-live.json",
          "index.html", "prediction-archive-core.js")


def now():
    return datetime.now(timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    # Remove a receipt from a failed/retried attempt before any network request.
    args.receipt.unlink(missing_ok=True)
    started = now()
    for command in (
        ["scripts/update_schedule.py"],
        ["scripts/update_rosters.py", "--workers", "8"],
        ["scripts/update_livesport_rosters.py"],
        ["scripts/update_match_context.py", "--strict"],
    ):
        subprocess.run([sys.executable, *command], cwd=ROOT, check=True)
    receipt = {
        "schemaVersion": 1, "startedAt": started, "checkedAt": now(),
        "runId": os.environ.get("GITHUB_RUN_ID"),
        "checks": ["official-schedule", "official-rosters-and-values", "livesport-stats", "strict-context"],
        "hashes": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in INPUTS},
    }
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
