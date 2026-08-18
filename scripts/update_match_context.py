#!/usr/bin/env python3
"""Build the live prediction context used by the Chance Liga dashboard.

The generated file is deliberately separate from browser/user data.  It contains
only reproducible inputs: Livesport predicted/official line-ups, missing players
and the current summer's club friendlies.  Locked prediction snapshots copy the
relevant rows, so later refreshes cannot rewrite a historical prediction.
"""

from __future__ import annotations

import argparse
import json
import re
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SEASON = "2026/27"
MODEL_VERSION = "2026.07-preseason-xi-v1"
PROJECT_ID = 1
LIVESPORT_ROOT = "https://www.livesport.cz"
GRAPHQL_ROOT = "https://1.ds.lsapp.eu/pq_graphql"
ROOT = Path(__file__).resolve().parents[1]
SCHEDULE_PATH = ROOT / "schedule-live.json"
OUTPUT_PATH = ROOT / "match-context-live.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/138 Safari/537.36"
)

TEAM_SEGMENTS = {
    "Hradec Králové": "hradec-kralove-vFXjbHms",
    "Pardubice": "pardubice-Ys4YYBPn",
    "Jablonec": "jablonec-CM8ySpMH",
    "Sigma Olomouc": "sigma-olomouc-drA4fSL4",
    "Slavia Prague": "slavia-praha-viXGgnyB",
    "Slovácko": "slovacko-MNEDyOlF",
    "Zbrojovka Brno": "zbrojovka-brno-4d5TT6i5",
    "Sparta Prague": "sparta-praha-6qA358jH",
    "Teplice": "teplice-r9XWmtLq",
    "Bohemians 1905": "bohemians-1905-fuXqHnxa",
    "Viktoria Plzeň": "viktoria-plzen-2LA0e86b",
    "Slovan Liberec": "slovan-liberec-4bp6yRjU",
    "Zlín": "zlin-C09N1Ikd",
    "Baník Ostrava": "banik-ostrava-lI6ddlih",
    "Artis Brno": "artis-brno-zHLktbZ1",
    "Mladá Boleslav": "mlada-boleslav-0f7GpAMu",
}

SCHEDULE_TEAM_ALIASES = {
    "SK Slavia Praha": "Slavia Prague",
    "AC Sparta Praha": "Sparta Prague",
    "FK Jablonec": "Jablonec",
    "FC Viktoria Plzeň": "Viktoria Plzeň",
    "FC Slovan Liberec": "Slovan Liberec",
    "FC Hradec Králové": "Hradec Králové",
    "SK Sigma Olomouc": "Sigma Olomouc",
    "SK Artis Brno": "Artis Brno",
    "FC Zlín": "Zlín",
    "FK Teplice": "Teplice",
    "Bohemians Praha 1905": "Bohemians 1905",
    "FK Pardubice": "Pardubice",
    "FK Mladá Boleslav": "Mladá Boleslav",
    "1.FC Slovácko": "Slovácko",
    "FC Baník Ostrava": "Baník Ostrava",
    "FC Zbrojovka Brno": "Zbrojovka Brno",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize(value: str) -> str:
    text = unicodedata.normalize("NFD", value or "")
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = (
        text.replace("ø", "r")
        .replace("Ø", "r")
        .replace("ł", "l")
        .replace("Ł", "l")
        .replace("đ", "d")
        .replace("Đ", "d")
        .replace("æ", "ae")
        .replace("Æ", "ae")
        .replace("œ", "oe")
        .replace("Œ", "oe")
        .lower()
    )
    return " ".join(re.findall(r"[a-z0-9]+", text))


def team_participant_id(team: str) -> str:
    return TEAM_SEGMENTS[team].rsplit("-", 1)[-1]


def team_page_path(team: str) -> str:
    slug, participant_id = TEAM_SEGMENTS[team].rsplit("-", 1)
    return f"{slug}/{participant_id}"


def fetch_text(url: str, *, referer: str | None = None, attempts: int = 3) -> str:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "cs,en;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=45) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status} for {url}")
                return response.read().decode("utf-8", errors="replace")
        except Exception as exc:  # urllib uses several unrelated exception types
            error = exc
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Could not fetch {url}: {error}")


def fetch_json(url: str, *, referer: str) -> dict[str, Any]:
    return json.loads(fetch_text(url, referer=referer))


def parse_environment(source: str) -> dict[str, Any]:
    marker = "window.environment = "
    start = source.find(marker)
    if start < 0:
        raise RuntimeError("Livesport page does not contain window.environment.")
    try:
        environment, _ = json.JSONDecoder().raw_decode(source[start + len(marker) :])
    except json.JSONDecodeError as exc:
        raise RuntimeError("Livesport environment JSON is invalid.") from exc
    if not isinstance(environment, dict):
        raise RuntimeError("Livesport environment has an unexpected shape.")
    return environment


def initial_feed(source: str, name: str) -> str:
    match = re.search(
        rf'cjs\.initialFeeds\["{re.escape(name)}"\]\s*=\s*\{{\s*data:\s*`(.*?)`',
        source,
        re.DOTALL,
    )
    if not match:
        raise RuntimeError(f"Livesport team page does not contain the {name!r} feed.")
    return match.group(1)


def parse_feed_matches(feed: str) -> list[dict[str, Any]]:
    division, separator = "\u00f7", "\u00ac"
    tournament: dict[str, str] = {}
    matches: list[dict[str, Any]] = []
    for record in feed.split("~"):
        fields: dict[str, str] = {}
        for token in record.strip(separator).split(separator):
            if division in token:
                key, value = token.split(division, 1)
                fields[key] = value
        if "ZA" in fields:
            tournament = fields
        if "AA" not in fields:
            continue
        row: dict[str, Any] = {
            "livesportMatchId": fields["AA"],
            "timestamp": int(fields.get("AD") or 0),
            "home": fields.get("AE") or "",
            "away": fields.get("AF") or "",
            "homeParticipantId": fields.get("PX") or "",
            "awayParticipantId": fields.get("PY") or "",
            "competition": tournament.get("ZK") or tournament.get("ZA") or "",
            "competitionPath": tournament.get("ZL") or "",
        }
        if fields.get("AG", "").isdigit() and fields.get("AH", "").isdigit():
            row["homeGoals"] = int(fields["AG"])
            row["awayGoals"] = int(fields["AH"])
            row["score"] = f"{fields['AG']}:{fields['AH']}"
        matches.append(row)
    return matches


def friendly_summary(matches: list[dict[str, Any]], participant_id: str) -> dict[str, Any]:
    wins = draws = losses = goals_for = goals_against = 0
    ordered = sorted(matches, key=lambda row: row["timestamp"], reverse=True)
    for match in ordered:
        is_home = match["homeParticipantId"] == participant_id
        gf = match["homeGoals"] if is_home else match["awayGoals"]
        ga = match["awayGoals"] if is_home else match["homeGoals"]
        goals_for += gf
        goals_against += ga
        if gf > ga:
            wins += 1
        elif gf == ga:
            draws += 1
        else:
            losses += 1
    games = len(ordered)
    points = wins * 3 + draws
    ppg = points / games if games else 0.0
    points_score = points / (games * 3) if games else 0.5
    goal_score = max(0.05, min(0.95, 0.5 + ((goals_for - goals_against) / max(1, games)) / 4))
    score = 0.7 * points_score + 0.3 * goal_score if games else 0.5
    return {
        "games": games,
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "goalsFor": goals_for,
        "goalsAgainst": goals_against,
        "ppg": round(ppg, 3),
        "score": round(score, 4),
    }


def build_preseason(schedule: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    dated_matches = [row for row in schedule["matches"] if row.get("date")]
    if not dated_matches:
        raise RuntimeError("The official schedule does not contain any dated fixtures.")
    first_date = min(datetime.fromisoformat(row["date"]) for row in dated_matches)
    window_start = datetime(first_date.year, 6, 1)
    old_teams = ((previous.get("preseason") or {}).get("teams") or {})
    teams: dict[str, Any] = {}
    for team in TEAM_SEGMENTS:
        participant_id = team_participant_id(team)
        url = f"{LIVESPORT_ROOT}/tym/{team_page_path(team)}/vysledky/"
        try:
            source = fetch_text(url)
            candidates = parse_feed_matches(initial_feed(source, "results"))
            friendlies = []
            seen: set[str] = set()
            for row in candidates:
                when = datetime.fromtimestamp(row["timestamp"], timezone.utc).replace(tzinfo=None)
                competition_key = normalize(row["competition"] + " " + row["competitionPath"])
                belongs = participant_id in {row["homeParticipantId"], row["awayParticipantId"]}
                if not belongs or not (window_start <= when < first_date):
                    continue
                if "atelske zapasy klubu" not in competition_key and "club friendly" not in competition_key:
                    continue
                if "score" not in row or row["livesportMatchId"] in seen:
                    continue
                seen.add(row["livesportMatchId"])
                is_home = row["homeParticipantId"] == participant_id
                gf = row["homeGoals"] if is_home else row["awayGoals"]
                ga = row["awayGoals"] if is_home else row["homeGoals"]
                friendlies.append(
                    {
                        "livesportMatchId": row["livesportMatchId"],
                        "date": when.date().isoformat(),
                        "home": row["home"],
                        "away": row["away"],
                        "score": row["score"],
                        "isHome": is_home,
                        "goalsFor": gf,
                        "goalsAgainst": ga,
                        "outcome": "W" if gf > ga else "D" if gf == ga else "L",
                        "sourceUrl": url,
                    }
                )
            friendlies.sort(key=lambda row: row["date"], reverse=True)
            teams[team] = {
                "participantId": participant_id,
                "source": "Livesport team results",
                "sourceUrl": url,
                "matches": friendlies,
                "summary": friendly_summary(
                    [
                        {
                            **row,
                            "timestamp": int(datetime.fromisoformat(row["date"]).replace(tzinfo=timezone.utc).timestamp()),
                            "homeParticipantId": participant_id if row["isHome"] else "opponent",
                            "awayParticipantId": "opponent" if row["isHome"] else participant_id,
                            "homeGoals": row["goalsFor"] if row["isHome"] else row["goalsAgainst"],
                            "awayGoals": row["goalsAgainst"] if row["isHome"] else row["goalsFor"],
                        }
                        for row in friendlies
                    ],
                    participant_id,
                ),
            }
        except Exception as exc:
            if team in old_teams:
                teams[team] = old_teams[team]
                teams[team]["staleReason"] = str(exc)
            else:
                raise RuntimeError(f"Could not build preseason form for {team}: {exc}") from exc
    return {
        "windowStart": window_start.date().isoformat(),
        "windowEnd": (first_date - timedelta(days=1)).date().isoformat(),
        "teams": teams,
    }


def graphql(event_id: str, query_hash: str, referer: str) -> dict[str, Any]:
    query = urllib.parse.urlencode(
        {"_hash": query_hash, "eventId": event_id, "projectId": PROJECT_ID}
    )
    payload = fetch_json(f"{GRAPHQL_ROOT}?{query}", referer=referer)
    if payload.get("errors"):
        raise RuntimeError(f"Livesport GraphQL {query_hash} returned errors: {payload['errors']}")
    event = (payload.get("data") or {}).get("findEventById")
    if not isinstance(event, dict):
        raise RuntimeError(f"Livesport GraphQL {query_hash} returned no event.")
    return event


def side_name(value: Any) -> str:
    return str((((value or {}).get("type") or {}).get("side") or "")).upper()


def role_position(player: dict[str, Any]) -> str | None:
    role = " ".join(
        str(item.get("title") or "") for item in (player.get("playerRoles") or [])
    )
    key = normalize(role)
    if "brankar" in key:
        return "GK"
    if "obrance" in key:
        return "D"
    if "zaloznik" in key:
        return "M"
    if "utocnik" in key:
        return "A"
    return None


def player_row(raw: dict[str, Any]) -> dict[str, Any]:
    participant = raw.get("participant") or {}
    return {
        "livesportPlayerId": str(raw.get("participantId") or raw.get("id") or ""),
        "name": raw.get("fieldName") or raw.get("name") or raw.get("listName") or "",
        "listName": raw.get("listName") or raw.get("name") or raw.get("fieldName") or "",
        "number": raw.get("number"),
        "position": role_position(raw),
        "profileSlug": participant.get("url"),
    }


def lineup_by_side(event: dict[str, Any], field: str) -> dict[str, list[dict[str, Any]]]:
    output = {"HOME": [], "AWAY": []}
    for participant in event.get("eventParticipants") or []:
        side = side_name(participant)
        if side not in output:
            continue
        lineup = participant.get(field) or {}
        output[side] = [player_row(row) for row in (lineup.get("players") or [])]
    return output


def missing_by_side(event: dict[str, Any]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    output = {
        "HOME": {"confirmed": [], "uncertain": []},
        "AWAY": {"confirmed": [], "uncertain": []},
    }
    for participant in event.get("eventParticipants") or []:
        side = side_name(participant)
        if side not in output:
            continue
        lineup = participant.get("lineup") or {}
        for source_key, target_key in (
            ("missingPlayers", "confirmed"),
            ("unsureMissingPlayers", "uncertain"),
        ):
            for raw in lineup.get(source_key) or []:
                row = player_row(raw.get("player") or {})
                row["reason"] = raw.get("reason") or "Neuvedeno"
                output[side][target_key].append(row)
    return output


def match_url(match: dict[str, Any]) -> str:
    return (
        f"{LIVESPORT_ROOT}/zapas/fotbal/"
        f"{TEAM_SEGMENTS[match['home']]}/{TEAM_SEGMENTS[match['away']]}/"
    )


def build_fixture_context(match: dict[str, Any]) -> dict[str, Any]:
    url = match_url(match)
    source = fetch_text(url)
    environment = parse_environment(source)
    event_id = str(environment.get("event_id_c") or "")
    if not event_id:
        raise RuntimeError("Livesport match page has no event_id_c.")
    predicted = lineup_by_side(graphql(event_id, "dplie", url), "predictedLineup")
    official = lineup_by_side(graphql(event_id, "dlie2", url), "lineup")
    missing = missing_by_side(graphql(event_id, "dmpe2", url))

    def team_context(side: str) -> dict[str, Any]:
        official_players = official[side]
        predicted_players = predicted[side]
        if len(official_players) >= 7:
            source_name, selected = "official", official_players
        elif len(predicted_players) >= 7:
            source_name, selected = "predicted", predicted_players
        else:
            source_name, selected = "proxy", []
        return {
            "source": source_name,
            "players": selected,
            "officialPlayers": official_players,
            "predictedPlayers": predicted_players,
            "missing": missing[side]["confirmed"],
            "uncertain": missing[side]["uncertain"],
        }

    return {
        "officialMatchId": str(match["officialMatchId"]),
        "round": int(match["round"]),
        "date": match["date"],
        "time": match.get("time"),
        "home": match["home"],
        "away": match["away"],
        "livesportMatchId": event_id,
        "source": "Livesport",
        "sourceUrl": url,
        "homeContext": team_context("HOME"),
        "awayContext": team_context("AWAY"),
    }


def build_fixtures(schedule: dict[str, Any], previous: dict[str, Any], horizon_days: int) -> list[dict[str, Any]]:
    old = {
        str(row.get("officialMatchId")): row
        for row in previous.get("fixtures") or []
        if row.get("officialMatchId")
    }
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=1)).date()
    end = (now + timedelta(days=horizon_days)).date()
    selected = [
        row
        for row in schedule["matches"]
        if row.get("date")
        and start <= datetime.fromisoformat(row["date"]).date() <= end
        and not row.get("score")
    ]
    fixtures: list[dict[str, Any]] = []
    for match in selected:
        key = str(match["officialMatchId"])
        try:
            fixtures.append(build_fixture_context(match))
        except Exception as exc:
            if key in old:
                fixtures.append(old[key])
            else:
                raise RuntimeError(
                    f"Could not build match context for {match['home']} - {match['away']}: {exc}"
                ) from exc
    fixtures.sort(key=lambda row: (row["date"], row.get("time") or "99:99", row["round"]))
    return fixtures


def comparable(payload: dict[str, Any]) -> dict[str, Any]:
    copy = json.loads(json.dumps(payload))
    copy.pop("updatedAt", None)
    return copy


def write_if_changed(payload: dict[str, Any], previous: dict[str, Any]) -> bool:
    if previous and comparable(previous) == comparable(payload):
        print("Livesport prediction context has not changed.")
        return False
    payload["updatedAt"] = now_iso()
    OUTPUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"Updated {OUTPUT_PATH.name}: {len(payload['fixtures'])} near-term fixtures, "
        f"{sum(v['summary']['games'] for v in payload['preseason']['teams'].values())} team-friendly rows."
    )
    return True


def validate_schedule(schedule: dict[str, Any]) -> None:
    matches = schedule.get("matches") or []
    if schedule.get("season") != SEASON or len(matches) != 240:
        raise RuntimeError("schedule-live.json must contain season 2026/27 and exactly 240 matches.")
    ids = {str(row.get("officialMatchId") or "") for row in matches}
    if len(ids) != 240 or "" in ids:
        raise RuntimeError("schedule-live.json official match IDs are incomplete or duplicated.")
    invalid_undated = [
        str(row.get("officialMatchId") or "unknown")
        for row in matches
        if not row.get("date")
        and row.get("status") not in {"postponed", "date_tba"}
    ]
    if invalid_undated:
        raise RuntimeError(
            "Undated schedule fixtures need a postponed or date_tba status: "
            + ", ".join(invalid_undated)
        )
    if set(TEAM_SEGMENTS) != {row[side] for row in matches for side in ("home", "away")}:
        raise RuntimeError("Livesport team mapping does not match the schedule's 16 clubs.")


def canonicalize_schedule_teams(schedule: dict[str, Any]) -> dict[str, Any]:
    """Translate official Chance Liga club names to the dashboard's stable names."""
    for match in schedule.get("matches") or []:
        for side in ("home", "away"):
            name = str(match.get(side) or "")
            match[side] = SCHEDULE_TEAM_ALIASES.get(name, name)
    return schedule


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--horizon-days", type=int, default=10)
    args = parser.parse_args()
    schedule = canonicalize_schedule_teams(
        json.loads(SCHEDULE_PATH.read_text(encoding="utf-8"))
    )
    validate_schedule(schedule)
    try:
        previous = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        previous = {}
    payload = {
        "schemaVersion": 1,
        "season": SEASON,
        "modelVersion": MODEL_VERSION,
        "sources": {
            "lineupsAndAbsences": "Livesport match detail",
            "preseason": "Livesport team results",
        },
        "preseason": build_preseason(schedule, previous),
        "fixtures": build_fixtures(schedule, previous, max(2, args.horizon_days)),
    }
    write_if_changed(payload, previous)


if __name__ == "__main__":
    main()
