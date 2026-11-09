"""Free public facts for prospective predictions. Never infer fitness from article text.

Structured dates and sources are retained; inaccessible/undated news is not evidence.
Official social links are exposed for inspection, not claimed to be crawled or complete.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo
from lxml import html, etree
import update_match_context as context

ROOT = Path(__file__).resolve().parents[1]

def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')

def date(value):
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(value)):
        # A date without a time is only safely before the cutoff after that day ends.
        return datetime.fromisoformat(str(value)+'T23:59:59').replace(tzinfo=ZoneInfo('Europe/Prague')).astimezone(timezone.utc)
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        try:
            result = parsedate_to_datetime(str(value))
        except (ValueError, TypeError):
            return None
    # Date-only/local dates are not safe publication timestamps.
    return result.astimezone(timezone.utc) if result.tzinfo else None

def feed_events(source, name, team, now):
    raw = context.initial_feed(source, name)
    states = {}
    for block in raw.split('~'):
        fields = dict(token.split('÷', 1) for token in block.split('¬') if '÷' in token)
        if 'AA' in fields:
            states[fields['AA']] = fields
    output = []
    for row in context.parse_feed_matches(raw):
        f = states[row['livesportMatchId']]
        t = datetime.fromtimestamp(row['timestamp'], timezone.utc)
        if not now-timedelta(days=21) <= t <= now+timedelta(days=10):
            continue
        if context.team_participant_id(team) not in (row['homeParticipantId'], row['awayParticipantId']):
            continue
        ended = f.get('AB') == '3' and f.get('AC') in ('3', '10', '11') and 'score' in row
        scheduled = f.get('AB') == '1' and f.get('AC') == '1'
        if not ended and not scheduled:
            continue
        completed = datetime.fromtimestamp(int(f.get('AO') or 0), timezone.utc) if ended else None
        if ended and (not t < completed <= now):
            continue
        if scheduled and t <= now:
            continue
        canonical = {context.team_participant_id(name): name for name in context.TEAM_SEGMENTS}
        output.append({'id': row['livesportMatchId'], 'home': canonical.get(row['homeParticipantId'], row['home']), 'away': canonical.get(row['awayParticipantId'], row['away']),
                       'competition': row['competition'], 'kickoffAt': iso(t),
                       'completedAt': iso(completed) if completed else None,
                       'status': 'completed' if ended else 'scheduled', 'score': row.get('score') if ended else None,
                       'sourceUrl': 'https://www.livesport.cz/zapas/'+row['livesportMatchId']+'/', 'fetchedAt': iso(now)})
    return output

def structured_news(source, url, now):
    """Extract titles/links only, not copyrighted article bodies or NLP conclusions."""
    items = []
    def add(title, link, published):
        when = date(published)
        link = urljoin(url, str(link or url))
        if when and now-timedelta(days=7) <= when <= now and same_host(link, url):
            items.append({'title': ' '.join(str(title or '').split()[:20])[:200], 'sourceUrl': link, 'publishedAt': iso(when),
                          'fetchedAt': iso(now), 'effect': 0, 'classification': 'informational-not-interpreted'})
    doc = html.fromstring(source.encode('utf-8'))
    def walk(obj):
        if isinstance(obj, list):
            for value in obj: walk(value)
        elif isinstance(obj, dict):
            typ = obj.get('@type', '')
            if any(t in str(typ) for t in ('NewsArticle', 'Article', 'BlogPosting')):
                add(obj.get('headline') or obj.get('name'), obj.get('url') or url, obj.get('datePublished'))
            for value in obj.values():
                if isinstance(value, (dict, list)): walk(value)
    for script in doc.xpath('//script[@type="application/ld+json"]/text()'):
        try: walk(json.loads(script))
        except (ValueError, TypeError): pass
    published = doc.xpath('//meta[@property="article:published_time"]/@content')
    if published:
        title = doc.xpath('//meta[@property="og:title"]/@content') or doc.xpath('//h1/text()')
        add(title[0] if title else '', url, published[0])
    # Explicit publication date adjacent to the article title (never a related story/footer).
    if re.search(r'/(article|clanek|novinky)/.+', url):
        for heading in doc.xpath('//h1')[:1]:
            times = heading.getparent().xpath('.//time[@datetime]/@datetime')
            if times: add(heading.text_content(), url, times[0])
    # Public RSS/Atom feeds, when advertised by the official club site.
    if source.lstrip().startswith('<?xml') or '<rss' in source[:500] or '<feed' in source[:500]:
        try:
            tree = etree.fromstring(source.encode(), etree.XMLParser(resolve_entities=False, no_network=True))
            for item in tree.xpath('//*[local-name()="item" or local-name()="entry"]'):
                field = lambda key: item.xpath('./*[local-name()="'+key+'"]')
                title, link, published = field('title'), field('link'), field('pubDate') or field('published')
                if title and link and published: add(title[0].text, link[0].get('href') or link[0].text, published[0].text)
        except etree.XMLSyntaxError: pass
    return items

def same_host(a, b):
    return (urlparse(a).hostname or '').removeprefix('www.') == (urlparse(b).hostname or '').removeprefix('www.')

def club_news(club_url, now):
    page = html.fromstring(context.fetch_text(club_url, attempts=2))
    official = page.xpath('//a[contains(@class,"ico-official")]/@href')
    if not official: raise ValueError('Official club website link not found')
    site = official[0].replace('http://', 'https://', 1)
    source = context.fetch_text(site, attempts=2)
    doc = html.fromstring(source)
    social = sorted(set(urljoin(site, a) for a in doc.xpath('//a/@href')
                        if urlparse(urljoin(site, a)).hostname in ('www.facebook.com', 'facebook.com', 'www.instagram.com', 'instagram.com', 'x.com', 'twitter.com')))
    candidates = doc.xpath('//link[contains(@type,"rss") or contains(@type,"atom")]/@href')
    candidates += [a for a in doc.xpath('//a/@href') if re.search(r'/(clanek|clanky|novinky|novinka|article|news)/', a)]
    items = structured_news(source, site, datetime.now(timezone.utc))
    visited = {site}; errors = []
    for candidate in candidates:
        link = urljoin(site, candidate).split('#')[0]
        if link in visited or not same_host(link, site): continue
        visited.add(link)
        if len(visited) > 7: break
        try:
            article = context.fetch_text(link, attempts=1)
            items += structured_news(article, link, datetime.now(timezone.utc))
        except Exception as exc: errors.append(str(exc)[:200])
    unique = {row['sourceUrl']: row for row in items}
    return sorted(unique.values(), key=lambda n: n['publishedAt'], reverse=True)[:6], social[:8], {
        'status': 'partial' if errors else 'checked-public-pages', 'sourceUrl': site,
        'pagesChecked': min(len(visited), 7), 'datedRecentArticles': len(unique),
        'errors': errors, 'socialStatus': 'links-only-not-crawled'}

def collect_team(team, clubs, now):
    out = {'fetchedAt': iso(now), 'matches': [], 'news': [], 'social': [], 'coverage': {}}
    for feed, suffix in (('results', 'vysledky'), ('fixtures', 'program')):
        url = 'https://www.livesport.cz/tym/'+context.team_page_path(team)+'/'+suffix+'/'
        try:
            source = context.fetch_text(url, attempts=2)
            out['matches'] += feed_events(source, feed, team, datetime.now(timezone.utc))
            out['coverage'][feed] = 'ok'
        except Exception as exc: out['coverage'][feed] = 'unavailable: '+str(exc)[:250]
    out['matches'] = list({row['id']: row for row in out['matches']}.values())
    try:
        out['news'], out['social'], out['coverage']['news'] = club_news(clubs[team], now)
    except Exception as exc: out['coverage']['news'] = {'status': 'unavailable', 'error': str(exc)[:250]}
    out['fetchedAt'] = iso(datetime.now(timezone.utc))
    return team, out

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT/'prediction-evidence-live.json')
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    rosters = json.loads((ROOT/'rosters-live.json').read_text(encoding='utf-8'))
    clubs = rosters['sources']['officialClubs']
    with ThreadPoolExecutor(max_workers=4) as pool:
        teams = dict(pool.map(lambda team: collect_team(team, clubs, now), context.TEAM_SEGMENTS))
    payload = {'schemaVersion': 1, 'season': context.SEASON, 'fetchedAt': iso(datetime.now(timezone.utc)), 'teams': teams,
               'policy': 'No paid API. News titles/links only; no inferred injury, sentiment, rotation or European win bonus.'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    complete = sum(t['coverage'].get('results') == t['coverage'].get('fixtures') == 'ok' for t in teams.values())
    print(f'Factual workload coverage: {complete}/16; optional gaps explicitly recorded, no stale fallback.')
    for team, row in teams.items():
        print(json.dumps({'team': team, 'matches': len(row['matches']), 'news': len(row['news']), 'coverage': row['coverage']}, ensure_ascii=True))

if __name__ == '__main__': main()
