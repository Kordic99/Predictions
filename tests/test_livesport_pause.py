import runpy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import livesport_policy as policy
import update_rosters as rosters
import update_match_context as context
import update_livesport_match_stats as stats
import update_schedule as schedule


class LivesportPauseTests(unittest.TestCase):
    def test_http_boundaries_reject_before_any_request(self):
        self.assertTrue(policy.LIVESPORT_IMPORTS_PAUSED)
        with patch("urllib.request.urlopen") as network:
            for fetch in (rosters.fetch_bytes, context.fetch_text, stats.fetch_text):
                for url in ("https://www.livesport.cz/", "https://1.ds.lsapp.eu/pq_graphql",
                            "https://www.flashscore.com/", "https://www.livesport.eu/"):
                    with self.subTest(fetch=fetch.__module__, url=url):
                        with self.assertRaisesRegex(RuntimeError, "paused by owner"):
                            fetch(url)
            with self.assertRaisesRegex(RuntimeError, "paused by owner"):
                stats.graphql_payload({"eventId": "test"}, "https://www.livesport.cz/")
            network.assert_not_called()

    def test_every_web_source_is_now_blocked_before_network(self):
        with patch("urllib.request.urlopen") as network, patch.object(schedule, "urlopen") as schedule_network:
            for url in ("https://www.chanceliga.cz/rozpis-zapasu", "https://www.transfermarkt.com/",
                        "https://tmapi.transfermarkt.technology/", "https://www.slavia.cz/",
                        "https://kordic99.github.io/Predictions/rosters-live.json"):
                for fetch in (rosters.fetch_bytes, context.fetch_text, stats.fetch_text):
                    with self.subTest(url=url, fetch=fetch.__module__):
                        with self.assertRaisesRegex(RuntimeError, "paused by owner"):
                            fetch(url)
            with self.assertRaisesRegex(RuntimeError, "paused by owner"):
                schedule.fetch_source()
            network.assert_not_called()
            schedule_network.assert_not_called()

    def test_cli_entrypoints_stop_before_network_writes_or_child_processes(self):
        files = list(ROOT.glob("*.json")) + list((ROOT / "data/predictions").glob("*.json"))
        before = {path: path.read_bytes() for path in files}
        with patch("urllib.request.urlopen") as network, patch("subprocess.run") as child:
            for name in ("update_rosters.py", "update_livesport_rosters.py", "update_match_context.py",
                         "update_prediction_evidence.py", "prepare_weekend_predictions.py", "update_schedule.py"):
                with self.subTest(script=name):
                    with self.assertRaisesRegex(RuntimeError, "paused by owner"):
                        runpy.run_path(str(ROOT / "scripts" / name), run_name="__main__")
            network.assert_not_called()
            child.assert_not_called()
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content, str(path))


if __name__ == "__main__":
    unittest.main()
