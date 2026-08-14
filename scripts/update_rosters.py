#!/usr/bin/env python3
"""Build the live Chance Liga roster and market-value data layer.

Membership is reconciled from the official Chance Liga club pages, the current
Transfermarkt squad pages and the current Livesport team rosters. Existing
match and career data is preserved from the previous live file (or an
explicitly supplied seed). The script validates the complete result before
atomically replacing rosters-live.json.
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
LIVESPORT_BASE = "https://www.livesport.cz"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/138 Safari/537.36"
)
TEAM_CONFIG = [
    ("SlovĂˇcko", "/klub/16-1-fc-slovacko", 5544, "1-fc-slovacko"),
    ("Sparta Prague", "/klub/2-ac-sparta-praha", 197, "ac-sparta-prag"),
    ("Bohemians 1905", "/klub/20-bohemians-praha-1905", 715, "fc-bohemians-prag-1905"),
    ("BanĂ­k Ostrava", "/klub/14-fc-banik-ostrava", 377, "fc-banik-ostrau"),
    ("Hradec KrĂˇlovĂ©", "/klub/11-fc-hradec-kralove", 1897, "fc-hradec-kralove"),
    ("Slovan Liberec", "/klub/7-fc-slovan-liberec", 697, "fc-slovan-liberec"),
    ("Viktoria PlzeĹ", "/klub/6-fc-viktoria-plzen", 941, "fc-viktoria-pilsen"),
    ("Zbrojovka Brno", "/klub/9-fc-zbrojovka-brno", 5225, "fc-zbrojovka-brunn"),
    ("ZlĂ­n", "/klub/33-fc-zlin", 5545, "fc-fastav-zlin"),
    ("Jablonec", "/klub/4-fk-jablonec", 1322, "fk-jablonec"),
    ("MladĂˇ Boleslav", "/klub/8-fk-mlada-boleslav", 5546, "fk-mlada-boleslav"),
    ("Pardubice", "/klub/39-fk-pardubice", 1496, "fk-pardubice"),
    ("Teplice", "/klub/17-fk-teplice", 814, "fk-teplice"),
    ("Artis Brno", "/klub/41-sk-artis-brno", 24325, "sk-lisen"),
    ("Sigma Olomouc", "/klub/13-sk-sigma-olomouc", 2311, "sk-sigma-olmutz"),
    ("Slavia Prague", "/klub/5-sk-slavia-praha", 62, "sk-slavia-prag"),
]
TEAM_ORDER = [row[0] for row in TEAM_CONFIG]
OFFICIAL_POSITION = {"B": "GK", "O": "D", "Z": "M", "U": "A"}
LIVESPORT_TEAM_CONFIG = {
    "Artis Brno": ("artis-brno", "zHLktbZ1"),
    "BanĂ­k Ostrava": ("banik-ostrava", "lI6ddlih"),
    "Bohemians 1905": ("bohemians-1905", "fuXqHnxa"),
    "Hradec KrĂˇlovĂ©": ("hradec-kralove", "vFXjbHms"),
    "Jablonec": ("jablonec", "CM8ySpMH"),
    "MladĂˇ Boleslav": ("mlada-boleslav", "0f7GpAMu"),
    "Pardubice": ("pardubice", "Ys4YYBPn"),
    "Sigma Olomouc": ("sigma-olomouc", "drA4fSL4"),
    "Slavia Prague": ("slavia-praha", "viXGgnyB"),
    "SlovĂˇcko": ("slovacko", "MNEDyOlF"),
    "Slovan Liberec": ("slovan-liberec", "4bp6yRjU"),
    "Sparta Prague": ("sparta-praha", "6qA358jH"),
    "Teplice": ("teplice", "r9XWmtLq"),
    "Viktoria PlzeĹ": ("viktoria-plzen", "2LA0e86b"),
    "Zbrojovka Brno": ("zbrojovka-brno", "4d5TT6i5"),
    "ZlĂ­n": ("zlin", "C09N1Ikd"),
}
LIVESPORT_POSITION = {
    "brankari": "GK",
    "obranci": "D",
    "zaloznici": "M",
    "utocnici": "A",
}

# Fallbacks for older duplicate registrations when one of the two independent
# current-roster sources temporarily omits the player.  When Transfermarkt and
# Livesport are both available, their agreement always takes precedence.
OFFICIAL_DUPLICATE_TEAM = {
    "3142": "SlovĂˇcko",        # David Ĺ tÄ›pĂˇnek
    "4674": "MladĂˇ Boleslav",  # Filip Ĺ patenka
    "4444": "Artis Brno",      # Alexis AlĂ©guĂ©
}
# A different, older player with the same name is incorrectly present on the
# Artis page; his current Transfermarkt club is outside Chance Liga.
IGNORED_OFFICIAL_PLAYER_IDS = {"2693"}
# Name variants that cannot safely be paired by general fuzzy matching.
OFFICIAL_TO_TM_ID = {
    "4084": "261010",   # Vlasij Sinjavskij / Vlasiy Sinyavskiy
    "5181": "1109889",  # Kauan Carneiro Da Silva KakĂˇ / KakĂˇ
    "2970": "303440",   # Ladislav TakĂˇcs / Laco Takacs
    "5149": "558467",   # Michal JeĹ™Ăˇbek (born 1995)
    "3558": "401475",   # Murphy Dorley Oscar / Oscar
    "4455": "723415",   # HĂ©lio ... Papalele / PapalelĂ©
    "5067": "1052374",  # Jevgenij Skyba / Yevgeniy Skyba
    "4519": "1048442",  # Ogungbayi Boluwatife / Bolu Ogungbayi
    "5024": "717199",   # Bohdan Sliubyk / Bogdan Slyubyk
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def clean(value: str) -> str:
    return " ".join((value or "").split())


def normalize(value: str) -> str:
    text = unicodedata.normalize("NFD", value or "")
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = (
        text.replace("Ă¸", "o")
        .replace("Ă", "o")
        .replace("Ĺ‚", "l")
        .replace("Ĺ", "l")
        .replace("Ä‘", "d")
        .replace("Ä", "d")
        .replace("Ă°", "d")
        .replace("Ă", "d")
        .replace("Ă¦", "ae")
        .replace("Ă†", "ae")
        .replace("Ĺ“", "oe")
        .replace("Ĺ’", "oe")
        .replace("Ăź", "ss")
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


def roster_position(row: dict) -> str | None:
    return row.get("pos") or row.get("position")


def best_identity_match(
    name: str,
    candidates: list[dict],
    *,
    position: str | None = None,
) -> tuple[float, dict] | None:
    """Match names without letting a shared first name merge two people.

    Exact token identities and high-confidence extra-name variants are allowed
    across position labels because the three sources sometimes classify a
    winger differently. Lower-confidence fuzzy matching is only accepted when
    the position agrees.
    """

    exact = [row for row in candidates if identity_key(row.get("name", "")) == identity_key(name)]
    if len(exact) == 1:
        return 1.0, exact[0]
    if len(exact) > 1 and position:
        positioned = [row for row in exact if roster_position(row) == position]
        if len(positioned) == 1:
            return 1.0, positioned[0]
        return None
    high_confidence = best_match(name, candidates, threshold=0.94)
    if high_confidence:
        return high_confidence
    if position:
        positioned = [row for row in candidates if roster_position(row) == position]
        return best_match(name, positioned, threshold=0.86)
    return None


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
    text = clean(value).replace("â‚¬", "").replace(",", ".").lower()
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
        return f"â‚¬{value / 1_000_000:g}m"
    return f"â‚¬{round(value / 1000)}k"


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


def class_text(row, class_name: str) -> str:
    values = row.xpath(
        ".//*[contains(concat(' ',normalize-space(@class),' '),"
        f"' {class_name} ')]//text()"
    )
    return clean(" ".join(values))


def scrape_livesport(team: str, slug: str, team_id: str) -> dict:
    """Read the visible Chance Liga/current-roster tables from Livesport.

    Livesport renders separate domestic-league, international-cup and overall
    tables in the page HTML. Membership comes from the overall table, while
    season totals must come only from the ``league-*`` table. Using the first
    occurrence is incorrect for clubs playing in Europe because a player who
    has not yet appeared in the league may first occur in the cup table.
    """

    url = f"{LIVESPORT_BASE}/tym/{slug}/{team_id}/soupiska/"
    document = html.fromstring(
        fetch_bytes(
            url,
            referer=f"{LIVESPORT_BASE}/fotbal/cesko/chance-liga/",
            extra_headers={"Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.7"},
        )
    )
    profile_tables = document.xpath(
        "//div[contains(concat(' ',normalize-space(@class),' '),' profileTable ')]"
    )
    overall_table = next(
        (table for table in profile_tables if table.get("id") == "overall-all-table"),
        None,
    )
    league_table = next(
        (table for table in profile_tables if str(table.get("id") or "").startswith("league-")),
        None,
    )
    if overall_table is None or league_table is None:
        raise RuntimeError(f"{team}: Livesport roster is missing overall or league table")

    row_xpath = (
        ".//*[contains(concat(' ',normalize-space(@class),' '),' lineupTable__row ')]"
        "[.//a[contains(@href,'/hrac/')]]"
    )

    def parse_row(row) -> tuple[str, Any, str, str] | None:
        links = row.xpath(".//a[contains(@href,'/hrac/')]")
        if not links:
            return None
        link = links[0]
        path = link.get("href") or ""
        match = re.search(r"/hrac/([^/]+)/([^/]+)/", path)
        if not match:
            return None
        table = row.getparent()
        heading = clean(" ".join(table.xpath("./*[contains(@class,'lineupTable__title')]//text()")))
        position = LIVESPORT_POSITION.get(normalize(heading).replace(" ", ""))
        if not position or not row.xpath(
            ".//*[contains(concat(' ',normalize-space(@class),' '),' lineupTable__cell--matchesPlayed ')]"
        ):
            return None
        return match.group(2), link, path, position

    league_stats: dict[str, dict] = {}
    for row in league_table.xpath(row_xpath):
        parsed = parse_row(row)
        if parsed is None:
            continue
        player_id, _, _, _ = parsed
        stats = {
            "season": SEASON,
            "competition": "Chance Liga",
            "apps": parse_int(class_text(row, "lineupTable__cell--matchesPlayed")) or 0,
            "minutes": parse_int(class_text(row, "lineupTable__cell--minutesPlayed")) or 0,
            "goals": parse_int(class_text(row, "lineupTable__cell--goal")) or 0,
            "assists": parse_int(class_text(row, "lineupTable__cell--assist")) or 0,
            "yellowCards": parse_int(class_text(row, "lineupTable__cell--yellowCard")) or 0,
            "redCards": parse_int(class_text(row, "lineupTable__cell--redCard")) or 0,
            "source": "Livesport team roster",
            "sourceUrl": url,
        }
        league_stats[player_id] = stats

    players_by_id: dict[str, dict] = {}
    for row in overall_table.xpath(row_xpath):
        parsed = parse_row(row)
        if parsed is None:
            continue
        player_id, link, path, position = parsed
        stats = league_stats.get(
            player_id,
            {
                "season": SEASON,
                "competition": "Chance Liga",
                "apps": 0,
                "minutes": 0,
                "goals": 0,
                "assists": 0,
                "yellowCards": 0,
                "redCards": 0,
                "source": "Livesport domestic-league roster table",
                "sourceUrl": url,
            },
        )
        players_by_id[player_id] = {
            "name": clean(link.text_content()),
            "team": team,
            "position": position,
            "livesportPlayerId": player_id,
            "livesportUrl": urllib.parse.urljoin(LIVESPORT_BASE, path),
            "shirtNumber": parse_int(class_text(row, "lineupTable__cell--jersey")),
            "seasonStats": stats,
        }
    players = list(players_by_id.values())
    if not 18 <= len(players) <= 60:
        raise RuntimeError(f"{team}: Livesport roster has implausible size {len(players)}")
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
    value_eur = (
        int(current_value)
        if isinstance(current_value, (int, float)) and current_value > 0
        else None
    )
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
    result = best_identity_match(
        target["name"],
        same_team,
        position=roster_position(target),
    )
    if result:
        return result[1]
    if allow_cross_team:
        result = best_identity_match(
            target["name"],
            [donor for donor in donors if id(donor) not in used],
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


def roster_membership_teams(name: str, position: str, clubs: dict) -> set[str]:
    """Return teams containing one high-confidence identity in a roster source."""

    key = identity_key(name)
    teams = set()
    for team, club in clubs.items():
        rows = club.get("players") or []
        exact = [row for row in rows if identity_key(row.get("name", "")) == key]
        if exact:
            teams.add(team)
            continue
        result = best_identity_match(name, rows, position=position)
        if result and result[0] >= 0.94:
            teams.add(team)
    return teams


def resolve_official_registrations(
    official_rows: list[dict],
    tm_clubs: dict,
    livesport_clubs: dict,
) -> tuple[list[dict], list[dict]]:
    """Resolve duplicate official IDs only from unambiguous current membership.

    The official league site can retain one player under both an old and a new
    club.  A newly observed duplicate is accepted automatically only when the
    current Transfermarkt and Livesport rosters independently select the same
    team.  Otherwise the update fails before any live file is replaced.
    """

    by_id = defaultdict(list)
    for row in official_rows:
        by_id[str(row["chanceLigaPlayerId"])].append(row)

    resolved = []
    ignored = []
    processed_ids = set()
    for row in official_rows:
        player_id = str(row["chanceLigaPlayerId"])
        if player_id in processed_ids:
            continue
        processed_ids.add(player_id)
        registrations = by_id[player_id]

        if player_id in IGNORED_OFFICIAL_PLAYER_IDS:
            ignored.extend(
                {**registration, "reason": "verified different current club/player identity"}
                for registration in registrations
            )
            continue
        if len(registrations) == 1:
            resolved.append(registrations[0])
            continue

        reference = registrations[0]
        candidate_teams = {registration["team"] for registration in registrations}
        tm_teams = roster_membership_teams(
            reference["name"], reference["position"], tm_clubs
        )
        livesport_teams = roster_membership_teams(
            reference["name"], reference["position"], livesport_clubs
        )
        manual_team = OFFICIAL_DUPLICATE_TEAM.get(player_id)

        selected_team = None
        reason = None
        if len(tm_teams) == 1 and tm_teams == livesport_teams:
            selected_team = next(iter(tm_teams))
            reason = "Transfermarkt + Livesport consensus"
        elif (
            manual_team in candidate_teams
            and all(not teams or teams == {manual_team} for teams in (tm_teams, livesport_teams))
        ):
            selected_team = manual_team
            reason = "verified fallback without conflicting current-roster evidence"

        if selected_team not in candidate_teams:
            labels = [f"{registration['team']} {registration['name']}" for registration in registrations]
            raise RuntimeError(
                f"Cannot resolve duplicate official Chance Liga id {player_id}: {labels}; "
                f"Transfermarkt teams={sorted(tm_teams)}; "
                f"Livesport teams={sorted(livesport_teams)}; "
                f"configured fallback={manual_team!r}"
            )

        selected = [registration for registration in registrations if registration["team"] == selected_team]
        if len(selected) != 1:
            raise RuntimeError(
                f"Duplicate official Chance Liga id {player_id} has "
                f"{len(selected)} rows for selected team {selected_team}"
            )
        resolved.append(selected[0])
        ignored.extend(
            {
                **registration,
                "reason": (
                    f"duplicate official id; current team is {selected_team} "
                    f"({reason})"
                ),
            }
            for registration in registrations
            if registration is not selected[0]
        )
    return resolved, ignored


def reconcile(
    official_clubs: dict,
    tm_clubs: dict,
    livesport_clubs: dict,
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

    # Resolve stale duplicate league registrations before identity matching.
    resolved_official, ignored = resolve_official_registrations(
        official_rows, tm_clubs, livesport_clubs
    )

    attached_official = set()
    official_for_tm = {}
    matched_tm_ids = set()
    for official in resolved_official:
        manual_tm_id = OFFICIAL_TO_TM_ID.get(str(official["chanceLigaPlayerId"]))
        match = (
            tm_by_id.get(manual_tm_id)
            if manual_tm_id and manual_tm_id not in matched_tm_ids
            else None
        )
        if not match:
            same_team = [
                row
                for row in tm_rows
                if row["team"] == official["team"]
                and str(row["transfermarktPlayerId"]) not in matched_tm_ids
            ]
            result = best_identity_match(
                official["name"],
                same_team,
                position=official["position"],
            )
            match = result[1] if result else None
        if not match:
            cross_team_candidates = [
                row
                for row in tm_rows
                if str(row["transfermarktPlayerId"]) not in matched_tm_ids
            ]
            result = best_identity_match(
                official["name"],
                cross_team_candidates,
            )
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
            transfermarkt_id = str(match["transfermarktPlayerId"])
            if transfermarkt_id in official_for_tm:
                raise RuntimeError(
                    f"Transfermarkt identity {transfermarkt_id} matched multiple official players"
                )
            official_for_tm[transfermarkt_id] = official
            matched_tm_ids.add(transfermarkt_id)
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


def livesport_ids(player: dict) -> set[str]:
    output = set()
    direct = str(player.get("livesportPlayerId") or "").strip()
    if direct:
        output.add(direct)
    for field in ("livesportUrl", "sourceUrl", "careerSourceUrl"):
        match = re.search(r"/hrac/[^/]+/([^/]+)/", str(player.get(field) or ""))
        if match:
            output.add(match.group(1))
    for match_row in player.get("matches") or []:
        player_id = str(match_row.get("livesportPlayerId") or "").strip()
        if player_id:
            output.add(player_id)
    return output


def update_current_livesport_career(player: dict, stats: dict) -> None:
    """Expose a current-season aggregate even before match detail is imported."""

    if not stats.get("apps"):
        player["career"] = [
            row
            for row in player.get("career") or []
            if not (
                row.get("season") in {SEASON, "2026/2027"}
                and normalize(row.get("competition") or "") == "chance liga"
                and row.get("source") == "Livesport team roster"
            )
        ]
        return
    career = copy.deepcopy(player.get("career") or [])
    current = next(
        (
            row
            for row in career
            if row.get("season") in {SEASON, "2026/2027"}
            and normalize(row.get("competition") or "") == "chance liga"
            and name_score(row.get("team") or "", player["team"]) >= 0.78
        ),
        None,
    )
    if current is None:
        current = {
            "season": SEASON,
            "team": player["team"],
            "competition": "Chance Liga",
            "rating": None,
        }
        career.insert(0, current)
    current.update(
        {
            "matches": stats["apps"],
            "minutes": stats["minutes"],
            "goals": stats["goals"],
            "assists": stats["assists"],
            "yellowCards": stats["yellowCards"],
            "redCards": stats["redCards"],
            "source": stats["source"],
            "sourceUrl": stats["sourceUrl"],
        }
    )
    player["career"] = career


def attach_match_team_context(players: list[dict]) -> None:
    """Persist the club represented in a match so later transfers stay valid."""

    team_ids = {
        team_id: team
        for team, (_, team_id) in LIVESPORT_TEAM_CONFIG.items()
    }
    for player in players:
        for match in player.get("matches") or []:
            if match.get("team"):
                continue
            source_url = str(match.get("sourceUrl") or "")
            opponent = normalize(match.get("opponent") or match.get("vs") or "")
            candidates = [
                team
                for team_id, team in team_ids.items()
                if team_id in source_url and normalize(team) != opponent
            ]
            if len(candidates) == 1:
                match["team"] = candidates[0]


def attach_livesport(
    players: list[dict],
    livesport_clubs: dict,
    donors: list[dict],
    checked_at: str,
) -> tuple[list[dict], dict]:
    """Attach stable Livesport identities/stats and add newly listed players.

    Livesport is the final current-club signal. Official and Transfermarkt rows
    remain preserved when Livesport does not list them, but a player shown by
    Livesport can never disappear from the published roster.
    """

    source_rows = [
        copy.deepcopy(player)
        for club in livesport_clubs.values()
        for player in club["players"]
    ]
    source_id_owners = defaultdict(list)
    for row in source_rows:
        source_id_owners[str(row["livesportPlayerId"])].append(row["team"])
    duplicate_source_ids = {
        player_id: teams
        for player_id, teams in source_id_owners.items()
        if len(set(teams)) > 1
    }
    if duplicate_source_ids:
        raise RuntimeError(f"Livesport player IDs occur at multiple clubs: {duplicate_source_ids}")

    matched_player_ids = set()
    added = []
    moved = []
    attached = []
    donor_by_livesport_id = {
        player_id: donor
        for donor in donors
        for player_id in livesport_ids(donor)
    }

    for source in source_rows:
        player_id = str(source["livesportPlayerId"])
        player = next(
            (
                candidate
                for candidate in players
                if id(candidate) not in matched_player_ids
                and player_id in livesport_ids(candidate)
                and name_score(candidate.get("name", ""), source["name"]) >= 0.90
            ),
            None,
        )
        match_method = "livesport-id" if player else None
        if player is None:
            same_team = [
                candidate
                for candidate in players
                if candidate["team"] == source["team"]
                and id(candidate) not in matched_player_ids
            ]
            result = best_identity_match(
                source["name"],
                same_team,
                position=source["position"],
            )
            if result:
                player = result[1]
                match_method = "same-team-name"
        if player is None:
            cross_team_candidates = [
                candidate
                for candidate in players
                if id(candidate) not in matched_player_ids
            ]
            result = best_identity_match(
                source["name"],
                cross_team_candidates,
            )
            if result:
                player = result[1]
                match_method = "cross-team-name"

        if player is None:
            donor_candidate = donor_by_livesport_id.get(player_id)
            donor = (
                donor_candidate
                if donor_candidate
                and name_score(donor_candidate.get("name", ""), source["name"]) >= 0.90
                else None
            )
            player = copy.deepcopy(donor) if donor else {}
            search = search_transfermarkt(source["name"])
            if search:
                player.update(search)
            player.update(
                {
                    "name": donor.get("name") if donor else source["name"],
                    "team": source["team"],
                    "pos": source["position"],
                    "matches": copy.deepcopy(player.get("matches") or []),
                    "career": copy.deepcopy(player.get("career") or []),
                    "marketValueSeason": SEASON,
                    "marketValueCheckedAt": checked_at,
                    "marketValueSource": (
                        "Transfermarkt player search"
                        if search
                        else player.get("marketValueSource") or "Transfermarkt"
                    ),
                    "marketValueStatus": (
                        None
                        if player.get("marketValueEur") is not None
                        else "no-published-value"
                    ),
                }
            )
            if player.get("marketValueEur") is not None:
                player["mv"] = format_value(int(player["marketValueEur"]))
            players.append(player)
            added.append(
                {
                    "team": source["team"],
                    "name": player["name"],
                    "livesportPlayerId": player_id,
                }
            )
            match_method = "livesport-new"

        previous_team = player.get("team")
        previous_livesport = {
            "team": previous_team,
            "pos": player.get("pos"),
            "livesportPlayerId": player.get("livesportPlayerId"),
            "livesportUrl": player.get("livesportUrl"),
            "shirtNumber": player.get("shirtNumber"),
            "seasonStats": {
                key: value
                for key, value in (player.get("livesportSeasonStats") or {}).items()
                if key != "checkedAt"
            },
        }
        if previous_team != source["team"]:
            moved.append(
                {
                    "name": player.get("name") or source["name"],
                    "from": previous_team,
                    "to": source["team"],
                    "livesportPlayerId": player_id,
                }
            )
            player["team"] = source["team"]
        player["pos"] = source["position"]
        player["livesportPlayerId"] = player_id
        player["livesportUrl"] = source["livesportUrl"]
        player["livesportRosterSourceUrl"] = livesport_clubs[source["team"]]["sourceUrl"]
        if source.get("shirtNumber") is not None:
            player["shirtNumber"] = source["shirtNumber"]
        current_livesport = {
            "team": player.get("team"),
            "pos": player.get("pos"),
            "livesportPlayerId": player_id,
            "livesportUrl": source["livesportUrl"],
            "shirtNumber": player.get("shirtNumber"),
            "seasonStats": source["seasonStats"],
        }
        livesport_changed = previous_livesport != current_livesport
        verified_at = (
            checked_at
            if livesport_changed
            else player.get("livesportRosterVerifiedAt") or checked_at
        )
        stats_checked_at = (
            checked_at
            if livesport_changed
            else (player.get("livesportSeasonStats") or {}).get("checkedAt") or checked_at
        )
        player["livesportRosterVerifiedAt"] = verified_at
        player["livesportSeasonStats"] = {
            **source["seasonStats"],
            "checkedAt": stats_checked_at,
        }
        update_current_livesport_career(player, player["livesportSeasonStats"])
        matched_player_ids.add(id(player))
        attached.append(
            {
                "team": source["team"],
                "name": player["name"],
                "livesportName": source["name"],
                "livesportPlayerId": player_id,
                "method": match_method,
                "apps": source["seasonStats"]["apps"],
                "goals": source["seasonStats"]["goals"],
            }
        )

    source_ids = {str(row["livesportPlayerId"]) for row in source_rows}
    unresolved = [
        {
            "team": row["team"],
            "name": row["name"],
            "livesportPlayerId": row["livesportPlayerId"],
            "apps": row["seasonStats"]["apps"],
            "goals": row["seasonStats"]["goals"],
        }
        for row in source_rows
        if not any(str(row["livesportPlayerId"]) in livesport_ids(player) for player in players)
    ]
    if unresolved:
        raise RuntimeError(f"Livesport roster rows were not attached: {unresolved}")
    attach_match_team_context(players)
    return players, {
        "sourceRows": len(source_rows),
        "attached": attached,
        "added": added,
        "moved": moved,
        "unresolved": unresolved,
        "publishedLivesportIds": len(
            {
                player_id
                for player in players
                for player_id in livesport_ids(player)
                if player_id in source_ids
            }
        ),
    }


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
    livesport_player_ids = defaultdict(list)
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
        for livesport_player_id in livesport_ids(player):
            livesport_player_ids[livesport_player_id].append(
                f"{player['team']} {player['name']}"
            )
        if len(livesport_ids(player)) > 1:
            errors.append(
                f"{player['team']} {player['name']}: multiple Livesport identities "
                f"{sorted(livesport_ids(player))}"
            )
        team_names[(player["team"], identity_key(player["name"]))].append(player["name"])
        value = player.get("marketValueEur")
        if value is not None and (not isinstance(value, int) or value < 0 or value > 250_000_000):
            errors.append(f"{player['team']} {player['name']}: implausible market value {value}")
        season_stats = player.get("livesportSeasonStats")
        if season_stats:
            apps = season_stats.get("apps")
            minutes = season_stats.get("minutes")
            goals = season_stats.get("goals")
            assists = season_stats.get("assists")
            yellow_cards = season_stats.get("yellowCards")
            red_cards = season_stats.get("redCards")
            values = (apps, minutes, goals, assists, yellow_cards, red_cards)
            if any(not isinstance(item, int) or item < 0 for item in values):
                errors.append(f"{player['team']} {player['name']}: invalid Livesport season totals")
            elif (
                apps > 60
                or minutes > apps * 130
                or goals > max(20, apps * 5)
                or assists > max(20, apps * 5)
                or yellow_cards > apps * 2
                or red_cards > apps
            ):
                errors.append(
                    f"{player['team']} {player['name']}: implausible Livesport season totals "
                    f"{season_stats}"
                )
    for player_id, rows in tm_ids.items():
        if len(rows) > 1:
            errors.append(f"duplicate Transfermarkt id {player_id}: {rows}")
    for player_id, rows in chance_ids.items():
        if len(rows) > 1:
            errors.append(f"duplicate Chance Liga id {player_id}: {rows}")
    for player_id, rows in livesport_player_ids.items():
        if len(rows) > 1:
            errors.append(f"duplicate Livesport player id {player_id}: {rows}")
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
        "withLivesportIdentity": sum(bool(livesport_ids(player)) for player in players),
        "withLivesportSeasonStats": sum(bool(player.get("livesportSeasonStats")) for player in players),
        "activeLivesportPlayers": sum(
            int((player.get("livesportSeasonStats") or {}).get("apps") or 0) > 0
            for player in players
        ),
        "activeLivesportScorers": sum(
            int((player.get("livesportSeasonStats") or {}).get("goals") or 0) > 0
            for player in players
        ),
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
    livesport_clubs = {}
    for team, official_path, club_id, slug in TEAM_CONFIG:
        official_clubs[team] = scrape_official(team, official_path)
        tm_clubs[team] = scrape_transfermarkt(team, club_id, slug)
        livesport_slug, livesport_team_id = LIVESPORT_TEAM_CONFIG[team]
        livesport_clubs[team] = scrape_livesport(
            team, livesport_slug, livesport_team_id
        )
    donors = load_seed(args.seed, args.output)
    players, ignored = reconcile(
        official_clubs, tm_clubs, livesport_clubs, donors, checked_at
    )
    players, livesport_reconciliation = attach_livesport(
        players, livesport_clubs, donors, checked_at
    )
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
            "livesportClubs": {
                team: club["sourceUrl"] for team, club in livesport_clubs.items()
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
        "livesportReconciliation": livesport_reconciliation,
        "careerEnriched": enriched,
        "sourceCounts": {
            "officialRows": sum(len(club["players"]) for club in official_clubs.values()),
            "transfermarktRows": sum(len(club["players"]) for club in tm_clubs.values()),
            "livesportRows": sum(len(club["players"]) for club in livesport_clubs.values()),
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

