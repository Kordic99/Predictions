import json
import sys
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import MagicMock, call, patch
from urllib.error import HTTPError, URLError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import update_livesport_match_stats as stats
import update_livesport_rosters as updater


def response(status=200, payload=None, headers=None):
    result = MagicMock()
    result.status = status
    result.headers = headers or {}
    result.read.return_value = json.dumps(payload or {"data": {"ready": True}}).encode()
    result.__enter__.return_value = result
    return result


def http_error(code=404, headers=None):
    return HTTPError(stats.GRAPHQL_ROOT, code, "Not Found", headers, None)


class GraphqlRetryTests(unittest.TestCase):
    params = {"eventId": "Sbuhp0Ok", "_hash": "epmsse", "projectId": "1"}

    def setUp(self):
        sleeper = patch.object(stats.time, "sleep")
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)
        printer = patch("builtins.print")
        self.printer = printer.start()
        self.addCleanup(printer.stop)

    def test_delayed_404_recovers_with_increasing_waits(self):
        with patch.object(
            stats.urllib.request, "urlopen",
            side_effect=[http_error() for _ in range(5)] + [response()],
        ) as request:
            payload = stats.graphql_payload(self.params, "https://www.livesport.cz/")

        self.assertEqual(payload, {"data": {"ready": True}})
        self.assertEqual(request.call_count, 6)
        self.assertEqual(self.sleep.call_args_list, [call(n) for n in (5, 10, 20, 40, 60)])
        self.assertTrue(any("recovered on attempt 6/6" in str(c) for c in self.printer.call_args_list))

    def test_persistent_404_has_bounded_retries_and_precise_final_error(self):
        with patch.object(stats.urllib.request, "urlopen", side_effect=http_error()) as request:
            with self.assertRaisesRegex(
                RuntimeError, r"Sbuhp0Ok \(query epmsse\) after 6 attempts: HTTP Error 404"
            ):
                stats.graphql_payload(self.params, "https://www.livesport.cz/")

        self.assertEqual(request.call_count, 6)
        self.assertEqual(self.sleep.call_count, 5)

    def test_rate_limit_respects_retry_after(self):
        headers = Message()
        headers["Retry-After"] = "45"
        with patch.object(stats.urllib.request, "urlopen", side_effect=[http_error(429, headers), response()]):
            stats.graphql_payload(self.params, "https://www.livesport.cz/")
        self.sleep.assert_called_once_with(45)

    def test_malformed_retry_after_uses_normal_backoff(self):
        headers = Message()
        headers["Retry-After"] = "not a date or number"
        with patch.object(stats.urllib.request, "urlopen", side_effect=[http_error(503, headers), response()]):
            stats.graphql_payload(self.params, "https://www.livesport.cz/")
        self.sleep.assert_called_once_with(5)

    def test_long_server_wait_does_not_retry_early(self):
        headers = Message()
        headers["Retry-After"] = "120"
        with patch.object(stats.urllib.request, "urlopen", side_effect=http_error(429, headers)) as request:
            with self.assertRaisesRegex(RuntimeError, "server requests a 120s wait"):
                stats.graphql_payload(self.params, "https://www.livesport.cz/")
        self.assertEqual(request.call_count, 1)
        self.sleep.assert_not_called()

    def test_accepted_but_not_ready_is_retried_with_server_hint(self):
        pending = response(status=202, headers={"x-retry-after-ms": "12000"})
        with patch.object(stats.urllib.request, "urlopen", side_effect=[pending, response()]):
            stats.graphql_payload(self.params, "https://www.livesport.cz/")
        self.sleep.assert_called_once_with(12)

    def test_persistent_not_ready_has_an_explicit_error(self):
        with patch.object(stats.urllib.request, "urlopen", return_value=response(status=202)):
            with self.assertRaisesRegex(RuntimeError, "after 2 attempts: HTTP Error 202"):
                stats.graphql_payload(self.params, "https://www.livesport.cz/", attempts=2)
        self.sleep.assert_called_once_with(5)

    def test_transient_connection_error_is_retried(self):
        with patch.object(stats.urllib.request, "urlopen", side_effect=[URLError("connection reset"), response()]):
            stats.graphql_payload(self.params, "https://www.livesport.cz/")
        self.sleep.assert_called_once_with(5)

    def test_permanent_http_error_does_not_consume_all_retries(self):
        with patch.object(stats.urllib.request, "urlopen", side_effect=http_error(403)) as request:
            with self.assertRaisesRegex(RuntimeError, "HTTP Error 403.*not retryable"):
                stats.graphql_payload(self.params, "https://www.livesport.cz/")
        self.assertEqual(request.call_count, 1)
        self.sleep.assert_not_called()

    def test_persistent_outage_cannot_replace_published_files(self):
        # Exercise the real updater entry point and real GraphQL retry code.
        # Roster reconciliation changes the in-memory data first; a failed
        # match request must still leave both previously published files intact.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output, audit, schedule = [root / name for name in ("rosters.json", "audit.json", "schedule.json")]
            previous = {"players": [{"name": "Existing player", "team": "Home", "matches": []}]}
            output.write_text(json.dumps(previous), encoding="utf-8")
            audit.write_text('{"previousAudit":true}', encoding="utf-8")
            schedule.write_text('{"matches":[]}', encoding="utf-8")
            original_bytes = (output.read_bytes(), audit.read_bytes())
            fixture = {
                "livesportMatchId": "Sbuhp0Ok", "home": "Home", "away": "Away",
                "sourceUrl": "https://www.livesport.cz/",
            }
            with (
                patch.object(sys, "argv", ["updater", "--output", str(output), "--audit-output", str(audit), "--schedule", str(schedule), "--refresh-all-matches"]),
                patch.object(updater, "LIVESPORT_TEAM_CONFIG", {"Home": ("home", "team-id")}),
                patch.object(updater, "scrape_livesport", return_value={}),
                patch.object(updater, "attach_livesport", return_value=([{"name": "New player", "matches": []}], {})),
                patch.object(updater, "discover_livesport_events", return_value=[fixture]),
                patch.object(stats.urllib.request, "urlopen", side_effect=http_error()),
                patch.object(updater, "atomic_json") as writer,
            ):
                with self.assertRaisesRegex(RuntimeError, "after 6 attempts: HTTP Error 404"):
                    updater.main()
            writer.assert_not_called()
            self.assertEqual((output.read_bytes(), audit.read_bytes()), original_bytes)


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

    def test_second_yellow_is_not_counted_as_two_red_cards(self):
        self.assertEqual(
            stats.red_card_total(
                {"CARDS_RED": "1", "CARDS_YELLOW_SECOND": "1"}
            ),
            1,
        )

    def test_straight_red_is_preserved(self):
        self.assertEqual(
            stats.red_card_total(
                {"CARDS_RED": "1", "CARDS_YELLOW_SECOND": "0"}
            ),
            1,
        )

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

    def test_stored_double_counted_red_card_is_forced_to_refresh(self):
        players = []
        for index, performance in enumerate(complete_rows()):
            match = dict(performance["match"])
            match.update(
                {
                    "livesportMatchId": "event",
                    "livesportPlayerId": f"player-{index}",
                    "importVersion": stats.IMPORT_VERSION,
                    "redCards": 0,
                }
            )
            players.append(
                {
                    "name": f"Player {index}",
                    "livesportPlayerId": f"player-{index}",
                    "matches": [match],
                }
            )
        players[0]["matches"][0]["redCards"] = 2
        fixture = {**self.fixture, "timestamp": 1}
        self.assertTrue(stats.should_refresh_fixture(players, fixture, False))


if __name__ == "__main__":
    unittest.main()
