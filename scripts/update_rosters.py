#!/usr/bin/env python3
"""Build the live Chance Liga roster and market-value data layer.

Membership is reconciled from the official Chance Liga club pages and the
current Transfermarkt squad pages.  Existing match and career data is preserved
from the previous live file (or an explicitly supplied seed).  The script
validates the complete result before atomically replacing rosters-live.json.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from lxml import html


SEASON = "2026/27"
SEASON_ID = 2026
CHANCE_BASE = "https://www.chanceliga.cz"
TM_BASE = "https://www.transfermarkt.com"
TM_API = "https://tmapi.transfermarkt.technology"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/138 Safari/537.36"
)
TEAM_CONFIG = [
    ("Slovácko", "/klub/16-1-fc-slovacko", 5544, "1-fc-slovacko"),
    ("Sparta Prague", "/klub/2-ac-sparta-praha", 197, "ac-sparta-prag"),
    ("Bohemians 1905", "/klub/20-bohemians-praha-1905", 715, "fc-bohemians-prag-1905"),
    ("Baník Ostrava", "/klub/14-fc-banik-ostrava", 377, "fc-banik-ostrau"),
    ("Hradec Králové", "/klub/11-fc-hradec-kralove", 1897, "fc-hradec-kralove"),
    ("Slovan Liberec", "/klub/7-fc-slovan-liberec", 697, "fc-slovan-liberec"),
    ("Viktoria Plzeň", "/klub/6-fc-viktoria-plzen", 941, "fc-viktoria-pilsen"),
    ("Zbrojovka Brno", "/klub/9-fc-zbrojovka-brno", 5225, "fc-zbrojovka-brunn"),
    ("Zlín", "/klub/33-fc-zlin", 5545, "fc-fastav-zlin"),
    ("Jablonec", "/klub/4-fk-jablonec", 1322, "fk-jablonec"),
    ("Mladá Boleslav", "/klub/8-fk-mlada-boleslav", 5546, "fk-mlada-boleslav"),
    ("Pardubice", "/klub/39-fk-pardubice", 1496, "fk-pardubice"),
    ("Teplice", "/klub/17-fk-teplice", 814, "fk-teplice"),
    ("Artis Brno", "/klub/41-sk-artis-brno", 24325, "sk-lisen"),
    ("Sigma Olomouc", "/klub/13-sk-sigma-olomouc", 2311, "sk-sigma-olmutz"),
    ("Slavia Prague", "/klub/5-sk-slavia-praha", 62, "sk-slavia-prag"),
]
TEAM_ORDER = [row[0] for row in TEAM_CONFIG]
OFFICIAL_POSITION = {"B": "GK", "O": "D", "Z": "M", "U": "A"}

# The official site temporarily lists these player IDs under two clubs.  The
# selected teams were verified against current club announcements/current
# squads on 2026-07-31.
OFFICIAL_DUPLICATE_TEAM = {
    "3142": "Slovácko",        # David Štěpánek
    "4674": "Mladá Boleslav",  # Filip Špatenka
    "4444": "Artis Brno",      # Alexis Alégué
}
# A different, older player with the same name is incorrectly present on the
# Artis page; his current Transfermarkt club is outside Chance Liga.
IGNORED_OFFICIAL_PLAYER_IDS = {"2693"}
# Name variants that cannot safely be paired by general fuzzy matching.
OFFICIAL_TO_TM_ID = {
    "4084": "261010",   # Vlasij Sinjavskij / Vlasiy Sinyavskiy
    "5181": "1109889",  # Kauan Carneiro Da Silva Kaká / Kaká
    "2970": "303440",   # Ladislav Takács / Laco Takacs
    "5149": "558467",   # Michal Jeřábek (born 1995)
    "3558": "401475",   # Murphy Dorley Oscar / Oscar
    "4455": "723415",   # Hélio ... Papalele / Papalelé
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def clean(value: str) -> str:
    return " ".join((value or "").split())


def normalize(value: str) -> str:
    text = unicodedata.normalize("NFD", value or "")
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = (
        text.replace("ø", "o")
        .replace("Ø", "o")
        .replace("ł", "l")
        .replace("Ł", "l")
        .replace("đ", "d")
        .replace("Đ", "d")
        .replace("ð", "d")
        .replace("Ð", "d")
        .replace("æ", "ae")
        .replace("Æ", "ae")
        .replace("œ", "oe")
        .replace("Œ", "oe")
        .replace("ß", "ss")
        .lower()
    )
    return " ".join(re.findall(r"[a-z0-9]+", text))


def token_set(value: str) -> set[str]:
    return set(normalize(value).split())


def identity_key(value: str) -> str:
    return "|".join(sorted(token_set(value)))


def name_score(left: str, right: str) -> float:
    left_tokens = token_set(left)
    right_tokens = token_set(right)
    if not left_tokens or not right_tokens:
        return 0.0
    if left_tokens == right_tokens:
        return 1.0
    smaller = left_tokens if len(left_tokens) <= len(right_tokens) else right_tokens
    larger = right_tokens if smaller is left_tokens else left_tokens
    if len(smaller) >= 2 and smaller <= larger:
        return 0.96
    return max(
        SequenceMatcher(None, normalize(left), normalize(right)).ratio(),
        SequenceMatcher(
            None, " ".join(sorted(left_tokens)), " ".join(sorted(right_tokens))
        ).ratio(),
    )


def best_match(name: str, candidates: list[dict], threshold: float = 0.82):
    ranked = sorted(
        ((name_score(name, row["name"]), row) for row in candidates),
        key=lambda item: item[0],
        reverse=True,
    )
    if not ranked or ranked[0][0] < threshold:
        return None
    if len(ranked) > 1 and ranked[1][0] >= ranked[0][0] - 0.015:
        return None
    return ranked[0]


def app_name(source_name: str) -> str:
    parts = clean(source_name).split()
    if len(parts) <= 1:
        return clean(source_name)
    return " ".join([parts[-1], *parts[:-1]])


def fetch_bytes(
    url: str,
    *,
    accept: str = "text/html",
    referer: str | None = None,
    extra_headers: dict[str, str] | None = None,
    attempts: int = 4,
) -> bytes:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
    if extra_headers:
        headers.update(extra_headers)
    transient_statuses = {202, 408, 425, 429, 500, 502, 503, 504}
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = response.read()
                if response.status == 200:
                    return payload
                last_error = RuntimeError(f"{url}: HTTP {response.status}")
                if response.status not in transient_statuses:
                    raise last_error
        except urllib.error.HTTPError as exc:
            last_error = RuntimeError(f"{url}: HTTP {exc.code}")
            if exc.code not in transient_statuses:
                raise last_error from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            last_error = exc
        if attempt + 1 < attempts:
            time.sleep(2 ** attempt)
    raise RuntimeError(f"{url}: failed after {attempts} attempts ({last_error})")


def fetch_json(url: str) -> dict:
    return json.loads(fetch_bytes(url, accept="application/json").decode("utf-8"))


def parse_int(value: str) -> int | None:
    match = re.search(r"\d+", value or "")
    return int(match.group(0)) if match else None


def parse_value(value: str) -> int | None:
    text = clean(value).replace("€", "").replace(",", ".").lower()
    match = re.fullmatch(r"([\d.]+)([mk])?", text)
    if not match:
        return None
    number = float(match.group(1))
    multiplier = 1_000_000 if match.group(2) == "m" else 1_000 if match.group(2) == "k" else 1
    return round(number * multiplier)


def format_value(value: int | None) -> str | None:
    if not value:
        return None
    if value >= 1_000_000:
        return f"€{value / 1_000_000:g}m"
    return f"€{round(value / 1000)}k"


def tm_position(value: str) -> str:
    position = clean(value)
    if position == "Goalkeeper":
        return "GK"
    if position in {"Defender", "Centre-Back", "Left-Back", "Right-Back"}:
        return "D"
    if position in {
        "Midfield",
        "Midfielder",
        "Defensive Midfield",
        "Central Midfield",
        "Attacking Midfield",
        "Left Midfield",
        "Right Midfield",
    }:
        return "M"
    if position in {
        "Attack",
        "Attacker",
        "Forward",
        "Striker",
        "Centre-Forward",
        "Second Striker",
        "Left Winger",
        "Right Winger",
    }:
        return "A"
    raise RuntimeError(f"Unknown Transfermarkt position {position!r}")


def scrape_official(team: str, path: str) -> dict:
    url = CHANCE_BASE + path
    document = html.fromstring(fetch_bytes(url))
    players = []
    for row in document.xpath("//tr[.//a[contains(@href,'/hrac/')]]"):
        cells = [clean(cell.text_content()) for cell in row.xpath("./th|./td")]
        links = row.xpath(".//a[contains(@href,'/hrac/')]/@href")
        if len(cells) < 7 or len(links) != 1:
            continue
        match = re.search(r"/hrac/(\d+)-", links[0])
        position = OFFICIAL_POSITION.get(cells[2])
        if not match or not position:
            continue
        players.append(
            {
                "name": cells[1],
                "team": team,
                "position": position,
                "chanceLigaPlayerId": match.group(1),
                "chanceLigaUrl": CHANCE_BASE + links[0],
                "shirtNumber": parse_int(cells[0]),
                "dateOfBirth": None if cells[4] == "-" else cells[4],
                "heightCm": parse_int(cells[5]),
                "weightKg": parse_int(cells[6]),
            }
        )
    if not 18 <= len(players) <= 60:
        raise RuntimeError(f"{team}: official roster has implausible size {len(players)}")
    return {"sourceUrl": url, "players": players}


def transfermarkt_player(
    *,
    team: str,
    name: str,
    player_id: str,
    profile_url: str,
    position_detail: str,
    value_text: str,
    squad_url: str,
) -> dict:
    value_eur = parse_value(value_text)
    return {
        "name": clean(name),
        "team": team,
        "position": tm_position(position_detail),
        "positionDetail": position_detail,
        "transfermarktPlayerId": player_id,
        "transfermarktUrl": profile_url,
        "mv": format_value(value_eur),
        "marketValueEur": value_eur,
        "transfermarktSquadUrl": squad_url,
    }


def parse_transfermarkt_html(team: str, url: str, payload: bytes) -> list[dict]:
    document = html.fromstring(payload)
    xpath = (
        "//tr["
        "contains(concat(' ',normalize-space(@class),' '),' odd ') or "
        "contains(concat(' ',normalize-space(@class),' '),' even ')"
        "][.//a[contains(@href,'/profil/spieler/')]]"
    )
    players = []
    seen = set()
    for row in document.xpath(xpath):
        links = row.xpath(".//td[contains(@class,'hauptlink')]/a[contains(@href,'/profil/spieler/')]")
        if not links:
            continue
        link = links[0]
        profile_path = link.get("href") or ""
        match = re.search(r"/profil/spieler/(\d+)", profile_path)
        if not match or match.group(1) in seen:
            continue
        player_id = match.group(1)
        seen.add(player_id)
        position_detail = clean(
            "".join(
                row.xpath(
                    "./td[contains(@class,'posrela')]"
                    "//table[contains(@class,'inline-table')]/tr[2]/td/text()"
                )
            )
        )
        value_text = clean(
            "".join(row.xpath("./td[contains(@class,'rechts') and contains(@class,'hauptlink')]//text()"))
        )
        players.append(
            transfermarkt_player(
                team=team,
                name=link.text_content(),
                player_id=player_id,
                profile_url=TM_BASE + profile_path,
                position_detail=position_detail,
                value_text=value_text,
                squad_url=url,
            )
        )
    return players


def tm_api_player(team: str, squad_url: str, row: dict) -> dict:
    attributes = row.get("attributes") or {}
    position = attributes.get("position") or {}
    position_detail = clean(
        position.get("name")
        or attributes.get("positionGroupName")
        or position.get("category")
    )
    current_value = ((row.get("marketValueDetails") or {}).get("current") or {}).get("value")
    value_eur = int(current_value) if isinstance(current_value, (int, float)) else None
    profile_path = row.get("relativeUrl") or ""
    return {
        "name": clean(row.get("name") or row.get("displayName") or row.get("shortName")),
        "team": team,
        "position": tm_position(position_detail),
        "positionDetail": position_detail,
        "transfermarktPlayerId": str(row["id"]),
        "transfermarktUrl": (
            TM_BASE + profile_path
            if profile_path.startswith("/")
            else f"{TM_BASE}/profil/spieler/{row['id']}"
        ),
        "mv": format_value(value_eur),
        "marketValueEur": value_eur,
        "transfermarktSquadUrl": squad_url,
    }


def parse_transfermarkt_api_squad(team: str, url: str, club_id: int) -> list[dict]:
    response = fetch_json(f"{TM_API}/club/{club_id}/squad")
    data = response.get("data") or {}
    squad = data.get("squad")
    if not response.get("success") or not isinstance(squad, list):
        raise RuntimeError(f"{team}: Transfermarkt squad API failed: {response}")
    ids = {
        str(assignment.get("playerId"))
        for assignment in squad
        if assignment.get("playerId")
        and assignment.get("type") in {"current", "additional"}
    }
    rows = batch_entities("players", ids)
    missing = sorted(ids - set(rows), key=int)
    if missing:
        raise RuntimeError(
            f"{team}: Transfermarkt player API did not resolve squad IDs {missing}"
        )
    return [
        tm_api_player(team, url, rows[player_id])
        for player_id in sorted(ids, key=int)
    ]


def scrape_transfermarkt(team: str, club_id: int, slug: str) -> dict:
    url = (
        f"{TM_BASE}/{slug}/kader/verein/{club_id}/"
        f"saison_id/{SEASON_ID}/plus/1"
    )
    source_method = "transfermarkt-direct"
    direct_error = None
    try:
        payload = fetch_bytes(
            url,
            referer=(
                f"{TM_BASE}/chance-liga/startseite/wettbewerb/TS1/"
                f"plus/?saison_id={SEASON_ID}"
            ),
            attempts=2,
        )
        players = parse_transfermarkt_html(team, url, payload)
    except RuntimeError as exc:
        direct_error = str(exc)
        players = []
    if not 18 <= len(players) <= 60:
        source_method = "transfermarkt-api-fallback"
        players = parse_transfermarkt_api_squad(team, url, club_id)
    if not 18 <= len(players) <= 60:
        detail = f"; direct fetch: {direct_error}" if direct_error else ""
        raise RuntimeError(
            f"{team}: Transfermarkt roster has implausible size {len(players)}{detail}"
        )
    return {
        "sourceUrl": url,
        "sourceMethod": source_method,
        "players": players,
    }


def search_transfermarkt(name: str) -> dict | None:
    url = (
        f"{TM_BASE}/schnellsuche/ergebnis/schnellsuche?query="
        + urllib.parse.quote(clean(name))
    )
    try:
        document = html.fromstring(
            fetch_bytes(url, referer=TM_BASE + "/", attempts=1)
        )
    except RuntimeError:
        # Official registrations absent from the current Transfermarkt squad
        # keep their existing stable identity/value. A genuinely new unresolved
        # player is emitted as a validation warning instead of trusting an
        # unverified third-party search proxy.
        return None
    candidates = []
    seen = set()
    for row in document.xpath("//tr[.//a[contains(@href,'/profil/spieler/')]]"):
        links = row.xpath(".//a[contains(@href,'/profil/spieler/')]")
        if not links:
            continue
        link = links[0]
        profile_path = link.get("href") or ""
        match = re.search(r"/profil/spieler/(\d+)", profile_path)
        if not match or match.group(1) in seen:
            continue
        seen.add(match.group(1))
        value_text = clean(
            "".join(row.xpath("./td[contains(@class,'rechts') and contains(@class,'hauptlink')]//text()"))
        )
        club_title = clean(
            "".join(
                row.xpath(
                    ".//a[contains(@href,'/startseite/verein/')]/@title"
                )[:1]
            )
        )
        value_eur = parse_value(value_text)
        candidates.append(
            {
                "name": clean(link.text_content()),
                "transfermarktPlayerId": match.group(1),
                "transfermarktUrl": TM_BASE + profile_path,
                "mv": format_value(value_eur),
                "marketValueEur": value_eur,
                "transfermarktSearchUrl": url,
                "transfermarktReportedClub": club_title or None,
            }
        )
    result = best_match(name, candidates, threshold=0.88)
    return result[1] if result else None


def load_seed(path: Path | None, output: Path) -> list[dict]:
    source = path if path and path.exists() else output if output.exists() else None
    if not source:
        return []
    payload = json.loads(source.read_text(encoding="utf-8"))
    players = payload.get("players")
    if not isinstance(players, list):
        raise RuntimeError(f"{source}: missing players array")
    return players


def choose_donor(
    target: dict,
    donors: list[dict],
    used: set[int],
    *,
    allow_cross_team: bool = False,
) -> dict | None:
    player_id = str(target.get("transfermarktPlayerId") or "")
    if player_id:
        for donor in donors:
            if id(donor) not in used and str(donor.get("transfermarktPlayerId") or "") == player_id:
                return donor
    chance_id = str(target.get("chanceLigaPlayerId") or "")
    if chance_id:
        for donor in donors:
            if id(donor) not in used and str(donor.get("chanceLigaPlayerId") or "") == chance_id:
                return donor
    same_team = [
        donor
        for donor in donors
        if id(donor) not in used and donor.get("team") == target.get("team")
    ]
    result = best_match(target["name"], same_team)
    if result:
        return result[1]
    if allow_cross_team:
        result = best_match(
            target["name"],
            [donor for donor in donors if id(donor) not in used],
            threshold=0.92,
        )
        if result:
            return result[1]
    return None


def batch_entities(endpoint: str, ids: set[str]) -> dict[str, dict]:
    output = {}
    values = sorted(value for value in ids if value and value != "0")
    for offset in range(0, len(values), 80):
        batch = values[offset : offset + 80]
        query = urllib.parse.urlencode([("ids[]", value) for value in batch])
        response = fetch_json(f"{TM_API}/{endpoint}?{query}")
        if not response.get("success") or not isinstance(response.get("data"), list):
            raise RuntimeError(f"Transfermarkt API failed to resolve {endpoint}: {response}")
        for row in response["data"]:
            output[str(row["id"])] = row
    return output


def normalized_season(display: str, season_id: int) -> str:
    value = clean(display)
    match = re.fullmatch(r"(\d{2})/(\d{2})", value)
    if match:
        return f"20{match.group(1)}/{match.group(2)}"
    if value:
        return value
    return str(season_id)


def aggregate_career(data: dict, clubs: dict[str, dict], competitions: dict[str, dict]) -> list[dict]:
    groups = {}
    for performance in data.get("performance", []):
        game = performance.get("gameInformation") or {}
        if game.get("isNationalGame"):
            continue
        statistics = performance.get("statistics") or {}
        general = statistics.get("generalStatistics") or {}
        playing = statistics.get("playingTimeStatistics") or {}
        if general.get("participationState") != "played" and not playing.get("playedMinutes"):
            continue
        club_id = str((performance.get("clubsInformation") or {}).get("club", {}).get("clubId") or "")
        competition_id = str(game.get("competitionId") or "")
        season_id = int(game.get("seasonId") or 0)
        season_display = (game.get("season") or {}).get("display") or ""
        key = (season_id, season_display, club_id, competition_id)
        row = groups.setdefault(
            key,
            {
                "season": normalized_season(season_display, season_id),
                "team": (
                    (clubs.get(club_id) or {}).get("baseDetails", {}).get("shortName")
                    or (clubs.get(club_id) or {}).get("name")
                    or f"Club {club_id}"
                ),
                "competition": (
                    (competitions.get(competition_id) or {}).get("shortName")
                    or (competitions.get(competition_id) or {}).get("name")
                    or competition_id
                ),
                "rating": None,
                "matches": 0,
                "minutes": 0,
                "goals": 0,
                "assists": 0,
                "yellowCards": 0,
                "redCards": None,
                "source": "Transfermarkt performance API",
            },
        )
        goals = statistics.get("goalStatistics") or {}
        cards = statistics.get("cardStatistics") or {}
        row["matches"] += 1
        row["minutes"] += int(playing.get("playedMinutes") or 0)
        row["goals"] += int(
            goals.get("goalsScoredTotalOfficial")
            if goals.get("goalsScoredTotalOfficial") is not None
            else goals.get("goalsScoredTotal")
            or 0
        )
        row["assists"] += int(
            goals.get("assistsOfficial")
            if goals.get("assistsOfficial") is not None
            else goals.get("assists")
            or 0
        )
        row["yellowCards"] += int(cards.get("yellowCardGross") or 0)
    return [
        row
        for _, row in sorted(
            groups.items(),
            key=lambda item: (item[0][0], item[1]["matches"], item[1]["competition"]),
            reverse=True,
        )
    ]


def enrich_empty_careers(players: list[dict], workers: int) -> list[dict]:
    targets = [
        player
        for player in players
        if not player.get("career") and player.get("transfermarktPlayerId")
    ]
    if not targets:
        return []
    performance_data = {}
    errors = []

    def fetch_performance(player: dict):
        player_id = str(player["transfermarktPlayerId"])
        response = fetch_json(f"{TM_API}/player/{player_id}/performance-game")
        if not response.get("success") or not isinstance(response.get("data"), dict):
            raise RuntimeError(f"{player['team']} {player['name']}: invalid performance response")
        return player_id, response["data"]

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(fetch_performance, player): player for player in targets}
        for future in as_completed(futures):
            player = futures[future]
            try:
                player_id, data = future.result()
                performance_data[player_id] = data
            except Exception as error:
                errors.append(f"{player['team']} {player['name']}: {error}")
    if errors:
        raise RuntimeError("Career fetch failed: " + "; ".join(errors))

    club_ids = set()
    competition_ids = set()
    for data in performance_data.values():
        club_ids.update(str(value) for value in data.get("clubIds", []))
        competition_ids.update(str(value) for value in data.get("competitionIds", []))
    clubs = batch_entities("clubs", club_ids)
    competitions = batch_entities("competitions", competition_ids)
    enriched = []
    for player in targets:
        player_id = str(player["transfermarktPlayerId"])
        career = aggregate_career(performance_data[player_id], clubs, competitions)
        player["career"] = career
        player["careerSource"] = "Transfermarkt performance API"
        player["careerCheckedAt"] = now_iso()
        enriched.append(
            {
                "team": player["team"],
                "name": player["name"],
                "transfermarktPlayerId": player_id,
                "careerRows": len(career),
            }
        )
    return enriched


def load_baseline(index_path: Path) -> tuple[list[dict], dict[str, dict]]:
    if not index_path.exists():
        return [], {}
    source = index_path.read_text(encoding="utf-8")

    def constant(name: str):
        marker = f"const {name} = "
        start = source.find(marker)
        if start < 0:
            return None
        value_start = start + len(marker)
        end = source.find(";\n", value_start)
        return json.loads(source[value_start:end])

    rosters = constant("AUTHORITATIVE_2026_27_ROSTERS") or {}
    values = constant("VERIFIED_TRANSFERMARKT_VALUES_202627") or []
    players = []
    for team, groups in rosters.items():
        for position in ("GK", "D", "M", "A"):
            for name in groups.get(position, []):
                players.append({"team": team, "name": name, "pos": position})
    values_by_id = {
        str(row.get("transfermarktPlayerId")): row
        for row in values
        if row.get("transfermarktPlayerId")
    }
    values_by_team_name = {
        f"{row.get('team')}|{identity_key(row.get('name', ''))}": row
        for row in values
    }
    for player in players:
        value = values_by_team_name.get(
            f"{player['team']}|{identity_key(player['name'])}"
        )
        if value:
            player["transfermarktPlayerId"] = value.get("transfermarktPlayerId")
            player["marketValueEur"] = value.get("marketValueEur")
    return players, values_by_id


def reconcile(
    official_clubs: dict,
    tm_clubs: dict,
    donors: list[dict],
    checked_at: str,
) -> tuple[list[dict], list[dict]]:
    official_rows = [
        copy.deepcopy(player)
        for club in official_clubs.values()
        for player in club["players"]
    ]
    tm_rows = [
        copy.deepcopy(player)
        for club in tm_clubs.values()
        for player in club["players"]
    ]
    tm_by_id = {str(row["transfermarktPlayerId"]): row for row in tm_rows}

    # Resolve duplicate official registrations before matching.
    resolved_official = []
    ignored = []
    for row in official_rows:
        player_id = str(row["chanceLigaPlayerId"])
        if player_id in IGNORED_OFFICIAL_PLAYER_IDS:
            ignored.append({**row, "reason": "verified different current club/player identity"})
            continue
        chosen_team = OFFICIAL_DUPLICATE_TEAM.get(player_id)
        if chosen_team and row["team"] != chosen_team:
            ignored.append({**row, "reason": f"duplicate official id; current team is {chosen_team}"})
            continue
        resolved_official.append(row)

    attached_official = set()
    official_for_tm = {}
    for official in resolved_official:
        manual_tm_id = OFFICIAL_TO_TM_ID.get(str(official["chanceLigaPlayerId"]))
        match = tm_by_id.get(manual_tm_id) if manual_tm_id else None
        if not match:
            same_team = [row for row in tm_rows if row["team"] == official["team"]]
            result = best_match(official["name"], same_team, threshold=0.78)
            match = result[1] if result else None
        if not match:
            result = best_match(official["name"], tm_rows, threshold=0.91)
            if result:
                cross_team = result[1]
                if cross_team["team"] != official["team"]:
                    ignored.append(
                        {
                            **official,
                            "reason": (
                                f"current Transfermarkt squad places player at "
                                f"{cross_team['team']}"
                            ),
                        }
                    )
                    attached_official.add(id(official))
                    continue
                match = cross_team
        if match:
            official_for_tm[str(match["transfermarktPlayerId"])] = official
            attached_official.add(id(official))

    final = []
    used_donors = set()
    for tm_player in tm_rows:
        official = official_for_tm.get(str(tm_player["transfermarktPlayerId"]))
        target = {
            **tm_player,
            **(
                {
                    "chanceLigaPlayerId": official["chanceLigaPlayerId"],
                    "chanceLigaUrl": official["chanceLigaUrl"],
                }
                if official
                else {}
            ),
        }
        donor = choose_donor(target, donors, used_donors, allow_cross_team=True)
        if donor:
            used_donors.add(id(donor))
        player = copy.deepcopy(donor) if donor else {}
        name = donor.get("name") if donor else app_name(official["name"] if official else tm_player["name"])
        position = (
            donor.get("pos")
            if donor and donor.get("pos") in {"GK", "D", "M", "A"}
            else official["position"]
            if official
            else tm_player["position"]
        )
        player.update(
            {
                "name": name,
                "team": tm_player["team"],
                "pos": position,
                "mv": tm_player.get("mv"),
                "marketValueEur": tm_player.get("marketValueEur"),
                "marketValueSource": "Transfermarkt current squad",
                "marketValueSeason": SEASON,
                "marketValueCheckedAt": checked_at,
                "marketValueStatus": (
                    None if tm_player.get("marketValueEur") is not None else "no-published-value"
                ),
                "transfermarktPlayerId": str(tm_player["transfermarktPlayerId"]),
                "transfermarktUrl": tm_player["transfermarktUrl"],
                "transfermarktSquadUrl": tm_player["transfermarktSquadUrl"],
                "rosterVerifiedAt": checked_at,
                "rosterSourceUrl": (
                    official_clubs[tm_player["team"]]["sourceUrl"]
                    if official
                    else tm_player["transfermarktSquadUrl"]
                ),
                "matches": copy.deepcopy(player.get("matches") or []),
                "career": copy.deepcopy(player.get("career") or []),
            }
        )
        if official:
            player.update(
                {
                    "chanceLigaPlayerId": str(official["chanceLigaPlayerId"]),
                    "chanceLigaUrl": official["chanceLigaUrl"],
                    "shirtNumber": official.get("shirtNumber"),
                    "dateOfBirth": official.get("dateOfBirth"),
                    "heightCm": official.get("heightCm"),
                    "weightKg": official.get("weightKg"),
                }
            )
        final.append(player)

    # Add official registrations absent from Transfermarkt's current squad
    # table.  Existing stable identities are preserved first; genuinely new
    # rows are resolved through Transfermarkt search.
    for official in resolved_official:
        if id(official) in attached_official:
            continue
        target = {**official, "name": official["name"]}
        manual_tm_id = OFFICIAL_TO_TM_ID.get(str(official["chanceLigaPlayerId"]))
        if manual_tm_id:
            target["transfermarktPlayerId"] = manual_tm_id
        donor = choose_donor(target, donors, used_donors, allow_cross_team=True)
        search = None
        if donor and donor.get("team") != official["team"]:
            # A strong cross-team identity is a transfer.  The official team
            # wins only when this is the explicitly resolved Alexis move.
            if str(official["chanceLigaPlayerId"]) != "4444":
                ignored.append(
                    {
                        **official,
                        "reason": f"existing stable identity is current at {donor.get('team')}",
                    }
                )
                used_donors.add(id(donor))
                continue
        if donor:
            used_donors.add(id(donor))
        search = search_transfermarkt(official["name"])
        if (
            donor
            and donor.get("transfermarktPlayerId")
            and search
            and str(search.get("transfermarktPlayerId"))
            != str(donor.get("transfermarktPlayerId"))
        ):
            search = None
        player = copy.deepcopy(donor) if donor else {}
        if search:
            player.update(search)
        player.update(
            {
                "name": donor.get("name") if donor else app_name(official["name"]),
                "team": official["team"],
                "pos": (
                    donor.get("pos")
                    if donor and donor.get("pos") in {"GK", "D", "M", "A"}
                    else official["position"]
                ),
                "chanceLigaPlayerId": str(official["chanceLigaPlayerId"]),
                "chanceLigaUrl": official["chanceLigaUrl"],
                "shirtNumber": official.get("shirtNumber"),
                "dateOfBirth": official.get("dateOfBirth"),
                "heightCm": official.get("heightCm"),
                "weightKg": official.get("weightKg"),
                "matches": copy.deepcopy(player.get("matches") or []),
                "career": copy.deepcopy(player.get("career") or []),
                "rosterSourceUrl": official_clubs[official["team"]]["sourceUrl"],
                "rosterVerifiedAt": checked_at,
                "marketValueSeason": SEASON,
                "marketValueCheckedAt": checked_at,
            }
        )
        if player.get("marketValueEur") is not None:
            player["mv"] = format_value(int(player["marketValueEur"]))
            player["marketValueSource"] = (
                "Transfermarkt player search" if search else player.get("marketValueSource") or "Transfermarkt"
            )
            player["marketValueStatus"] = None
        else:
            player["mv"] = None
            player["marketValueSource"] = (
                "Transfermarkt player search" if search else player.get("marketValueSource") or "Transfermarkt"
            )
            player["marketValueStatus"] = "no-published-value"
        final.append(player)

    return final, ignored


def validate(players: list[dict]) -> dict:
    errors = []
    warnings = []
    if set(player["team"] for player in players) != set(TEAM_ORDER):
        errors.append("final roster does not contain exactly the configured 16 teams")
    team_counts = Counter(player["team"] for player in players)
    for team in TEAM_ORDER:
        count = team_counts[team]
        if not 18 <= count <= 60:
            errors.append(f"{team}: implausible final roster size {count}")
    tm_ids = defaultdict(list)
    chance_ids = defaultdict(list)
    team_names = defaultdict(list)
    for player in players:
        if player.get("pos") not in {"GK", "D", "M", "A"}:
            errors.append(f"{player.get('team')} {player.get('name')}: invalid position")
        tm_id = str(player.get("transfermarktPlayerId") or "")
        chance_id = str(player.get("chanceLigaPlayerId") or "")
        if tm_id:
            tm_ids[tm_id].append(f"{player['team']} {player['name']}")
        else:
            warnings.append(f"{player['team']} {player['name']}: missing Transfermarkt identity")
        if chance_id:
            chance_ids[chance_id].append(f"{player['team']} {player['name']}")
        team_names[(player["team"], identity_key(player["name"]))].append(player["name"])
        value = player.get("marketValueEur")
        if value is not None and (not isinstance(value, int) or value < 0 or value > 250_000_000):
            errors.append(f"{player['team']} {player['name']}: implausible market value {value}")
    for player_id, rows in tm_ids.items():
        if len(rows) > 1:
            errors.append(f"duplicate Transfermarkt id {player_id}: {rows}")
    for player_id, rows in chance_ids.items():
        if len(rows) > 1:
            errors.append(f"duplicate Chance Liga id {player_id}: {rows}")
    for (team, key), names in team_names.items():
        if len(names) > 1:
            errors.append(f"{team}: duplicate normalized identity {key}: {names}")
    if errors:
        raise RuntimeError("Roster validation failed: " + "; ".join(errors))
    return {
        "clubCount": len(team_counts),
        "playerCount": len(players),
        "teamCounts": dict(sorted(team_counts.items())),
        "withMarketValue": sum(player.get("marketValueEur") is not None for player in players),
        "withoutPublishedMarketValue": sum(
            player.get("marketValueEur") is None for player in players
        ),
        "withCareer": sum(bool(player.get("career")) for player in players),
        "withoutCareerRows": sum(not player.get("career") for player in players),
        "warnings": warnings,
    }


def compute_changes(
    baseline: list[dict],
    baseline_values_by_id: dict[str, dict],
    players: list[dict],
) -> dict:
    baseline_by_id = {
        str(player["transfermarktPlayerId"]): player
        for player in baseline
        if player.get("transfermarktPlayerId")
    }
    final_by_id = {
        str(player["transfermarktPlayerId"]): player
        for player in players
        if player.get("transfermarktPlayerId")
    }
    baseline_fallback = {
        (player["team"], identity_key(player["name"])): player
        for player in baseline
        if not player.get("transfermarktPlayerId")
    }
    final_fallback = {
        (player["team"], identity_key(player["name"])): player
        for player in players
        if not player.get("transfermarktPlayerId")
    }
    added = [
        {
            "team": player["team"],
            "name": player["name"],
            "transfermarktPlayerId": player.get("transfermarktPlayerId"),
        }
        for player_id, player in final_by_id.items()
        if player_id not in baseline_by_id
    ]
    added.extend(
        {"team": player["team"], "name": player["name"], "transfermarktPlayerId": None}
        for key, player in final_fallback.items()
        if key not in baseline_fallback
    )
    removed = [
        {
            "team": player["team"],
            "name": player["name"],
            "transfermarktPlayerId": player.get("transfermarktPlayerId"),
        }
        for player_id, player in baseline_by_id.items()
        if player_id not in final_by_id
    ]
    removed.extend(
        {"team": player["team"], "name": player["name"], "transfermarktPlayerId": None}
        for key, player in baseline_fallback.items()
        if key not in final_fallback
    )
    moved = [
        {
            "name": final_by_id[player_id]["name"],
            "from": baseline_by_id[player_id]["team"],
            "to": final_by_id[player_id]["team"],
            "transfermarktPlayerId": player_id,
        }
        for player_id in sorted(set(baseline_by_id) & set(final_by_id))
        if baseline_by_id[player_id]["team"] != final_by_id[player_id]["team"]
    ]
    value_updates = []
    for player_id, player in final_by_id.items():
        old = baseline_values_by_id.get(player_id, {})
        if old.get("marketValueEur") != player.get("marketValueEur"):
            value_updates.append(
                {
                    "team": player["team"],
                    "name": player["name"],
                    "transfermarktPlayerId": player_id,
                    "from": old.get("marketValueEur"),
                    "to": player.get("marketValueEur"),
                }
            )
    return {
        "added": sorted(added, key=lambda row: (row["team"], row["name"])),
        "removed": sorted(removed, key=lambda row: (row["team"], row["name"])),
        "moved": sorted(moved, key=lambda row: (row["from"], row["name"])),
        "marketValueUpdated": sorted(
            value_updates, key=lambda row: (row["team"], row["name"])
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("rosters-live.json"))
    parser.add_argument("--seed", type=Path)
    parser.add_argument("--index", type=Path, default=Path("index.html"))
    parser.add_argument("--audit-output", type=Path, default=Path("roster-audit-live.json"))
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--skip-career", action="store_true")
    args = parser.parse_args()

    checked_at = now_iso()
    official_clubs = {}
    tm_clubs = {}
    for team, official_path, club_id, slug in TEAM_CONFIG:
        official_clubs[team] = scrape_official(team, official_path)
        tm_clubs[team] = scrape_transfermarkt(team, club_id, slug)
    donors = load_seed(args.seed, args.output)
    players, ignored = reconcile(official_clubs, tm_clubs, donors, checked_at)
    enriched = [] if args.skip_career else enrich_empty_careers(players, args.workers)
    validation = validate(players)
    if args.output.exists():
        previous = json.loads(args.output.read_text(encoding="utf-8"))
        baseline = previous.get("players") or []
        baseline_values = {
            str(player.get("transfermarktPlayerId")): player
            for player in baseline
            if player.get("transfermarktPlayerId")
        }
    else:
        baseline, baseline_values = load_baseline(args.index)
    changes = compute_changes(baseline, baseline_values, players)

    position_order = {"GK": 0, "D": 1, "M": 2, "A": 3}
    team_index = {team: index for index, team in enumerate(TEAM_ORDER)}
    players.sort(
        key=lambda player: (
            team_index[player["team"]],
            position_order[player["pos"]],
            normalize(player["name"]),
        )
    )
    rosters = {
        team: {
            position: [
                player["name"]
                for player in players
                if player["team"] == team and player["pos"] == position
            ]
            for position in ("GK", "D", "M", "A")
        }
        for team in TEAM_ORDER
    }
    payload = {
        "schemaVersion": 1,
        "season": SEASON,
        "generatedAt": checked_at,
        "sources": {
            "officialClubList": CHANCE_BASE + "/kluby",
            "officialClubs": {
                team: club["sourceUrl"] for team, club in official_clubs.items()
            },
            "transfermarktLeague": (
                f"{TM_BASE}/chance-liga/startseite/wettbewerb/TS1/"
                f"plus/?saison_id={SEASON_ID}"
            ),
            "transfermarktClubs": {
                team: club["sourceUrl"] for team, club in tm_clubs.items()
            },
        },
        "rosters": rosters,
        "players": players,
        "changes": changes,
        "validation": validation,
    }
    audit = {
        "generatedAt": checked_at,
        "changes": changes,
        "validation": validation,
        "ignoredSourceRows": ignored,
        "careerEnriched": enriched,
        "sourceCounts": {
            "officialRows": sum(len(club["players"]) for club in official_clubs.values()),
            "transfermarktRows": sum(len(club["players"]) for club in tm_clubs.values()),
            "seedRows": len(donors),
        },
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.audit_output.parent.mkdir(parents=True, exist_ok=True)
    for target, data in ((args.output, payload), (args.audit_output, audit)):
        content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=target.parent,
            prefix=target.name + ".",
            suffix=".tmp",
        ) as handle:
            handle.write(content)
            temporary = Path(handle.name)
        os.replace(temporary, target)

    print(
        json.dumps(
            {
                "output": str(args.output),
                "players": validation["playerCount"],
                "withMarketValue": validation["withMarketValue"],
                "withCareer": validation["withCareer"],
                "added": len(changes["added"]),
                "removed": len(changes["removed"]),
                "moved": len(changes["moved"]),
                "marketValueUpdated": len(changes["marketValueUpdated"]),
                "ignoredSourceRows": len(ignored),
                "careerEnriched": len(enriched),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
