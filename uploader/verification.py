"""Read published posts and compare them with an imported source; never submits."""

import argparse
import asyncio
import json
import re
import sqlite3
from collections import Counter, deque
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from .__main__ import playwright_api
from .async_browser import AsyncFormPublisher
from .browser import StopBatch, locate, publication_body, read_config, write_state
from .parallel import BatchSignal
from .queue import read_posts


def canonical(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, unquote(parts.path).rstrip('/'), '', ''))


def compact(text):
    return re.sub(r'\s+', '', text)


def publication_matches(item, expected_images, config):
    """Optional publication policies apply only when enabled in this site's config."""
    if not (item['title_matches'] and item['body_matches']
            and item['body_characters_excluding_footer'] >= config.get('min_body_characters', 0)
            and item['images'] == expected_images):
        return False
    if config.get('post_footer', '').strip() and not (item['footer_count'] == 1 and item['footer_at_bottom']):
        return False
    if config.get('strip_sources_and_links') and (item['body_links'] or item['contains_url'] or item['contains_source_section']):
        return False
    return True


async def verify(args):
    config = read_config(args.config)
    settings = config.get('verification', {})
    source = read_posts(args.source)
    with sqlite3.connect(f'file:{Path(args.db).resolve()}?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = []
        for payload, fingerprint in source:
            row = db.execute('SELECT * FROM posts WHERE id=?', (payload['id'],)).fetchone()
            if row is None or row['fingerprint'] != fingerprint:
                raise ValueError(f"원고와 등록 이력이 다릅니다: {payload['id']}")
            rows.append(dict(row))
        counts = dict(db.execute('SELECT status,count(*) FROM posts GROUP BY status'))
    plan = deque(rows)
    report = {
        'checked_at': datetime.now().astimezone().isoformat(), 'source': str(Path(args.source).resolve()),
        'expected_count': len(rows), 'queue_counts': counts, 'checks': [],
        'read_only': True, 'complete': False,
    }
    if args.resume_report:
        previous = json.loads(Path(args.out).read_text(encoding='utf-8'))
        valid = {row['id']: row['result_url'] for row in rows if row['status'] == 'posted'}
        if previous.get('source') != report['source'] or previous.get('expected_count') != len(rows):
            raise ValueError('검증 기록의 원고 파일이 다릅니다.')
        cached = previous.get('checks', [])
        if len({item['id'] for item in cached}) != len(cached) or any(
                item['id'] not in valid or item['url'] != valid[item['id']] or not item['passed'] for item in cached):
            raise ValueError('검증 기록과 현재 등록 이력이 다릅니다.')
        report['checks'] = cached
        report['body_checked_at'] = previous.get('body_checked_at', previous['checked_at'])
        checked = {item['id'] for item in cached}
        plan = deque(row for row in rows if row['id'] not in checked)
    signal = BatchSignal()
    playwright_api()
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=args.session)
        try:
            async def worker():
                page = await context.new_page()
                guardian = AsyncFormPublisher(page, config, signal)
                while plan and signal.error is None:
                    row = plan.popleft()
                    item = {'id': row['id'], 'status': row['status'], 'url': row['result_url'], 'passed': False}
                    try:
                        if row['status'] != 'posted' or not row['result_url']:
                            raise ValueError('등록 완료 URL이 없습니다.')
                        payload = json.loads(row['payload'])
                        expected = publication_body(payload['body'], config)
                        await guardian.guard()
                        response = await page.goto(row['result_url'], wait_until='domcontentloaded')
                        await guardian.guard()
                        if response and response.status >= 400:
                            raise ValueError(f'게시글 HTTP {response.status}')
                        heading = locate(page, config['success'])
                        await heading.wait_for(state='visible')
                        item['title_matches'] = await heading.inner_text() == payload['title']
                        if settings.get('body'):
                            body = locate(page, settings['body'])
                            await body.wait_for(state='visible')
                        else:
                            # Default adapter for the sample cafe's paragraph structure.
                            first = page.get_by_text(expected.split('\n\n')[0], exact=True)
                            await first.wait_for(state='visible')
                            body = first.locator('xpath=../..')
                        actual = await body.inner_text()
                        footer = config.get('post_footer', '').strip()
                        actual = actual.rstrip()
                        story = actual[:-len(footer)].rstrip() if footer and actual.endswith(footer) else actual
                        item.update(
                            body_matches=compact(actual) == compact(expected),
                            body_characters_excluding_footer=len(story.strip()),
                            footer_count=actual.count(footer) if footer else 0,
                            footer_at_bottom=bool(footer) and actual.rstrip().endswith(footer),
                            images=await body.locator('img').count(),
                            body_links=await body.locator('a[href]').count(),
                            contains_url=bool(re.search(r'https?://|www\.', actual)),
                            contains_source_section=bool(re.search(r'(?m)^\s*(?:출처|참고\s*자료|Sources?)\s*[:：]', actual, re.I)),
                        )
                        await guardian.guard()
                        expected_images = len(payload['images']) if config.get('include_images', True) else 0
                        item['expected_images'] = expected_images
                        item['passed'] = publication_matches(item, expected_images, config)
                    except Exception as error:
                        item['error'] = f'{type(error).__name__}: {error}'
                        if isinstance(error, StopBatch):
                            signal.stop(error)
                    report['checks'].append(item)
                    if len(report['checks']) % 10 == 0 or not item['passed']:
                        write_state(report, args.out)
                        print(f"실제 게시글 검증 {len(report['checks'])}/{len(rows)}; 불일치 {sum(not x['passed'] for x in report['checks'])}", flush=True)
                await page.close()

            await asyncio.gather(*(worker() for _ in range(min(args.workers, len(rows)))))
            if signal.error is not None:
                report['stopped'] = str(signal.error)
            elif settings.get('verify_feed', True):
                # Count actual feed cards, loading older pages until all selected posts are present.
                feed = await context.new_page()
                guardian = AsyncFormPublisher(feed, config, signal)
                feed_url = settings.get('feed_url', urljoin(config['write_url'], './'))
                await feed.goto(feed_url, wait_until='domcontentloaded')
                expected_urls = {canonical(row['result_url']) for row in rows if row['result_url']}
                item_selector = settings.get('items', 'main a[href*="/posts/"]')
                title_selector = settings.get('title', 'p.t5-regular')
                await feed.locator(item_selector).first.wait_for(state='visible')
                seen = Counter()
                seen_titles = {}
                urls_by_title = {}
                stable = 0
                snapshots = 0
                # Large batches may need more than 35 feed pages. The loop
                # exits once every selected URL is seen or pagination ends.
                for _ in range(max(35, len(expected_urls) + 1)):
                    await guardian.guard()
                    cards = await feed.locator(item_selector).evaluate_all(
                        "(els,title)=>els.map(el=>({href:el.href, text:el.innerText, title:el.querySelector(title)?.innerText}))", title_selector)
                    snapshot = Counter(canonical(card['href']) for card in cards if not card['href'].split('?')[0].endswith('/new'))
                    for card in cards:
                        key = canonical(card['href'])
                        if card['title']:
                            urls_by_title.setdefault(card['title'], set()).add(key)
                        if key in expected_urls:
                            seen_titles[key] = card['text']
                    previous = len(seen)
                    for key, value in snapshot.items():
                        seen[key] = max(seen[key], value)
                    snapshots += 1
                    if expected_urls <= set(seen):
                        break
                    stable = stable + 1 if len(seen) == previous else 0
                    if stable >= 6:
                        break
                    more = feed.locator(settings.get('more', 'main button:text-is("더보기"):visible'))
                    # The new cards render before the loading state restores
                    # the next-page button. Wait for it instead of ending early.
                    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
                    try:
                        await more.wait_for(state='visible', timeout=5000)
                    except PlaywrightTimeoutError:
                        await guardian.guard()
                        break
                    count = len(cards)
                    await more.click()
                    try:
                        from playwright.async_api import expect
                        await expect(feed.locator(item_selector)).not_to_have_count(count, timeout=config['timeout_ms'])
                    except Exception:
                        await guardian.guard()
                        raise ValueError('게시판 더보기에서 추가 글이 표시되지 않았습니다.')
                report['feed'] = {'url': feed.url, 'snapshots': snapshots, 'selected_posts_seen': len(expected_urls & set(seen)),
                                  'selected_card_occurrences': {row['id']: seen[canonical(row['result_url'])] for row in rows if row['result_url']},
                                  'selected_title_occurrences': {row['id']: len(urls_by_title.get(json.loads(row['payload'])['title'], set())) for row in rows},
                                  'selected_title_matches': {row['id']: json.loads(row['payload'])['title'] in seen_titles.get(canonical(row['result_url']), '') for row in rows if row['result_url']}}
                await guardian.guard()
                await feed.close()
        except Exception as error:
            report['verification_error'] = f'{type(error).__name__}: {error}'
        finally:
            await context.close()
            await browser.close()
    urls = [canonical(row['result_url']) for row in rows if row['result_url']]
    report.update(unique_result_urls=len(set(urls)), failed_ids=[x['id'] for x in report['checks'] if not x['passed']],
                  unverified_ids=[row['id'] for row in rows if row['id'] not in {x['id'] for x in report['checks']}])
    feed = report.get('feed', {})
    report['complete'] = (len(report['checks']) == len(rows) and not report['failed_ids']
                          and len(set(urls)) == len(rows) and not report.get('verification_error')
                          and (not settings.get('verify_feed', True) or (
                              feed.get('selected_posts_seen') == len(rows)
                              and all(n == 1 for n in feed.get('selected_card_occurrences', {}).values())
                              and all(n == 1 for n in feed.get('selected_title_occurrences', {}).values())
                              and all(feed.get('selected_title_matches', {}).values()))))
    write_state(report, args.out)
    print(json.dumps({'complete': report['complete'], 'verified': len(report['checks']),
                      'failed_ids': report['failed_ids'], 'feed_posts_seen': feed.get('selected_posts_seen'),
                      'report': str(Path(args.out).resolve())}, ensure_ascii=False), flush=True)
    return report['complete']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--db', default='.state/queue.sqlite3')
    parser.add_argument('--session', default='.auth/session.json')
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--resume-report', action='store_true', help='같은 원고·URL의 통과 기록을 재사용하고 남은 확인을 계속함')
    args = parser.parse_args()
    if not 1 <= args.workers <= 8:
        parser.error('--workers는 1~8이어야 합니다.')
    raise SystemExit(0 if asyncio.run(verify(args)) else 1)


if __name__ == '__main__':
    main()
