#!/usr/bin/env python3
"""Refresh schedule-live.json from the official Chance Liga schedule."""

from __future__ import annotations

import html
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
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
KNOWN_DATE_TBA_VALUES = {
    "-",
    "tba",
    "termín bude upřesněn",
    "termín bude upřesněný",
    "bude upřesněno",
}


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


def parse_date_state(date_text: str, match_id: str) -> tuple[str | None, str | None]:
    date_match = re.search(r"(\d{2})/(\d{2})/(\d{4})", date_text)
    if date_match:
        day, month, year = date_match.groups()
        return f"{year}-{month}-{day}", None

    normalized = date_text.casefold().strip()
    if "odložen" in normalized:
        return None, "postponed"
    if normalized in KNOWN_DATE_TBA_VALUES:
        return None, "date_tba"
    raise RuntimeError(
        f"Match {match_id} has an unsupported official date value: {date_text!r}."
    )


def parse_time(time_text: str, match_id: str) -> str | None:
    if re.fullmatch(r"\d{2}:\d{2}", time_text):
        return time_text
    if time_text in {"", "-"}:
        return None
    raise RuntimeError(
        f"Match {match_id} has an unsupported official time value: {time_text!r}."
    )


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

        match_link = re.search(
            r'href=["\'](/zapas/(\d+)-[^"\']+)["\']',
            row,
            re.IGNORECASE,
        )
        if not match_link:
            raise RuntimeError(
                f"Round {current_round} contains a game row without an official match link."
            )

        match_id = match_link.group(2)
        date, status = parse_date_state(
            clean_markup(cell_markup(row, "date")), match_id
        )
        match_time = parse_time(clean_markup(cell_markup(row, "time")), match_id)
        score_text = clean_markup(cell_markup(row, "score"))
        match = {
            "round": current_round,
            "date": date,
            "time": match_time,
            "home": team_name(row, "home"),
            "away": team_name(row, "away"),
            "score": score_text if re.fullmatch(r"\d+:\d+", score_text) else None,
            "officialMatchId": match_id,
            "officialUrl": "https://www.chanceliga.cz" + match_link.group(1),
        }
        if status:
            match["status"] = status
        matches.append(match)
    return matches


def validate(matches: list[dict]) -> None:
    rounds: dict[int, int] = {}
    for match in matches:
        rounds[match["round"]] = rounds.get(match["round"], 0) + 1
    errors: list[str] = []
    if len(matches) != 240:
        errors.append(f"expected 240 matches, parsed {len(matches)}")
    ids = [match.get("officialMatchId") for match in matches]
    duplicate_ids = sorted({match_id for match_id in ids if ids.count(match_id) > 1})
    if duplicate_ids:
        errors.append(f"duplicate official match IDs: {', '.join(duplicate_ids)}")
    wrong_rounds = [
        f"{round_number}={rounds.get(round_number, 0)}"
        for round_number in range(1, 31)
        if rounds.get(round_number, 0) != 8
    ]
    if wrong_rounds:
        errors.append("round counts: " + ", ".join(wrong_rounds))
    incomplete = [
        str(match.get("officialMatchId") or "unknown")
        for match in matches
        if not match.get("home") or not match.get("away") or not match.get("officialUrl")
    ]
    if incomplete:
        errors.append("incomplete fixtures: " + ", ".join(incomplete))
    invalid_undated = [
        str(match.get("officialMatchId") or "unknown")
        for match in matches
        if match.get("date") is None
        and match.get("status") not in {"postponed", "date_tba"}
    ]
    if invalid_undated:
        errors.append("undated fixtures without a valid status: " + ", ".join(invalid_undated))
    scored_without_date = [
        str(match.get("officialMatchId") or "unknown")
        for match in matches
        if match.get("score") and match.get("date") is None
    ]
    if scored_without_date:
        errors.append("scored fixtures without a date: " + ", ".join(scored_without_date))
    if errors:
        raise RuntimeError("Official schedule validation failed: " + "; ".join(errors) + ".")


def fetch_source(attempts: int = 3) -> str:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        request = Request(
            SOURCE_URL,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; ChanceLigaScheduleUpdater/1.1)",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "cs,en;q=0.8",
            },
        )
        try:
            with urlopen(request, timeout=45) as response:
                if response.status != 200:
                    raise RuntimeError(
                        f"Official schedule returned HTTP {response.status}."
                    )
                return response.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(2 ** (attempt - 1))
    raise RuntimeError(
        f"Could not download the official schedule after {attempts} attempts: {last_error}"
    ) from last_error


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
