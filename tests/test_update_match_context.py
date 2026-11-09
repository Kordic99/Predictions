import sys
from pathlib import Path
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import update_match_context as context


class StrictContextTests(unittest.TestCase):
    def test_strict_fixture_refresh_never_silently_reuses_old_context(self):
        row = {'officialMatchId': '123', 'date': datetime.now(timezone.utc).date().isoformat(),
               'round': 1, 'home': 'test-home', 'away': 'test-away'}
        previous = {'fixtures': [row]}
        with patch.object(context, 'build_fixture_context', side_effect=RuntimeError('upstream failed')):
            self.assertEqual(context.build_fixtures({'matches': [row]}, previous, 10), [row])
            with self.assertRaisesRegex(RuntimeError, 'upstream failed'):
                context.build_fixtures({'matches': [row]}, previous, 10, strict=True)

    def test_strict_preseason_refresh_does_not_bless_stale_data(self):
        schedule = {'matches': [{'date': '2026-07-25'}]}
        previous = {'preseason': {'teams': {team: {} for team in context.TEAM_SEGMENTS}}}
        with patch.object(context, 'fetch_text', side_effect=RuntimeError('upstream failed')):
            with self.assertRaisesRegex(RuntimeError, 'upstream failed'):
                context.build_preseason(schedule, previous, strict=True)


if __name__ == '__main__':
    unittest.main()
