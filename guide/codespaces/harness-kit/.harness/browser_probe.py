"""Optional real Playwright worker. No installation, existing profiles or model JS."""
import argparse
import json
import os
from pathlib import Path
import time
from urllib.parse import quote, urlsplit

from assurance_browser import origin, validate_browser


# This diagnostic is maintained harness code, never supplied by a model/config.
LAYOUT_DIAGNOSTIC = r'''() => {
  const visible = el => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  const text = el => el && (el.textContent || '').trim();
  const named = el => (el.getAttribute('aria-label') || '').trim() ||
    (el.getAttribute('aria-labelledby') || '').split(/\s+/).some(id => text(document.getElementById(id))) ||
    Array.from(el.labels || []).some(text) || (el.getAttribute('title') || '').trim() ||
    (['button','submit','reset'].includes(el.type) && (el.value || '').trim()) ||
    (el.type === 'image' && (el.getAttribute('alt') || '').trim());
  const unlabeled = Array.from(document.querySelectorAll('input:not([type="hidden"]),select,textarea'))
    .filter(el => visible(el) && !named(el)).map(el => ({tag: el.tagName.toLowerCase(), type: el.type || '',
       id: el.id || '', name: el.getAttribute('name') || ''}));
  return {overflow: Math.max(document.documentElement.scrollWidth, document.body ? document.body.scrollWidth : 0) >
       window.innerWidth + 1, viewport: window.innerWidth, unlabeled: unlabeled.slice(0, 30)};
}'''


def _save(path, value):
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(str(temporary), str(path))


def _budget(deadline, ceiling=10000):
    remaining = deadline - time.time()
    if remaining <= 0:
        raise TimeoutError('Original problem deadline expired')
    return max(1, min(ceiling, int(remaining * 1000)))


def _safe_url(url):
    parsed = urlsplit(url)
    return parsed.scheme + '://' + parsed.netloc + parsed.path


def run(config, directory, deadline):
    result = {'version': 1, 'engine': 'playwright', 'browser': config['browser'], 'status': 'BLOCKED',
              'reason': None, 'cases': []}
    try:
        from playwright.sync_api import expect, sync_playwright
    except ImportError:
        result['reason'] = 'Playwright is not installed; no installation was attempted'
        return result
    approved_origin = origin(config['server']['ready_url'])
    private_values = {step['value'] for case in config['cases'] for step in case['steps'] if step['value']}
    private_values.update(quote(value, safe='') for value in tuple(private_values))
    def redact(value):
        value = str(value)
        for private in sorted(private_values, key=len, reverse=True):
            value = value.replace(private, '[입력값]')
        return value[:500]
    try:
        with sync_playwright() as playwright:
            try:
                browser = getattr(playwright, config['browser']).launch(headless=True, timeout=_budget(deadline, 30000))
            except Exception as exc:
                result['reason'] = 'Browser engine is unavailable: ' + type(exc).__name__
                return result
            try:
                for case in config['cases']:
                    record = {'id': case['id'], 'path': case['path'], 'width': case['width'], 'height': case['height'],
                              'passed': False, 'assertions': [], 'issues': [], 'screenshot': None}
                    context = browser.new_context(viewport={'width': case['width'], 'height': case['height']},
                        service_workers='block', accept_downloads=False)
                    try:
                        if not hasattr(context, 'route_web_socket'):
                            result['reason'] = 'Installed Playwright lacks WebSocket request isolation; no page was opened'
                            return result
                        def route_request(route):
                            try:
                                permitted = origin(route.request.url) == approved_origin
                            except ValueError:
                                permitted = False
                            if permitted:
                                route.continue_()
                            else:
                                record['issues'].append('External request blocked: ' + redact(_safe_url(route.request.url)))
                                route.abort()
                        def route_socket(route):
                            equivalent = route.url.replace('wss:', 'https:', 1).replace('ws:', 'http:', 1)
                            try:
                                permitted = origin(equivalent) == approved_origin
                            except ValueError:
                                permitted = False
                            if permitted:
                                route.connect_to_server()
                            else:
                                record['issues'].append('External WebSocket blocked')
                                route.close(code=1008, reason='Outside approved loopback origin')
                        context.route('**/*', route_request)
                        context.route_web_socket('**/*', route_socket)
                        page = context.new_page()
                        page.on('console', lambda message: record['issues'].append('Console error: ' + redact(message.text))
                                if message.type == 'error' else None)
                        page.on('pageerror', lambda error: record['issues'].append('JavaScript error: ' + redact(error)))
                        page.on('requestfailed', lambda request: record['issues'].append('Request failed: ' + redact(_safe_url(request.url))))
                        page.on('response', lambda response: record['issues'].append(
                            'HTTP response error ' + str(response.status) + ': ' + redact(_safe_url(response.url)))
                            if response.status >= 400 else None)
                        page.on('download', lambda download: record['issues'].append('Unexpected browser download'))
                        page.set_default_timeout(_budget(deadline))
                        page.set_default_navigation_timeout(_budget(deadline))
                        response = page.goto(approved_origin + case['path'], wait_until='domcontentloaded')
                        if response is None or response.status >= 400:
                            record['issues'].append('Page did not return a successful document')
                        for step in case['steps']:
                            page.set_default_timeout(_budget(deadline))
                            locator = page.locator(step['selector'])
                            if step['action'] == 'fill':
                                locator.fill(step['value'])
                            elif step['action'] == 'click':
                                locator.click()
                            elif step['action'] == 'select':
                                locator.select_option(step['value'])
                            else:
                                locator.press(step['value'])
                        for expectation in case['expect']:
                            page.set_default_timeout(_budget(deadline))
                            locator = page.locator(expectation['selector'])
                            locator.wait_for(state='visible')
                            try:
                                expect(locator).to_contain_text(expectation['text'], timeout=_budget(deadline))
                                matched = True
                            except Exception:
                                matched = False
                            record['assertions'].append({'selector': expectation['selector'], 'passed': matched})
                            if not matched:
                                record['issues'].append('DOM text expectation failed: ' + redact(expectation['selector']))
                        if origin(page.url) != approved_origin:
                            record['issues'].append('Navigation escaped the approved origin')
                        layout = page.evaluate(LAYOUT_DIAGNOSTIC)
                        if layout['overflow']:
                            record['issues'].append('Horizontal viewport overflow')
                        if layout['unlabeled']:
                            record['issues'].append('Visible inputs without accessible labels: ' + str(len(layout['unlabeled'])))
                        record['layout'] = layout
                    except Exception as exc:
                        # Playwright call logs can echo fill values; do not save them.
                        record['issues'].append('Browser interaction failed: ' + type(exc).__name__)
                    finally:
                        try:
                            if context.pages:
                                page = context.pages[0]
                                masks = [page.locator('input[type="password"]')]
                                masks.extend(page.locator(step['selector']) for step in case['steps'] if step['action'] == 'fill')
                                name = 'case-' + case['id'] + '.png'
                                page.screenshot(path=str(directory / name), full_page=False, mask=masks,
                                                timeout=_budget(deadline))
                                record['screenshot'] = name
                        except Exception as exc:
                            record['issues'].append('Screenshot unavailable: ' + type(exc).__name__)
                        context.close()
                    record['issues'] = list(dict.fromkeys(record['issues']))[:50]
                    record['passed'] = (not record['issues'] and len(record['assertions']) == len(case['expect'])
                                        and all(item['passed'] for item in record['assertions']) and bool(record['screenshot']))
                    _save(directory / ('case-' + case['id'] + '.json'), record)
                    result['cases'].append(record)
                result['status'] = 'PASS' if all(case['passed'] for case in result['cases']) else 'FAIL'
                result['reason'] = None if result['status'] == 'PASS' else 'One or more real-browser cases failed'
            finally:
                browser.close()
    except Exception as exc:
        result['status'], result['reason'] = 'BLOCKED', 'Browser runtime stopped: ' + type(exc).__name__
    return result


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--deadline', required=True, type=float)
    args = parser.parse_args(arguments)
    directory = Path(args.output).resolve()
    config = validate_browser(json.loads(Path(args.config).read_text(encoding='utf-8')))
    result = run(config, directory, args.deadline)
    _save(directory / 'browser-result.json', result)
    print(json.dumps({'kind': 'browser', 'status': result['status'], 'case_count': len(result['cases'])}, ensure_ascii=False))
    return 0 if result['status'] == 'PASS' else 2 if result['status'] == 'BLOCKED' else 1


if __name__ == '__main__':
    raise SystemExit(main())
