import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from update_schedule import parse_schedule_html, validate


def schedule_source(date_text: str, time_text: str = "-") -> str:
    return f"""
    <table>
      <tr class="header"><td>5. kolo</td></tr>
      <tr class="game">
        <td class="date">{date_text}</td>
        <td class="time hidden-xs">{time_text}</td>
        <td class="team home"><span class="hidden-xs">FK Jablonec</span></td>
        <td class="score"><a href="/zapas/8362-fkj-fcb">-:-</a></td>
        <td class="team away"><span class="hidden-xs">FC Baník Ostrava</span></td>
      </tr>
    </table>
    """


class ScheduleParserTests(unittest.TestCase):
    def test_postponed_fixture_is_preserved_as_one_match(self):
        matches = parse_schedule_html(schedule_source("odloženo"))

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["officialMatchId"], "8362")
        self.assertIsNone(matches[0]["date"])
        self.assertIsNone(matches[0]["time"])
        self.assertEqual(matches[0]["status"], "postponed")

    def test_scheduled_fixture_keeps_date_and_time(self):
        matches = parse_schedule_html(
            schedule_source("ne, 23/08/2026", "15:00")
        )

        self.assertEqual(matches[0]["date"], "2026-08-23")
        self.assertEqual(matches[0]["time"], "15:00")
        self.assertNotIn("status", matches[0])

    def test_unknown_date_format_fails_loudly(self):
        with self.assertRaisesRegex(RuntimeError, "8362.*unsupported official date"):
            parse_schedule_html(schedule_source("čeká se na rozhodnutí"))

    def test_validation_reports_the_incomplete_round(self):
        matches = parse_schedule_html(schedule_source("odloženo"))

        with self.assertRaisesRegex(RuntimeError, "round counts: 1=0.*5=1"):
            validate(matches)


if __name__ == "__main__":
    unittest.main()
