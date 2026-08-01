#!/usr/bin/env python3
"""Import completed Chance Liga player performances from Livesport.

The Livesport team-roster table contains season totals, but it does not expose
match ratings or the detailed per-match statistics.  This module joins the
PMS match feed to the authoritative roster by the stable Livesport player ID.
Names are deliberately never used as the primary identity key.
"""

from __future__ import annotations

import copy
import json
import re
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from update_match_context import initial_feed, parse_feed_matches
from update_rosters import (
    LIVESPORT_BASE,
    LIVESPORT_TEAM_CONFIG,
    SEASON,
    USER_AGENT,
    livesport_ids,
    normalize,
)


GRAPHQL_ROOT = "https://1.ds.lsapp.eu/pq_graphql"
IMPORT_VERSION = "2026-08-01-livesport-match-stats-v3"
PRAGUE = ZoneInfo("Europe/Prague")
# Livesport occasionally corrects ratings after the first post-match import.
# Keep re-reading a rolling two-week window; the import version forces one
# complete refresh whenever the parser or validation rules change.
RECENT_REFRESH_HOURS = 24 * 14

SCHEDULE_TEAM_ALIASES = {
    "1.FC Slovácko": "Slovácko",
    "AC Sparta Praha": "Sparta Prague",
    "Bohemians Praha 1905": "Bohemians 1905",
    "FC Baník Ostrava": "Baník Ostrava",
    "FC Hradec Králové": "Hradec Králové",
    "FC Slovan Liberec": "Slovan Liberec",
    "FC Viktoria Plzeň": "Viktoria Plzeň",
    "FC Zbrojovka Brno": "Zbrojovka Brno",
    "FC Zlín": "Zlín",
    "FK Jablonec": "Jablonec",
    "FK Mladá Boleslav": "Mladá Boleslav",
    "FK Pardubice": "Pardubice",
    "FK Teplice": "Teplice",
    "SK Artis Brno": "Artis Brno",
    "SK Sigma Olomouc": "Sigma Olomouc",
    "SK Slavia Praha": "Slavia Prague",
}


def fetch_text(url: str, *, attempts: int = 4) -> str:
    error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                    "Accept-Language": "cs,en;q=0.8",
                },
            )
            with urllib.request.urlopen(request, timeout=45) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status} for {url}")
                return response.read().decode("utf-8", errors="replace")
        except Exception as exc:  # urllib raises several unrelated error types
            error = exc
            if attempt + 1 < attempts:
                time.sleep(1.25 * (attempt + 1))
    raise RuntimeError(f"Could not fetch {url}: {error}")


def graphql_payload(params: dict[str, Any], referer: str, attempts: int = 6) -> dict:
    url = f"{GRAPHQL_ROOT}?{urllib.parse.urlencode(params)}"
    error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                    "Accept-Language": "cs,en;q=0.8",
                    "Referer": referer,
                    "Origin": "https://www.livesport.cz",
                },
            )
            with urllib.request.urlopen(request, timeout=45) as response:
                if response.status == 202:
                    retry_ms = int(response.headers.get("x-retry-after-ms") or 750)
                    time.sleep(min(5, max(0.25, retry_ms / 1000)))
                    continue
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}")
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("errors"):
                raise RuntimeError(str(payload["errors"][0].get("message") or payload["errors"][0]))
            return payload
        except Exception as exc:
            error = exc
            if attempt + 1 < attempts:
                time.sleep(0.75 * (attempt + 1))
    raise RuntimeError(f"Livesport GraphQL failed for {params.get('eventId')}: {error}")


def canonical_team(value: str) -> str:
    if value in LIVESPORT_TEAM_CONFIG:
        return value
    if value in SCHEDULE_TEAM_ALIASES:
        return SCHEDULE_TEAM_ALIASES[value]
    key = normalize(value)
    exact = [team for team in LIVESPORT_TEAM_CONFIG if normalize(team) == key]
    if len(exact) == 1:
        return exact[0]
    raise RuntimeError(f"Unknown Chance Liga team: {value!r}")


def score_pair(value: Any) -> tuple[int, int] | None:
    match = re.fullmatch(r"\s*(\d+)\s*:\s*(\d+)\s*", str(value or ""))
    return (int(match.group(1)), int(match.group(2))) if match else None


def team_page_url(team: str, page: str) -> str:
    slug, participant_id = LIVESPORT_TEAM_CONFIG[team]
    return f"{LIVESPORT_BASE}/tym/{slug}/{participant_id}/{page}/"


def match_page_url(home: str, away: str, event_id: str) -> str:
    home_slug, home_id = LIVESPORT_TEAM_CONFIG[home]
    away_slug, away_id = LIVESPORT_TEAM_CONFIG[away]
    return (
        f"{LIVESPORT_BASE}/zapas/fotbal/{home_slug}-{home_id}/"
        f"{away_slug}-{away_id}/{event_id}/prehled/hracske-stats/obecne/"
    )


def discover_livesport_events(schedule: dict) -> list[dict]:
    """Resolve every scored schedule fixture to one unique Livesport event."""

    feed_rows: dict[str, dict] = {}
    for team in LIVESPORT_TEAM_CONFIG:
        url = team_page_url(team, "vysledky")
        source = fetch_text(url)
        for row in parse_feed_matches(initial_feed(source, "results")):
            if "score" not in row:
                continue
            competition = normalize(f"{row.get('competition', '')} {row.get('competitionPath', '')}")
            if "chance liga" not in competition and "/fotbal/cesko/chance-liga/" not in str(
                row.get("competitionPath") or ""
            ):
                continue
            feed_rows[str(row["livesportMatchId"])] = row

    by_pair: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in feed_rows.values():
        by_pair[(str(row["homeParticipantId"]), str(row["awayParticipantId"]))].append(row)

    output = []
    unresolved = []
    for fixture in schedule.get("matches") or []:
        score = score_pair(fixture.get("score"))
        if score is None:
            continue
        home = canonical_team(str(fixture.get("home") or ""))
        away = canonical_team(str(fixture.get("away") or ""))
        home_id = LIVESPORT_TEAM_CONFIG[home][1]
        away_id = LIVESPORT_TEAM_CONFIG[away][1]
        candidates = by_pair.get((home_id, away_id), [])
        exact = [
            row
            for row in candidates
            if (row.get("homeGoals"), row.get("awayGoals")) == score
            and datetime.fromtimestamp(int(row["timestamp"]), PRAGUE).date().isoformat()
            == str(fixture.get("date"))
        ]
        if len(exact) != 1:
            unresolved.append(
                {
                    "officialMatchId": fixture.get("officialMatchId"),
                    "home": home,
                    "away": away,
                    "date": fixture.get("date"),
                    "score": fixture.get("score"),
                    "candidateEventIds": [row.get("livesportMatchId") for row in candidates],
                }
            )
            continue
        row = exact[0]
        output.append(
            {
                "season": schedule.get("season") or SEASON,
                "round": int(fixture["round"]),
                "officialMatchId": str(fixture.get("officialMatchId") or ""),
                "date": str(fixture["date"]),
                "kickoff": fixture.get("time"),
                "home": home,
                "away": away,
                "homeGoals": score[0],
                "awayGoals": score[1],
                "score": f"{score[0]}:{score[1]}",
                "timestamp": int(row["timestamp"]),
                "livesportMatchId": str(row["livesportMatchId"]),
                "sourceUrl": match_page_url(home, away, str(row["livesportMatchId"])),
            }
        )

    if unresolved:
        raise RuntimeError(f"Could not uniquely resolve scored fixtures on Livesport: {unresolved}")
    event_ids = [row["livesportMatchId"] for row in output]
    if len(event_ids) != len(set(event_ids)):
        raise RuntimeError(f"Duplicate resolved Livesport match IDs: {event_ids}")
    return sorted(output, key=lambda row: (row["date"], row.get("kickoff") or "", row["livesportMatchId"]))


def number(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(str(value).replace(",", ".").rstrip("%"))
    except (TypeError, ValueError):
        return 0.0


def rounded(value: Any, digits: int = 2) -> float:
    return round(number(value), digits)


def ratio(won: Any, total: Any) -> str | int:
    won_number = number(won)
    total_number = number(total)
    if not total_number:
        return 0
    won_display = int(won_number) if won_number.is_integer() else won_number
    total_display = int(total_number) if total_number.is_integer() else total_number
    return f"{won_display}/{total_display} ({round(won_number / total_number * 100)}%)"


def entry_lookup(entries: list[dict]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = defaultdict(dict)
    for entry in entries:
        output[str(entry.get("playerId") or "")][str(entry.get("typeId") or "")] = entry.get(
            "rawValue", entry.get("value")
        )
    return output


def position_code(player: dict) -> str:
    position = player.get("position") or {}
    if position.get("isGoalkeeper"):
        return "GK"
    key = normalize(str(position.get("name") or ""))
    if "obrance" in key or "wingback" in key:
        return "D"
    if "utocnik" in key or "kridlo" in key or "forward" in key:
        return "A"
    return "M"


def build_rich_stats(values: dict[str, Any], is_goalkeeper: bool) -> dict:
    value = lambda key: number(values.get(key))
    stats = {
        "shotsTotal": value("SHOTS_TOTAL"),
        "shotsOnTarget": value("SHOTS_ON_TARGET"),
        "xG": rounded(value("EXPECTED_GOALS")),
        "xGOT": rounded(value("EXPECTED_GOALS_ON_TARGET")),
        "touches": value("TOUCHES_TOTAL"),
        "touchesInBox": value("TOUCHES_BOX_OPPOSITE"),
        "successfulDribbles": ratio(value("DRIBBLES_WON"), value("DRIBBLES_TOTAL")),
        "bigChancesMissed": value("BIG_CHANCES_MISSED"),
        "foulsWon": value("FOULS_SUFFERED"),
        "offsides": value("OFFSIDES"),
        "accuratePasses": ratio(value("PASSES_ACCURATE"), value("PASSES_TOTAL")),
        "bigChancesCreated": value("BIG_CHANCES_CREATED"),
        "keyPasses": value("KEY_PASSES"),
        "xA": rounded(value("EXPECTED_ASSISTS")),
        "duelsWon": ratio(value("DUELS_WON"), value("DUELS_TOTAL")),
        "aerialDuelsWon": ratio(value("DUELS_AERIAL_WON"), value("DUELS_AERIAL_TOTAL")),
        "groundDuelsWon": ratio(value("DUELS_GROUND_WON"), value("DUELS_GROUND_TOTAL")),
        "defensiveActions": ratio(value("TACKLES_WON"), value("TACKLES_TOTAL")),
        "foulsCommitted": value("FOULS_COMMITTED"),
        "interceptions": value("INTERCEPTIONS"),
        "clearances": value("CLEARANCES"),
        "errorsToGoal": value("ERRORS_LEAD_TO_GOAL"),
        "errorsToShot": value("ERRORS_LEAD_TO_SHOT"),
        "goalkeeper": None,
    }
    if is_goalkeeper:
        stats["goalkeeper"] = {
            "saves": value("SAVES_TOTAL"),
            "goalsConceded": value("GOALS_CONCEDED"),
            "goalsPrevented": rounded(value("GOALS_PREVENTED")),
            "xGOTAgainst": rounded(value("EXPECTED_GOALS_ON_TARGET_FACED")),
            "punches": value("PUNCHES_TOTAL"),
            "throws": value("KEEPER_THROWS_TOTAL"),
            "sweeperActions": value("KEEPER_SWEEPER_TOTAL"),
        }
    return stats


def int_if_whole(value: Any) -> int | float:
    numeric = number(value)
    return int(numeric) if numeric.is_integer() else numeric


def load_match_performances(fixture: dict) -> list[dict]:
    event_id = fixture["livesportMatchId"]
    referer = fixture["sourceUrl"]
    settings_payload = graphql_payload(
        {"_hash": "epmsse", "eventId": event_id, "projectId": "1"}, referer
    )
    settings = (settings_payload.get("data") or {}).get("findEventPMSById")
    if not isinstance(settings, dict):
        raise RuntimeError(f"{event_id}: player-stat settings are unavailable")
    provider_id = (settings.get("updateFeedProviderId") or {}).get("id")
    if provider_id is None:
        raise RuntimeError(f"{event_id}: player-stat provider is unavailable")

    stats_payload = graphql_payload(
        {"_hash": "epmsd", "eventId": event_id, "providerId": str(provider_id)}, referer
    )
    performance = (stats_payload.get("data") or {}).get("findEventPMSById")
    if not isinstance(performance, dict):
        raise RuntimeError(f"{event_id}: player performances are unavailable")

    entries = entry_lookup((performance.get("stats") or {}).get("entries") or [])
    ratings = {
        str(row.get("participantId") or ""): (
            None if row.get("value") in (None, "") else number(row.get("value"))
        )
        for row in performance.get("ratings") or []
    }
    team_by_participant = {
        participant_id: team
        for team, (_, participant_id) in LIVESPORT_TEAM_CONFIG.items()
    }
    rows = []
    for player in settings.get("players") or []:
        participant = player.get("participant") or {}
        player_id = str(participant.get("id") or "")
        values = entries.get(player_id) or {}
        minutes = number(values.get("MATCH_MINUTES_PLAYED"))
        if minutes <= 0:
            continue
        team = team_by_participant.get(str(player.get("teamId") or ""))
        if team not in {fixture["home"], fixture["away"]}:
            raise RuntimeError(
                f"{event_id} {player_id}: unexpected teamId {player.get('teamId')!r}"
            )
        is_home = team == fixture["home"]
        opponent = fixture["away"] if is_home else fixture["home"]
        is_goalkeeper = bool((player.get("position") or {}).get("isGoalkeeper"))
        rich_stats = build_rich_stats(values, is_goalkeeper)
        straight_red = number(values.get("CARDS_RED"))
        second_yellow = max(
            number(values.get("CARDS_YELLOW_SECOND")),
            number(values.get("CARDS_YELLOW_RED")),
        )
        red_cards = straight_red + second_yellow
        saves = rich_stats["goalkeeper"]["saves"] if is_goalkeeper else None
        goals_conceded = (
            rich_stats["goalkeeper"]["goalsConceded"] if is_goalkeeper else 0
        )
        home_won = fixture["homeGoals"] > fixture["awayGoals"]
        result = (
            "D"
            if fixture["homeGoals"] == fixture["awayGoals"]
            else "W"
            if (is_home and home_won) or (not is_home and not home_won)
            else "L"
        )
        match = {
            "season": fixture["season"],
            "round": fixture["round"],
            "competition": "Chance Liga",
            "date": fixture["date"],
            "kickoff": fixture.get("kickoff"),
            "vs": opponent,
            "opponent": opponent,
            "homeAway": "H" if is_home else "A",
            "result": result,
            "score": fixture["score"],
            "starter": bool(player.get("inBaseLineup")),
            "shirtNumber": player.get("shirtNumber") or player.get("jerseyNumber"),
            "livesportPosition": (player.get("position") or {}).get("name") or "",
            "mins": int_if_whole(minutes),
            "form": ratings.get(player_id),
            "ratingSource": "Livesport player ratings",
            "goals": int_if_whole(values.get("GOALS")),
            "ownGoals": int_if_whole(values.get("GOALS_OWN")),
            "assists": int_if_whole(values.get("ASSISTS_GOAL")),
            "yellowCards": int_if_whole(values.get("CARDS_YELLOW")),
            "redCards": int_if_whole(red_cards),
            "saves": int_if_whole(saves) if saves is not None else None,
            "shotsOn": int_if_whole(number(saves) + number(goals_conceded))
            if is_goalkeeper
            else None,
            "stats": rich_stats,
            "source": "Livesport",
            "sourceUrl": fixture["sourceUrl"],
            "livesportMatchId": event_id,
            "livesportPlayerId": player_id,
            "importVersion": IMPORT_VERSION,
            "team": team,
        }
        rows.append(
            {
                "livesportPlayerId": player_id,
                "sourceName": participant.get("name")
                or participant.get("shortDisplayName")
                or "",
                "sourcePosition": position_code(player),
                "match": match,
            }
        )

    if not 22 <= len(rows) <= 40:
        raise RuntimeError(f"{event_id}: implausible player-performance count {len(rows)}")
    if len({row["livesportPlayerId"] for row in rows}) != len(rows):
        raise RuntimeError(f"{event_id}: duplicate player IDs in performance feed")
    return rows


def existing_event_rows(players: list[dict], event_id: str) -> list[tuple[dict, dict]]:
    return [
        (player, match)
        for player in players
        for match in player.get("matches") or []
        if str(match.get("livesportMatchId") or "") == event_id
    ]


def should_refresh_fixture(players: list[dict], fixture: dict, refresh_all: bool) -> bool:
    if refresh_all:
        return True
    rows = existing_event_rows(players, fixture["livesportMatchId"])
    complete = (
        len(rows) >= 22
        and all(match.get("importVersion") == IMPORT_VERSION for _, match in rows)
        and len({str(match.get("livesportPlayerId") or "") for _, match in rows}) == len(rows)
    )
    cutoff = datetime.now(timezone.utc) - timedelta(hours=RECENT_REFRESH_HOURS)
    event_time = datetime.fromtimestamp(fixture["timestamp"], timezone.utc)
    return not complete or event_time >= cutoff


def update_current_rating(player: dict) -> None:
    current_matches = [
        match
        for match in player.get("matches") or []
        if (match.get("season") or SEASON) == SEASON
        and normalize(str(match.get("competition") or "")) == "chance liga"
        and number(match.get("mins")) > 0
    ]
    ratings = [
        number(match.get("form"))
        for match in current_matches
        if match.get("form") not in (None, "") and number(match.get("form")) > 0
    ]
    rating = round(sum(ratings) / len(ratings), 2) if ratings else None
    stats = player.get("livesportSeasonStats")
    if isinstance(stats, dict) and stats.get("season") == SEASON:
        if current_matches:
            stats.update(
                {
                    "apps": len(current_matches),
                    "minutes": round(sum(number(match.get("mins")) for match in current_matches)),
                    "goals": round(sum(number(match.get("goals")) for match in current_matches)),
                    "assists": round(sum(number(match.get("assists")) for match in current_matches)),
                    "yellowCards": round(
                        sum(number(match.get("yellowCards")) for match in current_matches)
                    ),
                    "redCards": round(
                        sum(number(match.get("redCards")) for match in current_matches)
                    ),
                    "source": "Livesport match player statistics",
                    "sourceUrl": "https://www.livesport.cz/fotbal/cesko/chance-liga/",
                }
            )
        stats["rating"] = rating
        stats["ratedApps"] = len(ratings)

    current_careers = [
        row
        for row in player.get("career") or []
        if row.get("season") in {SEASON, "2026/2027"}
        and normalize(str(row.get("competition") or "")) == "chance liga"
    ]
    if current_careers:
        preferred = next(
            (row for row in current_careers if normalize(str(row.get("team") or "")) == normalize(player["team"])),
            current_careers[0],
        )
        preferred["rating"] = rating
        preferred["ratingSource"] = "Livesport match player ratings"
        if isinstance(stats, dict) and stats.get("season") == SEASON and current_matches:
            preferred.update(
                {
                    "matches": stats["apps"],
                    "minutes": stats["minutes"],
                    "goals": stats["goals"],
                    "assists": stats["assists"],
                    "yellowCards": stats["yellowCards"],
                    "redCards": stats["redCards"],
                }
            )


def apply_match_performances(
    players: list[dict], fixtures: list[dict], refresh_all: bool = False
) -> tuple[list[dict], dict]:
    updated = copy.deepcopy(players)
    by_livesport_id: dict[str, dict] = {}
    for player in updated:
        for player_id in livesport_ids(player):
            if player_id in by_livesport_id:
                raise RuntimeError(f"Duplicate roster Livesport player ID {player_id}")
            by_livesport_id[player_id] = player

    processed = []
    skipped = []
    for fixture in fixtures:
        event_id = fixture["livesportMatchId"]
        if not should_refresh_fixture(updated, fixture, refresh_all):
            skipped.append(event_id)
            continue
        rows = load_match_performances(fixture)
        unresolved = [
            {
                "livesportPlayerId": row["livesportPlayerId"],
                "sourceName": row["sourceName"],
                "sourceTeam": row["match"]["team"],
            }
            for row in rows
            if row["livesportPlayerId"] not in by_livesport_id
        ]
        if unresolved:
            raise RuntimeError(f"{event_id}: performance players missing from roster: {unresolved}")

        for player in updated:
            player["matches"] = [
                match
                for match in player.get("matches") or []
                if str(match.get("livesportMatchId") or "") != event_id
            ]
        for row in rows:
            player = by_livesport_id[row["livesportPlayerId"]]
            opponent = row["match"]["opponent"]
            player["matches"] = [
                match
                for match in player.get("matches") or []
                if not (
                    (match.get("season") or SEASON) == fixture["season"]
                    and int(match.get("round") or 0) == fixture["round"]
                    and normalize(str(match.get("opponent") or match.get("vs") or ""))
                    == normalize(opponent)
                )
            ]
            player["matches"].append(row["match"])
            player["matches"].sort(
                key=lambda match: (
                    str(match.get("date") or ""),
                    str(match.get("kickoff") or ""),
                    str(match.get("livesportMatchId") or ""),
                )
            )
        processed.append(
            {
                "livesportMatchId": event_id,
                "round": fixture["round"],
                "home": fixture["home"],
                "away": fixture["away"],
                "score": fixture["score"],
                "playerPerformances": len(rows),
                "ratedPerformances": sum(row["match"]["form"] is not None for row in rows),
                "sourceUrl": fixture["sourceUrl"],
            }
        )

    for player in updated:
        update_current_rating(player)
    validation = validate_match_details(updated, fixtures)
    return updated, {
        "source": "Livesport match player statistics",
        "sourceUrl": "https://www.livesport.cz/fotbal/cesko/chance-liga/",
        "importVersion": IMPORT_VERSION,
        "completedMatches": len(fixtures),
        "playerPerformances": validation["playerPerformances"],
        "ratedPerformances": validation["ratedPerformances"],
        "processed": processed,
        "skippedStableEventIds": skipped,
        "validation": validation,
    }


def validate_match_details(players: list[dict], fixtures: list[dict]) -> dict:
    expected = {fixture["livesportMatchId"]: fixture for fixture in fixtures}
    event_counts: dict[str, int] = defaultdict(int)
    event_ratings: dict[str, int] = defaultdict(int)
    event_teams: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(int))
    )
    identity_keys: set[tuple[str, str]] = set()
    errors = []
    reconciliation_issues = []

    for player in players:
        player_ids = livesport_ids(player)
        detailed = []
        for match in player.get("matches") or []:
            event_id = str(match.get("livesportMatchId") or "")
            if event_id not in expected:
                continue
            match_player_id = str(match.get("livesportPlayerId") or "")
            key = (event_id, match_player_id)
            if not match_player_id:
                errors.append(f"{event_id} {player['name']}: missing player ID")
            elif match_player_id not in player_ids:
                errors.append(
                    f"{event_id} {player['name']}: match player ID {match_player_id} does not match roster identity"
                )
            if key in identity_keys:
                errors.append(f"duplicate match/player identity {event_id}/{match_player_id}")
            identity_keys.add(key)
            minutes = number(match.get("mins"))
            rating = match.get("form")
            if not 0 < minutes <= 130:
                errors.append(f"{event_id} {player['name']}: invalid minutes {minutes}")
            if rating not in (None, "") and not 1 <= number(rating) <= 10:
                errors.append(f"{event_id} {player['name']}: invalid rating {rating}")
            if rating in (None, "") and minutes > 10:
                errors.append(
                    f"{event_id} {player['name']}: missing rating after {minutes:g} minutes"
                )
            for field, maximum in (
                ("goals", 10),
                ("ownGoals", 5),
                ("assists", 10),
                ("yellowCards", 2),
                ("redCards", 2),
            ):
                value = number(match.get(field))
                if not 0 <= value <= maximum:
                    errors.append(f"{event_id} {player['name']}: invalid {field} {value}")
            if player.get("pos") == "GK" and match.get("saves") is not None:
                saves = number(match.get("saves"))
                shots_on = number(match.get("shotsOn"))
                if saves < 0 or shots_on < saves:
                    errors.append(
                        f"{event_id} {player['name']}: invalid saves/shotsOn {saves}/{shots_on}"
                    )
            event_counts[event_id] += 1
            event_ratings[event_id] += rating not in (None, "")
            team = str(match.get("team") or "")
            team_totals = event_teams[event_id][team]
            team_totals["rows"] += 1
            team_totals["starts"] += bool(match.get("starter"))
            team_totals["goals"] += round(number(match.get("goals")))
            team_totals["ownGoals"] += round(number(match.get("ownGoals")))
            team_totals["assists"] += round(number(match.get("assists")))
            team_totals["goalkeepers"] += (match.get("stats") or {}).get("goalkeeper") is not None
            detailed.append(match)

        season_stats = player.get("livesportSeasonStats") or {}
        if season_stats.get("season") == SEASON and int(season_stats.get("apps") or 0) > 0:
            expected_values = {
                "apps": len(detailed),
                "minutes": round(sum(number(match.get("mins")) for match in detailed)),
                "goals": round(sum(number(match.get("goals")) for match in detailed)),
                "assists": round(sum(number(match.get("assists")) for match in detailed)),
                "yellowCards": round(
                    sum(number(match.get("yellowCards")) for match in detailed)
                ),
                "redCards": round(sum(number(match.get("redCards")) for match in detailed)),
            }
            differences = {
                field: {"published": int(season_stats.get(field) or 0), "detailed": value}
                for field, value in expected_values.items()
                if int(season_stats.get(field) or 0) != value
            }
            if differences:
                reconciliation_issues.append(
                    {
                        "team": player["team"],
                        "name": player["name"],
                        "livesportPlayerId": next(iter(player_ids), None),
                        "differences": differences,
                    }
                )

    for event_id in expected:
        count = event_counts[event_id]
        if not 22 <= count <= 40:
            errors.append(f"{event_id}: incomplete detailed match rows ({count})")
            continue
        fixture = expected[event_id]
        team_rows = event_teams[event_id]
        expected_teams = {fixture["home"], fixture["away"]}
        if set(team_rows) != expected_teams:
            errors.append(
                f"{event_id}: unexpected performance teams {sorted(team_rows)}; expected {sorted(expected_teams)}"
            )
            continue
        for team in expected_teams:
            totals = team_rows[team]
            if not 11 <= totals["rows"] <= 20:
                errors.append(f"{event_id} {team}: invalid player count {totals['rows']}")
            if totals["starts"] != 11:
                errors.append(f"{event_id} {team}: invalid starter count {totals['starts']}")
            if totals["goalkeepers"] not in {1, 2}:
                errors.append(
                    f"{event_id} {team}: invalid goalkeeper appearance count {totals['goalkeepers']}"
                )
        home_totals = team_rows[fixture["home"]]
        away_totals = team_rows[fixture["away"]]
        derived_home_goals = home_totals["goals"] + away_totals["ownGoals"]
        derived_away_goals = away_totals["goals"] + home_totals["ownGoals"]
        if (derived_home_goals, derived_away_goals) != (
            fixture["homeGoals"],
            fixture["awayGoals"],
        ):
            errors.append(
                f"{event_id}: scorer totals {derived_home_goals}:{derived_away_goals} "
                f"do not match result {fixture['score']}"
            )
        if home_totals["assists"] > fixture["homeGoals"]:
            errors.append(f"{event_id} {fixture['home']}: more assists than goals")
        if away_totals["assists"] > fixture["awayGoals"]:
            errors.append(f"{event_id} {fixture['away']}: more assists than goals")
    if errors:
        raise RuntimeError("Livesport match-stat validation failed: " + "; ".join(errors))
    return {
        "status": "ok" if not reconciliation_issues else "needs-review",
        "eventCount": len(expected),
        "eventPlayerCounts": dict(sorted(event_counts.items())),
        "eventRatedCounts": dict(sorted(event_ratings.items())),
        "playerPerformances": sum(event_counts.values()),
        "ratedPerformances": sum(event_ratings.values()),
        "reconciliationIssues": reconciliation_issues,
    }
