#!/usr/bin/env python3
"""Refresh schedule-live.json from the official Chance Liga schedule."""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

SOURCE_URL = "https://www.chanceliga.cz/rozpis-zapasu"
OUTPUT_PATH = Path(__file__).resolve().parents[1] / "schedule-live.json"
SEASON = "2026/27"

ROW_RE = re.compile(r"<tr\b[^>]*>.*?</tr>", re.IGNORECASE | re.DOTALL)
ROUND_RE = re.compile(
    r'class=["\'][^"\']*\bheader\b[^"\']*["\'][^>]*>.*?(\d+)\s*\.\s*kolo',
    re.IGNORECASE | re.DOTALL,
)
GAME_RE = re.compile(
    r'<tr\b[^>]*class=["\'][^"\']*\bgame\b[^"\']*["\']',
    re.IGNORECASE,
)


def clean_markup(value: str) -> str:
    return re.sub(
        r"\s+",
        " ",
        html.unescape(re.sub(r"<[^>]+>", " ", value)),
    ).strip()


def cell_markup(row: str, class_pattern: str) -> str:
    match = re.search(
        rf'<td\b[^>]*class=["\'][^"\']*\b{class_pattern}\b[^"\']*["\'][^>]*>(.*?)</td>',
        row,
        re.IGNORECASE | re.DOTALL,
    )
    return match.group(1) if match else ""


def team_name(row: str, side: str) -> str:
    cell = cell_markup(row, rf"team\s+{side}")
    visible = re.search(
        r'<span\b[^>]*class=["\'][^"\']*\bhidden-xs\b[^"\']*["\'][^>]*>(.*?)</span>',
        cell,
        re.IGNORECASE | re.DOTALL,
    )
    return clean_markup(visible.group(1) if visible else cell)


def parse_schedule_html(source: str) -> list[dict]:
    matches: list[dict] = []
    current_round: int | None = None
    for row in ROW_RE.findall(source):
        round_match = ROUND_RE.search(row)
        if round_match:
            current_round = int(round_match.group(1))
            continue
        if not GAME_RE.search(row) or current_round is None:
            continue

        date_match = re.search(
            r"(\d{2})/(\d{2})/(\d{4})",
            clean_markup(cell_markup(row, "date")),
        )
        time_text = clean_markup(cell_markup(row, "time"))
        match_link = re.search(
            r'href=["\'](/zapas/(\d+)-[^"\']+)["\']',
            row,
            re.IGNORECASE,
        )
        if not date_match or not match_link:
            continue

        day, month, year = date_match.groups()
        score_text = clean_markup(cell_markup(row, "score"))
        matches.append(
            {
                "round": current_round,
                "date": f"{year}-{month}-{day}",
                "time": time_text if re.fullmatch(r"\d{2}:\d{2}", time_text) else None,
                "home": team_name(row, "home"),
                "away": team_name(row, "away"),
                "score": score_text if re.fullmatch(r"\d+:\d+", score_text) else None,
                "officialMatchId": match_link.group(2),
                "officialUrl": "https://www.chanceliga.cz" + match_link.group(1),
            }
        )
    return matches


def validate(matches: list[dict]) -> None:
    if len(matches) != 240:
        raise RuntimeError(f"Expected 240 matches, parsed {len(matches)}.")
    ids = {match["officialMatchId"] for match in matches}
    if len(ids) != 240:
        raise RuntimeError("Official match IDs are not unique.")
    rounds: dict[int, int] = {}
    for match in matches:
        rounds[match["round"]] = rounds.get(match["round"], 0) + 1
    if set(rounds) != set(range(1, 31)) or any(
        count != 8 for count in rounds.values()
    ):
        raise RuntimeError("Expected 30 rounds with eight matches each.")


def fetch_source() -> str:
    request = Request(
        SOURCE_URL,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; ChanceLigaScheduleUpdater/1.0)",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "cs,en;q=0.8",
        },
    )
    with urlopen(request, timeout=45) as response:
        if response.status != 200:
            raise RuntimeError(
                f"Official schedule returned HTTP {response.status}."
            )
        return response.read().decode("utf-8", errors="replace")


def write_if_changed(matches: list[dict]) -> bool:
    previous = None
    if OUTPUT_PATH.exists():
        try:
            previous = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = None
    if (
        previous
        and previous.get("season") == SEASON
        and previous.get("matches") == matches
    ):
        print("Official schedule has not changed.")
        return False

    payload = {
        "season": SEASON,
        "source": "Chance Liga",
        "sourceUrl": SOURCE_URL,
        "updatedAt": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "matches": matches,
    }
    OUTPUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Updated {OUTPUT_PATH.name}: {len(matches)} matches.")
    return True


def main() -> None:
    matches = parse_schedule_html(fetch_source())
    validate(matches)
    write_if_changed(matches)


if __name__ == "__main__":
    main()
