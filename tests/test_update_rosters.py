import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from update_rosters import TEAM_ORDER, reconcile, resolve_official_registrations
import update_rosters as updater


def official(team, player_id="9000", name="Roman Example"):
    return {
        "name": name,
        "team": team,
        "position": "M",
        "chanceLigaPlayerId": player_id,
        "chanceLigaUrl": f"https://www.chanceliga.cz/hrac/{player_id}-roman-example",
    }


def source(*memberships):
    clubs = {}
    for team, name in memberships:
        clubs.setdefault(team, {"players": []})["players"].append(
            {"name": name, "position": "M"}
        )
    return clubs


class OfficialDuplicateResolutionTests(unittest.TestCase):
    def test_czech_team_names_keep_their_utf8_diacritics(self):
        expected = {
            "Ban\u00edk Ostrava",
            "Hradec Kr\u00e1lov\u00e9",
            "Mlad\u00e1 Boleslav",
            "Slov\u00e1cko",
            "Viktoria Plze\u0148",
            "Zl\u00edn",
        }

        self.assertTrue(expected.issubset(set(TEAM_ORDER)))

    def test_independent_sources_select_same_current_team(self):
        rows = [official("Old Club"), official("Current Club")]
        resolved, ignored = resolve_official_registrations(
            rows,
            source(("Current Club", "Roman Example")),
            source(("Current Club", "Example Roman")),
        )

        self.assertEqual([row["team"] for row in resolved], ["Current Club"])
        self.assertEqual([row["team"] for row in ignored], ["Old Club"])
        self.assertIn("Transfermarkt + Livesport consensus", ignored[0]["reason"])

    def test_source_disagreement_fails_closed(self):
        rows = [official("Club A"), official("Club B")]

        with self.assertRaisesRegex(RuntimeError, "Cannot resolve duplicate official"):
            resolve_official_registrations(
                rows,
                source(("Club A", "Roman Example")),
                source(("Club B", "Example Roman")),
            )

    def test_non_duplicate_registration_is_preserved(self):
        row = official("Only Club")
        resolved, ignored = resolve_official_registrations([row], {}, {})

        self.assertEqual(resolved, [row])
        self.assertEqual(ignored, [])

    def test_same_club_duplicate_identity_uses_livesport_shirt_number(self):
        old = {
            **official("Jablonec", "5233", "Saidou Alioum"),
            "dateOfBirth": "25.07.2003",
            "heightCm": 175,
            "shirtNumber": None,
        }
        current = {
            **official("Jablonec", "5238", "Saidou Alioum"),
            "dateOfBirth": "25.07.2003",
            "heightCm": None,
            "shirtNumber": 17,
        }
        livesport = {
            "Jablonec": {
                "players": [
                    {
                        "name": "Alioum Saidou",
                        "position": "M",
                        "shirtNumber": 17,
                    }
                ]
            }
        }

        resolved, ignored = resolve_official_registrations(
            [old, current], {}, livesport
        )

        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["chanceLigaPlayerId"], "5238")
        self.assertEqual(resolved[0]["heightCm"], 175)
        self.assertEqual([row["chanceLigaPlayerId"] for row in ignored], ["5233"])

    def test_same_club_duplicate_identity_without_current_evidence_fails(self):
        rows = [
            official("Jablonec", "5233", "Saidou Alioum"),
            official("Jablonec", "5238", "Saidou Alioum"),
        ]

        with self.assertRaisesRegex(RuntimeError, "duplicate official identity"):
            resolve_official_registrations(rows, {}, {})


def obinaya_sources():
    team = "Bohemians 1905"
    rows = [
        {
            **official(team, "4974", "Samuel Obinaja"),
            "dateOfBirth": "28.08.2005", "shirtNumber": 62,
        },
        {
            **official(team, "5245", "Samuel Obinaya"),
            "dateOfBirth": "28.08.2005", "shirtNumber": 62,
            "heightCm": 184, "weightKg": 76,
        },
    ]
    tm = {
        team: {"players": [{
            "team": team, "name": "Samuel Obinaya", "position": "M",
            "transfermarktPlayerId": "1237999",
            "transfermarktUrl": "https://www.transfermarkt.com/samuel-obinaya/profil/spieler/1237999",
            "transfermarktSquadUrl": "https://www.transfermarkt.com/bohemians/kader/verein/715",
            "marketValueEur": 250000, "mv": "€250k",
        }]},
    }
    live = {
        team: {"players": [{
            "team": team, "name": "Obinaya Samuel", "position": "M",
            "livesportPlayerId": "zHgOo00h", "shirtNumber": 62,
        }]},
    }
    return rows, tm, live


class OfficialNameAliasTests(unittest.TestCase):
    def test_obinaya_alias_resolved_independently_of_row_order(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                rows, tm, live = obinaya_sources()
                if reverse:
                    rows.reverse()
                original = copy.deepcopy(rows)
                resolved, ignored = resolve_official_registrations(rows, tm, live)
                self.assertEqual(len(resolved), 1)
                self.assertEqual(resolved[0]["chanceLigaPlayerId"], "5245")
                self.assertEqual(resolved[0]["heightCm"], 184)
                self.assertEqual([row["chanceLigaPlayerId"] for row in ignored], ["4974"])
                self.assertIn("Transfermarkt 1237999 + Livesport zHgOo00h", ignored[0]["reason"])
                self.assertEqual(rows, original)

    def test_conflicting_or_missing_biography_fails_closed(self):
        for field, value in (
            ("dateOfBirth", "28.08.2004"), ("dateOfBirth", None),
            ("shirtNumber", 16), ("shirtNumber", None),
        ):
            with self.subTest(field=field, value=value):
                rows, tm, live = obinaya_sources()
                rows[0][field] = value
                with self.assertRaisesRegex(RuntimeError, "conflicting or incomplete identity evidence"):
                    resolve_official_registrations(rows, tm, live)

    def test_missing_or_conflicting_livesport_shirt_fails_closed(self):
        for shirt in (None, 16):
            with self.subTest(shirt=shirt):
                rows, tm, live = obinaya_sources()
                live["Bohemians 1905"]["players"][0]["shirtNumber"] = shirt
                with self.assertRaisesRegex(RuntimeError, "Cannot resolve official name aliases"):
                    resolve_official_registrations(rows, tm, live)

    def test_missing_independent_source_does_not_merge_similar_names(self):
        for missing_source in ("tm", "livesport"):
            with self.subTest(missing_source=missing_source):
                rows, tm, live = obinaya_sources()
                resolved, ignored = resolve_official_registrations(
                    rows, {} if missing_source == "tm" else tm,
                    {} if missing_source == "livesport" else live,
                )
                self.assertEqual(len(resolved), 2)
                self.assertEqual(ignored, [])

    def test_independent_sources_disagreeing_on_name_do_not_merge(self):
        rows, tm, live = obinaya_sources()
        live["Bohemians 1905"]["players"][0]["name"] = "Obinaja Samuel"
        resolved, ignored = resolve_official_registrations(rows, tm, live)
        self.assertEqual(len(resolved), 2)
        self.assertEqual(ignored, [])

    def test_ambiguous_source_identity_is_not_evidence_for_a_merge(self):
        rows, tm, live = obinaya_sources()
        players = live["Bohemians 1905"]["players"]
        players.append({**players[0], "livesportPlayerId": "another-person"})
        resolved, ignored = resolve_official_registrations(rows, tm, live)
        self.assertEqual(len(resolved), 2)
        self.assertEqual(ignored, [])

    def test_two_real_source_players_are_not_collapsed(self):
        rows, tm, live = obinaya_sources()
        for clubs, field, player_id, name in (
            (tm, "transfermarktPlayerId", "other-tm-id", "Samuel Obinaja"),
            (live, "livesportPlayerId", "other-live-id", "Obinaja Samuel"),
        ):
            players = clubs["Bohemians 1905"]["players"]
            players.append({**players[0], field: player_id, "name": name})
        resolved, ignored = resolve_official_registrations(rows, tm, live)
        self.assertEqual(len(resolved), 2)
        self.assertEqual(ignored, [])

    def test_aliases_without_one_exact_official_spelling_fail_closed(self):
        rows, tm, live = obinaya_sources()
        rows[1]["name"] = "Samuel Obinayi"
        with self.assertRaisesRegex(RuntimeError, "Cannot resolve official name aliases"):
            resolve_official_registrations(rows, tm, live)

    def test_missing_measurements_are_preserved_from_proven_alias(self):
        rows, tm, live = obinaya_sources()
        rows[0]["heightCm"] = rows[1].pop("heightCm")
        resolved, _ = resolve_official_registrations(rows, tm, live)
        self.assertEqual(resolved[0]["heightCm"], 184)

    def test_reconciliation_preserves_history_and_never_creates_second_player(self):
        rows, tm, live = obinaya_sources()
        team = "Bohemians 1905"
        donor = {
            "name": "Obinaya Samuel", "team": team, "pos": "M",
            "chanceLigaPlayerId": "5245", "transfermarktPlayerId": "1237999",
            "livesportPlayerId": "zHgOo00h",
            "matches": [{"livesportMatchId": "O6WhCbS1", "mins": 9}],
            "career": [{"season": "2025/26", "team": "Jablonec", "matches": 7}],
            "userNote": "Keep this note",
        }
        original = copy.deepcopy(donor)
        with patch("update_rosters.search_transfermarkt") as search:
            players, ignored = reconcile(
                {team: {"sourceUrl": "https://www.chanceliga.cz/klub/20-bohemians-praha-1905", "players": rows}},
                tm, live, [donor], "2026-09-18T12:00:00Z",
            )
        search.assert_not_called()
        self.assertEqual(len(players), 1)
        self.assertEqual(players[0]["chanceLigaPlayerId"], "5245")
        for field in ("matches", "career", "livesportPlayerId", "userNote"):
            self.assertEqual(players[0][field], donor[field])
        self.assertIsNot(players[0]["matches"], donor["matches"])
        self.assertEqual(donor, original)
        self.assertEqual(len(ignored), 1)

    def test_conflicting_alias_cannot_replace_published_files(self):
        rows, tm, live = obinaya_sources()
        rows[0]["dateOfBirth"] = "28.08.2004"
        team = "Bohemians 1905"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "rosters-live.json"
            audit = Path(directory) / "roster-audit-live.json"
            output.write_text(json.dumps({"players": []}), encoding="utf-8")
            audit.write_text('{"previous":"verified audit"}', encoding="utf-8")
            original = (output.read_bytes(), audit.read_bytes())
            with (
                patch.object(sys, "argv", ["updater", "--output", str(output), "--audit-output", str(audit)]),
                patch.object(updater, "TEAM_CONFIG", [(team, "/club", 715, "bohemians")]),
                patch.object(updater, "scrape_official", return_value={"players": rows}),
                patch.object(updater, "scrape_transfermarkt", return_value=tm[team]),
                patch.object(updater, "scrape_livesport", return_value=live[team]),
                patch.object(updater.os, "replace") as replace,
            ):
                with self.assertRaisesRegex(RuntimeError, "Cannot resolve official name aliases"):
                    updater.main()
            replace.assert_not_called()
            self.assertEqual((output.read_bytes(), audit.read_bytes()), original)


if __name__ == "__main__":
    unittest.main()
