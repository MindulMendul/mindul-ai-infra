"""Run with Playwright Chromium and GRAFANA_ADMIN_PASSWORD in the environment."""
import asyncio
import os
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
        for environment, host in [('qa', 'qa-mindul-murder-mystery.duckdns.org'), ('production', 'mindul-murder-mystery.duckdns.org')]:
            page = await browser.new_page(locale='ko-KR')
            errors, batches, statuses = [], [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            def capture(request):
                if '/telemetry/collect' in request.url:
                    try: batches.append(request.post_data_json)
                    except Exception: pass
            page.on('request', capture)
            page.on('response', lambda response: statuses.append(response.status) if '/telemetry/collect' in response.url else None)
            await page.goto('https://' + host, wait_until='networkidle')
            await page.locator('#nickname').fill('모니터링 검증')
            await page.get_by_role('button', name='게스트로 시작하기').click()
            await page.get_by_text('실시간 연결됨', exact=True).wait_for(timeout=15000)
            await page.evaluate("fetch('/api/health').then(r=>r.json())")
            if environment == 'qa':
                await page.evaluate("setTimeout(()=>{throw new TypeError('rum-privacy-sentinel-room')},0)")
            await page.wait_for_timeout(6000)
            await page.goto('about:blank')
            await page.wait_for_timeout(2000)
            unexpected = [error for error in errors if 'rum-privacy-sentinel-room' not in error]
            assert not unexpected, unexpected
            assert statuses and all(status == 202 for status in statuses), statuses
            events = {event['name'] for batch in batches for event in batch.get('events', [])}
            measurements = {name for batch in batches for measurement in batch.get('measurements', []) for name in measurement['values']}
            assert {'page_view', 'session_start', 'api_request', 'ws_open'} <= events, events
            assert {'lcp', 'fcp', 'ttfb'} <= measurements, measurements
            assert any('traces' in batch for batch in batches), 'Missing HTTP traces'
            if environment == 'qa': assert any('exceptions' in batch for batch in batches)
            print(environment + ': login, WebSocket, page views, Web Vitals, HTTP traces and collector 202 OK')
            await page.close()
        page = await browser.new_page(locale='ko-KR')
        await page.goto('https://mindul-server.duckdns.org/grafana/login', wait_until='networkidle')
        await page.locator('input[name="user"]').fill('admin')
        await page.locator('input[name="password"]').fill(os.environ['GRAFANA_ADMIN_PASSWORD'])
        await page.locator('button[type="submit"]').click()
        await page.wait_for_timeout(2000)
        for uid in ['mindul-infra', 'mindul-rum', 'mindul-ai']:
            await page.goto('https://mindul-server.duckdns.org/grafana/d/' + uid, wait_until='networkidle')
            await page.wait_for_timeout(2000)
            assert '/login' not in page.url
            assert 'Mindul' in await page.title()
            assert await page.get_by_text('If you\'re seeing this Grafana has failed to load its application files', exact=False).count() == 0
            print('Grafana dashboard OK: ' + uid)
        await browser.close()

if __name__ == '__main__': asyncio.run(main())
