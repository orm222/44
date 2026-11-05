"""Browser-controller tests use explicit simulated probes and real local servers.

These tests do not claim a real installed browser was exercised. The production
worker's unavailable-dependency path is also executed without installing software.
"""
import copy
import importlib.util
import json
from pathlib import Path
import socket
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch


BASE = Path(__file__).resolve().parents[1]
LOADER = importlib.util.spec_from_file_location('tested_assurance_browser', BASE / 'assurance_browser.py')
browser = importlib.util.module_from_spec(LOADER)
LOADER.loader.exec_module(browser)

SERVER = r'''
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import os, sys
directory = Path(os.environ['HARNESS_BROWSER_RUN_DIR'])
counter = directory / 'server-starts.txt'
counter.write_text(str(int(counter.read_text())+1) if counter.exists() else '1')
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'<h1>Fixture app</h1>')
    def log_message(self, *args):
        pass
HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
'''

FAKE_PROBE = r'''
# Explicit offline simulation for controller tests, not a browser implementation.
import argparse, json, struct, zlib
from pathlib import Path
p=argparse.ArgumentParser()
p.add_argument('--config');p.add_argument('--output');p.add_argument('--deadline')
a=p.parse_args()
directory=Path(a.output)
config=json.loads(Path(a.config).read_text(encoding='utf-8'))
scenario=json.loads(Path('fixture.json').read_text(encoding='utf-8'))
count=directory/'probe-calls.txt'
count.write_text(str(int(count.read_text())+1) if count.exists() else '1')
if scenario.get('mutate'):
    path=Path('node_modules/unapproved.py');path.parent.mkdir(exist_ok=True);path.write_text('unexpected')
if scenario.get('mutate_control'):
    Path('.harness/user-state.json').write_text('unexpected control change')
def chunk(name,data):
    return struct.pack('!I',len(data))+name+data+struct.pack('!I',zlib.crc32(name+data)&0xffffffff)
def png(width,height):
    header=struct.pack('!2I5B',width,height,8,2,0,0,0)
    pixels=(b'\0'+b'\xff\xff\xff'*width)*height
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',header)+chunk(b'IDAT',zlib.compress(pixels))+chunk(b'IEND',b'')
cases=[]
for case in config['cases']:
    name='case-'+case['id']
    value={'id':case['id'],'path':case['path'],'width':case['width'],'height':case['height'],
           'passed':not scenario.get('fail'),'assertions':[{'selector':x['selector'],'passed':not scenario.get('fail')} for x in case['expect']],
           'issues':['Simulated DOM failure'] if scenario.get('fail') else [],'screenshot':name+'.png'}
    (directory/(name+'.png')).write_bytes(png(case['width'],case['height']))
    (directory/(name+'.json')).write_text(json.dumps(value),encoding='utf-8')
    cases.append(value)
status='FAIL' if scenario.get('fail') else 'PASS'
(directory/'browser-result.json').write_text(json.dumps({'version':1,'engine':'playwright',
    'browser':config['browser'],'status':status,'reason':None,'cases':cases}),encoding='utf-8')
print('Explicit simulated probe fixture')
raise SystemExit(1 if scenario.get('fail') else 0)
'''


class Interrupted(BaseException):
    pass


class BrowserAssuranceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='browser assurance ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.app, self.problem = self.root / 'app-project', self.root / 'problem-project'
        self.app.mkdir()
        self.problem.mkdir()
        (self.app / 'server_fixture.py').write_text(SERVER, encoding='utf-8')
        (self.app / 'fixture.json').write_text('{}', encoding='utf-8')
        (self.problem / 'SPEC.md').write_text('A web fixture', encoding='utf-8')
        browser.clock.begin(self.problem)
        with socket.socket() as connection:
            connection.bind(('127.0.0.1', 0))
            self.port = connection.getsockname()[1]
        self.config = {'browser': 'chromium', 'server': {'argv': [sys.executable, 'server_fixture.py', str(self.port)],
            'cwd': '.', 'ready_url': 'http://127.0.0.1:' + str(self.port) + '/', 'timeout_seconds': 5},
            'cases': [{'id': 'desktop', 'path': '/', 'width': 640, 'height': 480, 'steps': [],
                       'expect': [{'selector': 'h1', 'text': 'Fixture app'}]},
                      {'id': 'mobile', 'path': '/', 'width': 390, 'height': 844, 'steps': [],
                       'expect': [{'selector': 'h1', 'text': 'Fixture app'}]}]}
        self.run_dir = self.app / '.harness/browser/one'
        self.probe = self.root / 'explicit_simulation.py'
        self.probe.write_text(FAKE_PROBE, encoding='utf-8')

    def run_fixture(self):
        with patch.object(browser, 'PROBE', self.probe):
            return browser.run_browser(self.app, self.problem, self.config, self.run_dir)

    def scenario(self, **value):
        (self.app / 'fixture.json').write_text(json.dumps(value), encoding='utf-8')

    def assert_stopped(self):
        with socket.socket() as connection:
            connection.settimeout(0.2)
            self.assertNotEqual(connection.connect_ex(('127.0.0.1', self.port)), 0)

    def test_contract_rejects_external_origins_arbitrary_js_and_unbounded_cases(self):
        invalid = []
        value = copy.deepcopy(self.config); value['server']['ready_url'] = 'https://example.com:443/'; invalid.append(value)
        value = copy.deepcopy(self.config); value['cases'][0]['path'] = '//example.com'; invalid.append(value)
        value = copy.deepcopy(self.config); value['cases'][0]['steps'] = [{'action': 'evaluate', 'selector': 'body', 'value': 'alert(1)'}]; invalid.append(value)
        value = copy.deepcopy(self.config); value['server']['timeout_seconds'] = 181; invalid.append(value)
        value = copy.deepcopy(self.config); value['cases'][0]['expect'] = []; invalid.append(value)
        value = copy.deepcopy(self.config); value['cases'][0]['width'] = 0; invalid.append(value)
        value = copy.deepcopy(self.config); value['cases'][1]['id'] = value['cases'][0]['id']; invalid.append(value)
        for value in invalid:
            with self.subTest(config=value), self.assertRaises(ValueError):
                browser.validate_browser(value)

    def test_simulated_success_has_both_viewports_and_server_cleanup(self):
        result = self.run_fixture()
        self.assertEqual(result['status'], 'PASS')
        self.assertTrue(result['passed'])
        self.assertTrue(result['server']['cleanup']['completed'])
        self.assertEqual([case['id'] for case in result['cases']], ['desktop', 'mobile'])
        self.assertEqual(len(result['artifacts']), 2)
        self.assertEqual(browser.verify_browser(result), result)
        self.assert_stopped()

    def test_success_is_reused_without_restarting_owned_server_or_probe(self):
        first = self.run_fixture()
        with patch.object(browser.processes, 'start_process', side_effect=AssertionError('unexpected launch')):
            self.assertEqual(self.run_fixture(), first)
        self.assertEqual((self.run_dir / 'server-starts.txt').read_text(), '1')
        self.assertEqual((self.run_dir / 'probe-calls.txt').read_text(), '1')

    def test_missing_screenshot_cannot_pass_historical_verification(self):
        result = self.run_fixture()
        (self.run_dir / 'case-mobile.png').unlink()
        with self.assertRaisesRegex(ValueError, 'evidence is missing or changed'):
            browser.verify_browser(result)

    def test_unknown_server_exit_never_relaunches_or_kills_a_pid(self):
        self.run_fixture()
        (self.run_dir / 'report.json').unlink()
        (self.run_dir / 'server.exit.json').unlink()
        with patch.object(browser.processes, 'start_process') as start, patch.object(browser.processes, 'stop_process') as stop:
            with self.assertRaisesRegex(ValueError, 'no exit receipt'):
                self.run_fixture()
            start.assert_not_called()
            stop.assert_not_called()
            (self.run_dir / 'server.launch.json').unlink()
            with self.assertRaisesRegex(ValueError, 'no server launch binding'):
                self.run_fixture()
            start.assert_not_called()
            stop.assert_not_called()

    def test_report_publication_interruption_recovers_without_new_processes(self):
        original = browser.runtime.write_record
        captured = []
        def interrupt(path, value):
            if Path(path).name == 'report.json':
                captured.append(value)
                raise Interrupted()
            return original(path, value)
        with patch.object(browser.runtime, 'write_record', side_effect=interrupt), self.assertRaises(Interrupted):
            self.run_fixture()
        stderr = self.run_dir / 'server.stderr.log'
        diagnostic = dict(captured[0], server_stderr=stderr.read_text(encoding='utf-8', errors='replace')[-4000:]
                          if stderr.exists() else '(server was not launched)')
        self.assertTrue(captured[0]['passed'], diagnostic)
        self.assert_stopped()
        with patch.object(browser.processes, 'start_process', side_effect=AssertionError('unexpected launch')):
            resumed = self.run_fixture()
            self.assertTrue(resumed['passed'], resumed)

    def test_ignored_source_mutation_blocks_and_cleans_server(self):
        self.scenario(mutate=True)
        with self.assertRaisesRegex(ValueError, 'protected'):
            self.run_fixture()
        result = browser.runtime.read_record(self.run_dir / 'report.json')
        self.assertFalse(result['passed'])
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertTrue(result['protected_changed'])
        self.assertTrue(result['server']['cleanup']['completed'])
        self.assert_stopped()

    def test_control_mutation_raises_after_cleanup_and_preserves_user_bytes(self):
        self.scenario(mutate_control=True)
        target = self.app / '.harness/user-state.json'
        target.parent.mkdir(exist_ok=True)
        target.write_text('original controller state', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'protected'):
            self.run_fixture()
        self.assertEqual(target.read_text(), 'unexpected control change')
        result = browser.runtime.read_record(self.run_dir / 'report.json')
        self.assertTrue(result['protected_changed'])
        self.assertTrue(result['server']['cleanup']['completed'])
        self.assert_stopped()
        with patch.object(browser.processes, 'start_process') as start:
            with self.assertRaisesRegex(ValueError, 'protection failed'):
                self.run_fixture()
            start.assert_not_called()

    def test_existing_service_port_is_not_reused_or_stopped(self):
        with socket.socket() as service:
            service.bind(('127.0.0.1', self.port))
            service.listen()
            with patch.object(browser.processes, 'start_process') as start, patch.object(browser.processes, 'stop_process') as stop:
                result = self.run_fixture()
                self.assertEqual(result['status'], 'BLOCKED')
                self.assertIn('occupied', result['reason'])
                start.assert_not_called()
                stop.assert_not_called()
            self.assertGreater(service.fileno(), -1)

    def test_expired_original_deadline_prevents_start(self):
        with patch.object(browser.clock, 'budget', side_effect=ValueError('deadline expired')):
            with patch.object(browser.processes, 'start_process') as start:
                with self.assertRaisesRegex(ValueError, 'deadline expired'):
                    self.run_fixture()
                start.assert_not_called()

    def test_simulated_dom_failure_is_fail_and_preserves_screenshots(self):
        self.scenario(fail=True)
        result = self.run_fixture()
        self.assertEqual(result['status'], 'FAIL')
        self.assertFalse(result['passed'])
        self.assertEqual(len(result['artifacts']), 2)
        self.assert_stopped()

    def test_missing_server_is_blocked_without_unowned_cleanup(self):
        self.config['server']['argv'] = [str(self.app / 'missing-server-executable')]
        result = self.run_fixture()
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertFalse(result['server']['process_started'])
        self.assertTrue(result['server']['cleanup']['completed'])

    def test_production_probe_missing_dependency_is_blocked_without_installing(self):
        # Force the optional import unavailable regardless of the CI environment.
        loader = importlib.util.spec_from_file_location('tested_browser_probe', BASE / 'browser_probe.py')
        module = importlib.util.module_from_spec(loader)
        with patch.dict(sys.modules, {'assurance_browser': browser}):
            loader.loader.exec_module(module)
        with patch.dict(sys.modules, {'playwright': None, 'playwright.sync_api': None}):
            result = module.run(self.config, self.run_dir, browser.clock.load(self.problem)['deadline_at'])
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertIn('no installation was attempted', result['reason'])
        self.assertEqual(result['cases'], [])

    def test_probe_mock_waits_for_dom_text_and_records_http_errors(self):
        # Mocked Playwright verifies wiring only, not a real browser rendering.
        loader = importlib.util.spec_from_file_location('mocked_browser_probe', BASE / 'browser_probe.py')
        module = importlib.util.module_from_spec(loader)
        with patch.dict(sys.modules, {'assurance_browser': browser}):
            loader.loader.exec_module(module)
        package = types.ModuleType('playwright')
        package.__path__ = []
        api = types.ModuleType('playwright.sync_api')
        manager, playwright, engine = MagicMock(), MagicMock(), MagicMock()
        manager.__enter__.return_value = playwright
        playwright.chromium.launch.return_value = engine
        api.sync_playwright = lambda: manager
        assertion = MagicMock()
        api.expect = MagicMock(return_value=assertion)
        contexts = []
        for _ in self.config['cases']:
            context, page, callbacks = MagicMock(), MagicMock(), {}
            page.url = self.config['server']['ready_url']
            page.on.side_effect = lambda event, handler, callbacks=callbacks: callbacks.__setitem__(event, handler)
            def visit(url, wait_until, callbacks=callbacks):
                callbacks['response'](types.SimpleNamespace(status=503, url=url))
                return types.SimpleNamespace(status=200)
            page.goto.side_effect = visit
            page.evaluate.return_value = {'overflow': False, 'viewport': 640, 'unlabeled': []}
            page.screenshot.side_effect = lambda path, **kwargs: Path(path).write_bytes(b'explicit mock screenshot')
            context.new_page.return_value = page
            context.pages = [page]
            contexts.append(context)
        engine.new_context.side_effect = contexts
        self.run_dir.mkdir(parents=True)
        with patch.dict(sys.modules, {'playwright': package, 'playwright.sync_api': api}):
            result = module.run(self.config, self.run_dir, browser.clock.load(self.problem)['deadline_at'])
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(engine.new_context.call_count, 2)
        self.assertEqual(assertion.to_contain_text.call_count, 2)
        self.assertTrue(all(call.kwargs['timeout'] > 0 for call in assertion.to_contain_text.call_args_list))
        self.assertTrue(all(any('HTTP response error 503' in issue for issue in case['issues']) for case in result['cases']))
        for context in contexts:
            context.close.assert_called_once()
        engine.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
