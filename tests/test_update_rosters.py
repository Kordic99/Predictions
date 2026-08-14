import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from update_rosters import TEAM_ORDER, resolve_official_registrations


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


if __name__ == "__main__":
    unittest.main()
