import copy
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
                "round": 1, "score": "0:0",
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

    def test_only_transport_errors_are_classified_as_source_unavailable(self):
        for error in (http_error(404), http_error(503), URLError("connection reset")):
            with self.subTest(error=error), patch.object(stats.urllib.request, "urlopen", side_effect=error):
                with self.assertRaises(stats.LivesportSourceUnavailable):
                    stats.graphql_payload(self.params, "https://www.livesport.cz/", attempts=1)
        bad_json = response()
        bad_json.read.return_value = b"not JSON"
        for bad in (bad_json, response(payload={"errors": [{"message": "Unknown query"}]})):
            with self.subTest(response=bad), patch.object(stats.urllib.request, "urlopen", return_value=bad):
                with self.assertRaises(RuntimeError) as caught:
                    stats.graphql_payload(self.params, "https://www.livesport.cz/", attempts=1)
                self.assertNotIsInstance(caught.exception, stats.LivesportSourceUnavailable)


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


def fixture(event_id, home="Home", away="Away", round_number=1):
    return {
        "livesportMatchId": event_id, "home": home, "away": away,
        "homeGoals": 0, "awayGoals": 0, "score": "0:0", "round": round_number,
        "season": stats.SEASON, "timestamp": 1, "date": "2026-07-25",
        "sourceUrl": f"https://www.livesport.cz/zapas/{event_id}/",
    }


def performances(game):
    rows = []
    for team in (game["home"], game["away"]):
        for index in range(11):
            player_id = f"{team}-{index}"
            rows.append({
                "livesportPlayerId": player_id, "sourceName": player_id,
                "match": {
                    "livesportMatchId": game["livesportMatchId"],
                    "livesportPlayerId": player_id, "team": team,
                    "opponent": game["away"] if team == game["home"] else game["home"],
                    "season": stats.SEASON, "round": game["round"],
                    "competition": "Chance Liga", "date": game["date"],
                    "mins": 90, "form": 7.0, "goals": 0, "ownGoals": 0,
                    "assists": 0, "yellowCards": 0, "redCards": 0,
                    "starter": True, "importVersion": stats.IMPORT_VERSION,
                    "stats": {"goalkeeper": {} if index == 0 else None},
                },
            })
    return rows


def roster_for(game, *, stored=False):
    return [{
        "name": row["sourceName"], "team": row["match"]["team"], "pos": "M",
        "livesportPlayerId": row["livesportPlayerId"],
        "matches": [copy.deepcopy(row["match"])] if stored else [],
        "livesportSeasonStats": {
            "season": stats.SEASON, "apps": 2, "minutes": 180,
            "goals": 0, "assists": 0, "yellowCards": 0, "redCards": 0,
            "source": "Livesport team roster", "rating": 7.0,
        },
        "career": [{"season": stats.SEASON, "competition": "Chance Liga",
                    "team": row["match"]["team"], "matches": 2, "minutes": 180}],
    } for row in performances(game)]


class DeferredMatchTests(unittest.TestCase):
    def setUp(self):
        self.old = fixture("old")
        self.pending = fixture("unavailable", round_number=2)
        self.good = fixture("good", "Third", "Fourth")
        self.players = roster_for(self.old, stored=True) + roster_for(self.good)
        self.original = copy.deepcopy(self.players)

    def load(self, game):
        if game["livesportMatchId"] == "unavailable":
            raise stats.LivesportSourceUnavailable("HTTP Error 404: Not Found")
        return performances(game)

    def test_missing_match_does_not_block_other_matches_or_zero_season_totals(self):
        with patch.object(stats, "load_match_performances", side_effect=self.load):
            players, report = stats.apply_match_performances(
                self.players, [self.old, self.pending, self.good], refresh_all=True
            )
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["completedMatches"], 3)
        self.assertEqual(report["availableMatches"], 2)
        self.assertEqual(report["pendingMatches"][0]["retainedPlayerPerformances"], 0)
        self.assertEqual(report["validation"]["pendingEventIds"], ["unavailable"])
        affected = players[0]
        self.assertEqual(affected["matches"], self.original[0]["matches"])
        self.assertEqual(affected["livesportSeasonStats"]["apps"], 2)
        self.assertEqual(affected["livesportSeasonStats"]["minutes"], 180)
        self.assertEqual(affected["career"][0]["matches"], 2)
        self.assertEqual(affected["livesportSeasonStats"]["ratingPendingEventIds"], ["unavailable"])
        self.assertEqual(affected["livesportSeasonStats"]["ratedApps"], 1)
        self.assertEqual(len(players[-1]["matches"]), 1)
        self.assertNotIn("ratingPendingEventIds", players[-1]["livesportSeasonStats"])
        self.assertEqual(self.players, self.original)

    def test_cached_match_is_retained_and_still_validated_during_outage(self):
        cached = roster_for(self.pending, stored=True) + roster_for(self.good)
        with patch.object(stats, "load_match_performances", side_effect=self.load):
            players, report = stats.apply_match_performances(cached, [self.pending, self.good], True)
        self.assertEqual(report["pendingMatches"][0]["retainedPlayerPerformances"], 22)
        self.assertEqual(players[0]["matches"], cached[0]["matches"])
        for invalid in ("rating", "score", "partial"):
            broken = copy.deepcopy(cached)
            if invalid == "rating":
                broken[0]["matches"][0]["form"] = 99
            elif invalid == "score":
                broken[0]["matches"][0]["goals"] = 1
            else:
                broken[0]["matches"] = []
            with self.subTest(invalid=invalid), patch.object(stats, "load_match_performances", side_effect=self.load):
                with self.assertRaisesRegex(RuntimeError, "match-stat validation failed"):
                    stats.apply_match_performances(broken, [self.pending, self.good], True)

    def test_missing_data_without_a_transport_failure_is_still_an_error(self):
        with self.assertRaisesRegex(RuntimeError, "incomplete detailed match rows"):
            stats.validate_match_details(self.players, [self.pending])

    def test_parser_or_validation_failure_is_not_silently_deferred(self):
        with patch.object(stats, "load_match_performances", side_effect=RuntimeError("bad scorer total")):
            with self.assertRaisesRegex(RuntimeError, "bad scorer total"):
                stats.apply_match_performances(self.players, [self.good], True)

    def test_retry_recovers_old_pending_match_and_removes_partial_markers(self):
        with patch.object(stats, "load_match_performances", side_effect=self.load):
            players, _ = stats.apply_match_performances(self.players, [self.old, self.pending, self.good], True)
        with patch.object(stats, "should_refresh_fixture", return_value=False), patch.object(
            stats, "load_match_performances", side_effect=performances
        ) as loader:
            recovered, report = stats.apply_match_performances(
                players, [self.old, self.pending, self.good], retry_event_ids={"unavailable"}
            )
        loader.assert_called_once_with(self.pending)
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["pendingMatches"], [])
        self.assertEqual(report["availableMatches"], 3)
        self.assertEqual(len(recovered[0]["matches"]), 2)
        self.assertNotIn("ratingPendingEventIds", recovered[0]["livesportSeasonStats"])
        self.assertNotIn("ratingPendingEventIds", recovered[0]["career"][0])

    def test_broad_outage_stops_with_a_bounded_number_of_matches(self):
        games = [fixture(str(i)) for i in range(5)]
        with patch.object(stats, "load_match_performances", side_effect=stats.LivesportSourceUnavailable("503")) as loader:
            with self.assertRaisesRegex(RuntimeError, "3 consecutive matches"):
                stats.apply_match_performances(self.players, games, True)
        self.assertEqual(loader.call_count, 3)
        self.assertEqual(self.players, self.original)

    def test_pending_status_is_published_even_without_player_changes_then_cleared(self):
        for previous_pending, next_pending, writes_expected in (
            ([], [{**self.pending, "reason": "404", "retainedPlayerPerformances": 0}], True),
            ([{**self.pending, "firstUnavailableAt": "earlier", "reason": "404"}], [], True),
            (
                [{**self.pending, "firstUnavailableAt": "earlier", "reason": "404"}],
                [{**self.pending, "reason": "404"}], False,
            ),
        ):
            with self.subTest(next_pending=next_pending), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                output, audit, schedule = [root / name for name in ("roster.json", "audit.json", "schedule.json")]
                one_player = self.players[:1]
                output.write_text(json.dumps({"players": one_player, "livesportMatchStats": {"pendingMatches": previous_pending}}), encoding="utf-8")
                schedule.write_text('{"matches":[]}', encoding="utf-8")
                report = {
                    "pendingMatches": next_pending, "validation": {"reconciliationIssues": []},
                    "completedMatches": 1, "availableMatches": 0,
                    "playerPerformances": 0, "ratedPerformances": 0,
                }
                with (
                    patch.object(sys, "argv", ["updater", "--output", str(output), "--audit-output", str(audit), "--schedule", str(schedule)]),
                    patch.object(updater, "LIVESPORT_TEAM_CONFIG", {}),
                    patch.object(updater, "TEAM_ORDER", ["Home"]),
                    patch.object(updater, "attach_livesport", return_value=(one_player, {})),
                    patch.object(updater, "discover_livesport_events", return_value=[]),
                    patch.object(updater, "apply_match_performances", return_value=(one_player, report)) as apply,
                    patch.object(updater, "validate", return_value={"playerCount": 1, "activeLivesportPlayers": 1, "activeLivesportScorers": 0}),
                    patch("builtins.print"),
                ):
                    updater.main()
                saved = json.loads(output.read_text(encoding="utf-8"))
                self.assertEqual(saved["livesportMatchStats"]["pendingMatches"], next_pending)
                self.assertEqual(apply.call_args.kwargs["retry_event_ids"], {row["livesportMatchId"] for row in previous_pending})
                self.assertEqual(audit.exists(), writes_expected)
                if next_pending:
                    self.assertTrue(next_pending[0]["firstUnavailableAt"])
                    if previous_pending:
                        self.assertEqual(next_pending[0]["firstUnavailableAt"], "earlier")


if __name__ == "__main__":
    unittest.main()
