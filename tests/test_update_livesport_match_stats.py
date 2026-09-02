import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import update_livesport_match_stats as stats


def row(team: str, starter: bool, goalkeeper: bool = False) -> dict:
    return {
        "livesportPlayerId": f"{team}-{starter}-{goalkeeper}",
        "match": {
            "team": team,
            "starter": starter,
            "stats": {"goalkeeper": {} if goalkeeper else None},
        },
    }


def complete_rows() -> list[dict]:
    rows = []
    for team in ("Home", "Away"):
        rows.extend(row(team, True, goalkeeper=index == 0) for index in range(11))
        rows.extend(row(team, False) for _ in range(5))
    return rows


class PerformanceSnapshotTests(unittest.TestCase):
    fixture = {"livesportMatchId": "event", "home": "Home", "away": "Away"}

    def test_complete_snapshot_is_accepted(self):
        self.assertEqual(
            stats.performance_snapshot_errors(complete_rows(), self.fixture), []
        )

    def test_partial_starter_snapshot_is_retried(self):
        incomplete = complete_rows()
        incomplete[1]["match"]["starter"] = False
        with patch.object(
            stats,
            "_load_match_performances_once",
            side_effect=[incomplete, complete_rows()],
        ) as loader, patch.object(stats.time, "sleep"):
            result = stats.load_match_performances(self.fixture, attempts=2)
        self.assertEqual(len(result), 32)
        self.assertEqual(loader.call_count, 2)

    def test_persistent_partial_snapshot_fails_without_publishing(self):
        incomplete = complete_rows()
        incomplete[1]["match"]["starter"] = False
        with patch.object(
            stats, "_load_match_performances_once", return_value=incomplete
        ), patch.object(stats.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "starter count 10"):
                stats.load_match_performances(self.fixture, attempts=2)

    def test_stored_historical_snapshot_is_not_refetched_only_for_roster_coverage(self):
        players = []
        for index, performance in enumerate(complete_rows()):
            match = dict(performance["match"])
            match.update(
                {
                    "livesportMatchId": "event",
                    "livesportPlayerId": f"player-{index}",
                    "importVersion": stats.IMPORT_VERSION,
                }
            )
            players.append(
                {
                    "name": f"Player {index}",
                    "livesportPlayerId": f"player-{index}",
                    "matches": [match],
                }
            )
        players[1]["matches"][0]["starter"] = False
        fixture = {
            **self.fixture,
            "timestamp": 1,
        }
        self.assertFalse(stats.should_refresh_fixture(players, fixture, False))


if __name__ == "__main__":
    unittest.main()
