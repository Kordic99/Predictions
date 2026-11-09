import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import update_prediction_evidence as e

class EvidenceTest(unittest.TestCase):
    now = datetime(2026, 11, 13, 7, tzinfo=timezone.utc)

    def test_structured_news_is_informational_and_future_dates_excluded(self):
        source = '<script type="application/ld+json">{"@type":"NewsArticle","headline":"Hráč není zraněný","datePublished":"2026-11-12T10:00:00+01:00"}</script>'
        rows = e.structured_news(source, 'https://club.example/article/1', self.now)
        self.assertEqual(rows[0]['effect'], 0)
        self.assertEqual(rows[0]['publishedAt'], '2026-11-12T09:00:00Z')
        self.assertEqual(e.structured_news(source.replace('2026-11-12', '2026-11-14'), 'https://club.example/article/1', self.now), [])

    def test_only_finished_scores_and_scheduled_fixtures_are_used(self):
        t = int(datetime(2026, 11, 12, 18, tzinfo=timezone.utc).timestamp())
        raw = f'~ZA÷Evropa¬ZK÷Evropská liga¬~AA÷event1¬AD÷{t}¬AB÷3¬AC÷3¬AO÷{t+7200}¬PX÷6qA358jH¬PY÷other¬AE÷Sparta¬AF÷Other¬AG÷2¬AH÷0¬'
        source = 'cjs.initialFeeds["results"] = { data: `'+raw+'`'
        rows = e.feed_events(source, 'results', 'Sparta Prague', self.now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'completed')
        self.assertEqual(e.feed_events(source.replace('AB÷3', 'AB÷2'), 'results', 'Sparta Prague', self.now), [])
        self.assertEqual(e.feed_events(source.replace(f'AO÷{t+7200}', f'AO÷{t+200000}'), 'results', 'Sparta Prague', self.now), [])

    def test_missing_feed_is_not_reported_as_empty_verified_schedule(self):
        with self.assertRaises(RuntimeError): e.feed_events('<html></html>', 'fixtures', 'Sparta Prague', self.now)

    def test_external_news_urls_not_trusted(self):
        source = '<script type="application/ld+json">{"@type":"NewsArticle","headline":"x","url":"https://other.example/1","datePublished":"2026-11-12T10:00:00Z"}</script>'
        self.assertEqual(e.structured_news(source, 'https://club.example/', self.now), [])

    def test_date_only_publication_uses_end_of_prague_day_not_fake_midnight(self):
        source = '<div><time datetime="2026-11-12">12. 11.</time><h1>Oficiální článek</h1></div>'
        rows = e.structured_news(source, 'https://club.example/article/1', self.now)
        self.assertEqual(rows[0]['publishedAt'], '2026-11-12T22:59:59Z')
        self.assertEqual(e.structured_news(source.replace('2026-11-12', '2026-11-13'), 'https://club.example/article/1', self.now), [])

    def test_rss_xml_declaration_and_safe_entity_parser(self):
        source = '<?xml version="1.0" encoding="UTF-8"?><rss><channel><item><title>Novinka</title><link>https://club.example/news/1</link><pubDate>Thu, 12 Nov 2026 10:00:00 +0100</pubDate></item></channel></rss>'
        self.assertEqual(e.structured_news(source, 'https://club.example/rss', self.now)[0]['effect'], 0)

if __name__ == '__main__': unittest.main()
