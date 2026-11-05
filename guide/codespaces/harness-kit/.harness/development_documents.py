"""Pure document rendering; the controller owns atomic publication and approvals.

Flow IDs keep a user's explanation attached to its feature after reordering or
renaming. Python declarations are checked with AST; other languages receive an
identifier existence check and still require the reviewer to verify the flow.
"""
import ast
import hashlib
import html
import json
from pathlib import Path, PurePosixPath
import re
import stat


FLOW_HEADER = '| 기능 | 시작점(화면, URL, 명령) | 거치는 모듈 순서(파일:함수 > 파일:함수) | 저장, 읽는 곳 | 내 한 줄 설명 |'
FLOW_SEPARATOR = '|---|---|---|---|---|'
FLOW_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z')
FLOW_MARKER = re.compile(r'<!--\s*flow:([A-Za-z0-9][A-Za-z0-9_.-]{0,79})\s*-->')


def _text(value):
    if value is None:
        return ''
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _cell(value):
    return html.escape(_text(value), quote=False).replace('|', '&#124;').replace('\r\n', '\n').replace('\n', '<br>')


def _row(*values):
    return '| ' + ' | '.join(_cell(value) for value in values) + ' |'


def _ordinary_path(root, relative, optional=False):
    root = Path(root).absolute()
    if not isinstance(relative, str) or not relative or '\\' in relative or ':' in relative:
        raise ValueError('문서 대상은 프로젝트 상대 경로여야 합니다: ' + _text(relative))
    name = PurePosixPath(relative)
    if name.is_absolute() or '..' in name.parts or str(name) != relative:
        raise ValueError('문서 대상 경로가 프로젝트 밖을 가리킵니다: ' + relative)
    path = root.joinpath(*name.parts)
    if root != root.resolve() or not root.is_dir():
        raise ValueError('문서 프로젝트 경로가 없거나 우회되었습니다')
    for item in (path, *path.parents):
        if item == root:
            break
        if item.is_symlink() or item.absolute() != item.resolve():
            raise ValueError('문서 대상 경로가 우회되었습니다: ' + relative)
        if item.exists() and getattr(item.lstat(), 'st_file_attributes', 0) & 0x400:
            raise ValueError('문서 대상 경로가 우회되었습니다: ' + relative)
    if not path.exists():
        if optional:
            return path
        raise ValueError('흐름표에 적힌 파일이 없습니다: ' + relative)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('문서 대상은 일반 파일이어야 합니다: ' + relative)
    return path


def _python_functions(tree):
    names = set()
    def visit(node, prefix=''):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualified = prefix + child.name
                if not isinstance(child, ast.ClassDef):
                    names.update((child.name, qualified))
                visit(child, qualified + '.')
            else:
                visit(child, prefix)
    visit(tree)
    return names


def validate_flows(app_root, flows):
    """Reject nonexistent files/functions; this is not a call-graph proof."""
    if not isinstance(flows, list):
        raise ValueError('기능 흐름은 목록이어야 합니다')
    seen, sources = set(), {}
    for flow in flows:
        if not isinstance(flow, dict) or not isinstance(flow.get('id'), str) or not FLOW_ID.fullmatch(flow['id']):
            raise ValueError('기능 흐름 ID가 올바르지 않습니다')
        if flow['id'] in seen:
            raise ValueError('중복 기능 흐름 ID: ' + flow['id'])
        seen.add(flow['id'])
        if any(not isinstance(flow.get(key), str) or not flow[key].strip() for key in ('feature', 'entry')):
            raise ValueError('기능 이름과 시작점을 적어야 합니다')
        chain = flow.get('chain')
        if not isinstance(chain, list) or not chain:
            raise ValueError('기능 흐름에는 실제 파일과 함수가 필요합니다')
        for step in chain:
            if not isinstance(step, dict) or not isinstance(step.get('function'), str) or not step['function'].strip():
                raise ValueError('기능 흐름의 함수가 올바르지 않습니다')
            filename, function = step.get('file'), step['function']
            path = _ordinary_path(app_root, filename)
            if filename not in sources:
                source = path.read_text(encoding='utf-8-sig')
                if path.suffix.lower() == '.py':
                    try:
                        names = _python_functions(ast.parse(source, filename=filename))
                    except SyntaxError as exc:
                        raise ValueError('흐름표 대상 Python 파일의 구문이 올바르지 않습니다: ' + filename) from exc
                    sources[filename] = names
                else:
                    sources[filename] = source
            if function == '<module>':
                continue
            found = sources[filename]
            if isinstance(found, set):
                exists = function in found
            else:
                exists = re.search(r'(?<![\w$])' + re.escape(function) + r'(?![\w$])', found) is not None
            if not exists:
                raise ValueError('흐름표의 함수를 파일에서 찾지 못했습니다: ' + filename + ':' + function)
        for key in ('reads', 'writes'):
            values = flow.get(key, [])
            if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError('읽기·저장 위치는 문자열 목록이어야 합니다')


def _table_cells(line):
    parts, current, escaped = [], [], False
    for char in line.strip():
        if escaped:
            current.append(char if char == '|' else '\\' + char)
            escaped = False
        elif char == '\\':
            escaped = True
        elif char == '|':
            parts.append(''.join(current).strip())
            current = []
        else:
            current.append(char)
    if escaped:
        current.append('\\')
    parts.append(''.join(current).strip())
    if not parts or parts[0] or parts[-1]:
        raise ValueError('사용자 설명 표의 양 끝에 |가 필요합니다')
    return parts[1:-1]


def read_explanations(app_root):
    """Read only the human-owned last column; malformed rows stop regeneration."""
    target = _ordinary_path(app_root, 'doc/UNDERSTANDING.md', optional=True)
    if not target.exists():
        return {}
    notes = {}
    for line in target.read_text(encoding='utf-8-sig').splitlines():
        markers = FLOW_MARKER.findall(line)
        if not markers:
            continue
        if len(markers) != 1 or markers[0] in notes:
            raise ValueError('중복 또는 잘못된 사용자 설명 ID')
        cells = _table_cells(line)
        if len(cells) != 5 or not FLOW_MARKER.search(cells[0]):
            raise ValueError('사용자 설명 표는 5개 칸이어야 합니다')
        decoded = re.sub(r'<br\s*/?>', '\n', cells[-1], flags=re.I)
        notes[markers[0]] = html.unescape(decoded)
    return notes


def understanding_hash(app_root):
    target = _ordinary_path(app_root, 'doc/UNDERSTANDING.md', optional=True)
    return hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None


def _understanding(app_root, state):
    flows = state.get('flows', [])
    validate_flows(app_root, flows)
    notes = read_explanations(app_root)
    lines = ['# 기능별 실행 흐름과 이해 확인', '',
             '마지막 칸은 사용자가 직접 작성합니다. 기능 이름 옆의 숨은 flow ID는 보존해 주세요.',
             '파일·함수 존재 여부와 실제 호출 순서·저장소의 의미는 별도로 확인합니다. 설명 확인 결과는 검토 기록에 남습니다.', '',
             FLOW_HEADER, FLOW_SEPARATOR]
    current_ids = set()
    for flow in flows:
        current_ids.add(flow['id'])
        chain = ' > '.join(step['file'] + ':' + step['function'] for step in flow['chain'])
        storage = '읽기: ' + (', '.join(flow.get('reads', [])) or '없음')
        storage += ' / 저장: ' + (', '.join(flow.get('writes', [])) or '없음')
        line = _row(flow['feature'], flow['entry'], chain, storage, notes.get(flow['id'], ''))
        line = line.replace(' | ', ' <!-- flow:' + flow['id'] + ' --> | ', 1)
        lines.append(line)
    retired = {key: value for key, value in notes.items() if key not in current_ids and value}
    if retired:
        lines += ['', '## 이전 기능의 설명', '', '현재 흐름표에서 빠진 기능의 사용자 설명을 보존합니다.', '', FLOW_HEADER, FLOW_SEPARATOR]
        for flow_id, explanation in sorted(retired.items()):
            line = _row('이전 기능 ' + flow_id, '현재 흐름에서 제외', '—', '—', explanation)
            lines.append(line.replace(' | ', ' <!-- flow:' + flow_id + ' --> | ', 1))
    return '\n'.join(lines) + '\n'


def _plan(plan):
    lines = ['# 구현 계획', '', _text(plan.get('summary', '구현 계획 작성 대기')), '', '## 구성과 선택 이유', '']
    architecture = plan.get('architecture', {})
    if isinstance(architecture, dict):
        lines += [_row('항목', '내용'), '|---|---|']
        lines += [_row(key, value) for key, value in architecture.items()]
    else:
        lines.append(_text(architecture))
    if plan.get('stage_minutes'):
        labels = {'plan': '계획', 'implementation': '구현·검사·검토', 'docker': 'Docker·최종 확인'}
        lines += ['', '## 단계별 계획 시간', '', _row('단계', '계획 시간(분)'), '|---|---|']
        lines += [_row(labels.get(key, key), value) for key, value in plan['stage_minutes'].items()]
    lines += ['', '## 작업과 검증 기준', '', _row('작업', '요구사항', '변경 파일', '테스트 파일', '계획 시간(분)'), '|---|---|---|---|---|']
    for task in plan.get('tasks', []):
        lines.append(_row(task.get('id', '') + ' ' + task.get('title', ''), ', '.join(task.get('requirements', [])),
                          ', '.join(task.get('files', [])), ', '.join(task.get('test_files', [])), task.get('expected_minutes', '미정')))
        for check in task.get('checks', []):
            lines += ['', '- 검사 ' + _text(check.get('id')) + ': ' + _text(check.get('requirement')),
                      '  - 독립 기대값·판정 기준: ' + _text(check.get('oracle', '미기록')),
                      '  - 명령 인수: `' + _text(check.get('argv', [])) + '`']
    lines += _assurance_plan(plan.get('assurance'))
    return '\n'.join(lines) + '\n'


def _assurance_plan(config):
    lines = ['', '## 추가 검사 계약', '']
    if config is None:
        return lines + ['추가 검사 미설정: 이전 계획의 검증 범위를 유지하며 새 검사를 통과한 것으로 표시하지 않습니다.']
    lines += ['이 계약도 계획 승인 대상입니다. 적용 여부·기준값·측정 명령을 확인하세요. 접수 당시 마감과 승인 지점을 유지합니다.', '',
              _row('항목', '적용', '이유'), '|---|---|---|',
              _row('과제 분류', config.get('project_type'), config.get('reason')),
              _row('입력 파일 연결', len(config.get('joins', [])), config.get('join_reason')),
              _row('입력 처리대장', len(config.get('reconciliations', [])), config.get('reconciliation_reason')),
              _row('알고리즘 후보 비교', len(config.get('algorithms', [])), config.get('algorithm_reason')),
              _row('브라우저', '웹 검사 적용' if config.get('browser') else '해당 없음', config.get('reason')),
              '', '### 필요한 실행 환경', '', '```json',
              json.dumps(config.get('environment', {}), ensure_ascii=False, indent=2), '```']
    for key, title in (('joins', '입력 파일 연결'), ('reconciliations', '입력 처리대장'), ('algorithms', '알고리즘 후보 비교')):
        for item in config.get(key, []):
            contract = item.get('contract', item)
            lines += ['', '### ' + title + ': ' + _text(contract.get('id')), '',
                      '```json', json.dumps(contract, ensure_ascii=False, indent=2), '```']
            if 'after_task' in item:
                lines += ['', '- 담당 작업: ' + _text(item['after_task'])]
            if 'measurements' in item:
                lines += ['- 측정 산출물: ' + _text(item['measurements'])]
            if 'probe' in item:
                lines += ['- 실제 생성·측정 명령 계약:', '```json',
                          json.dumps(item['probe'], ensure_ascii=False, indent=2), '```']
    if config.get('browser'):
        lines += ['', '### 브라우저 서버·화면·조작·기대값', '', '```json',
                  json.dumps(config['browser'], ensure_ascii=False, indent=2), '```',
                  '', '브라우저가 없으면 BLOCKED입니다. 자동 설치하거나 화면 검사를 생략하지 않습니다.']
    return lines


def _decisions(state):
    plan = state.get('plan') or {}
    lines = ['# 판단과 방향 전환 기록', '', '## 최초 선택과 이유', '',
             _row('선택', '이유', '버린 대안'), '|---|---|---|']
    for item in plan.get('decisions', []):
        lines.append(_row(item.get('decision'), item.get('reason'), item.get('discarded', [])))
    lines += ['', '## 방향 전환 기록', '']
    for item in state.get('decisions', []):
        lines += ['- ' + _text(item.get('time', item.get('at', ''))) + ' ' + _text(item.get('id')) +
                  ' 전: ' + _text(item.get('before', '')) + ' > 후: ' + _text(item.get('after', item.get('decision', ''))),
                  '  - 왜: ' + _text(item.get('reason', '미기록')),
                  '  - 버린 대안: ' + _text(item.get('discarded', '미기록')),
                  '  - 예상 비용: ' + _text(item.get('expected_minutes', '미기록')) + '분',
                  '  - 승인 상태: ' + _text(item.get('status', '승인 대기')),
                  '  - 실제 비용: ' + _text(item.get('actual') if item.get('actual') is not None else '아직 적용 결과를 기록하지 않음')]
    if not state.get('decisions'):
        lines.append('등록된 방향 전환이 없습니다.')
    lines += ['', '## AI의 주장과 직접 확인한 결과', '', _row('시각', 'AI가 한 말', '어떻게 확인했나', '결과'), '|---|---|---|---|']
    for note in state.get('verification_notes', []):
        lines.append(_row(note.get('time', note.get('at', '')), note.get('claim'), note.get('method'), note.get('result')))
    lines += ['', '## 실패 대응과 판단', '']
    for event in state.get('history', []):
        if not any(key in event for key in ('diagnosis', 'hypothesis', 'disposition')):
            continue
        lines += ['- 발견: ' + _text(event.get('reason', event.get('summary', ''))),
                  '  - AI 진단: ' + _text(event.get('diagnosis', event.get('hypothesis'))),
                  '  - 받아들임·고침·버림과 이유: ' + _text(event.get('disposition')),
                  '  - 확인: ' + _text(event.get('verification', '미기록'))]
    lines += ['', '## 한계와 남은 확인', '', '- 어떤 입력에서 틀리나: ' + _text(state.get('known_bad_inputs', '확인된 내용 미기록')),
              '- 못 막은 것: ' + _text(state.get('unhandled', '확인된 내용 미기록')),
              '- 시간이 더 있었다면 할 검증: ' + _text(state.get('future_checks', '미기록'))]
    limitations = list(dict.fromkeys(plan.get('limitations', []) + state.get('limitations', [])))
    lines += ['- ' + _text(value) for value in limitations]
    if state.get('input_changes'):
        lines += ['', '## 입력 변경과 완료 판정 무효화', '', _row('시각', '이전 리비전', '변경 이유'), '|---|---|---|']
        for change in state['input_changes']:
            lines.append(_row(change.get('time'), change.get('previous_revision'), change.get('reason')))
    return '\n'.join(lines) + '\n'


def _changelog(state):
    lines = ['# 변경 이력', '', '과거의 수정 완료 기록은 당시의 검사·검토 결과입니다. 현재 리비전의 완료 상태는 아래 표와 하네스 상태로 확인합니다.', '',
             _row('시각·작업', '수정 이유', '변경 파일', '수정 내용', '당시 상태'), '|---|---|---|---|---|']
    for event in state.get('history', []):
        if not event.get('files'):
            continue
        verified = event.get('tests_passed') is True and event.get('review_approved') is True
        status = '수정 완료' if verified else '검증 대기'
        if event.get('rolled_back') is True:
            status = '실패한 변경 되돌림'
        lines.append(_row(_text(event.get('time', event.get('at', ''))) + ' ' + _text(event.get('task_id', event.get('id'))),
                          event.get('reason', ''), ', '.join(event['files']), event.get('summary', ''), status))
    plan = state.get('plan') or {}
    if plan.get('tasks'):
        lines += ['', '## 현재 리비전의 작업 상태', '', '리비전: ' + _text(state.get('revision', '미기록')), '',
                  _row('작업', '현재 상태'), '|---|---|']
        statuses = state.get('tasks') or {}
        for task in plan['tasks']:
            status = statuses.get(task['id'], {})
            lines.append(_row(task['id'] + ' ' + task.get('title', ''), '검증·검토 완료' if status.get('complete') is True else '미완료'))
    return '\n'.join(lines) + '\n'


def _check_entries(report):
    if isinstance(report, list):
        for entry in report:
            yield from _check_entries(entry)
    elif isinstance(report, dict):
        nested = report.get('checks', report.get('stages', report.get('report')))
        if isinstance(nested, (list, dict)):
            if isinstance(nested, dict):
                if any(key in nested for key in ('checks', 'stages', 'outcome', 'argv', 'report')):
                    yield from _check_entries(nested)
                    return
                nested = list(nested.values())
            yield from _check_entries(nested)
        else:
            yield report


def _usage_report(state):
    records = state.get('usage', state.get('token_usage'))
    if records is None:
        return ['실제 사용량 미집계']
    if isinstance(records, dict):
        records = [dict(role='미기록', run_id='미기록', usage=records)]
    if not isinstance(records, list):
        raise ValueError('토큰 사용량 기록은 목록이어야 합니다')
    lines = [_row('역할', '실행 ID', '입력 토큰', '입력 중 캐시', '출력 토큰', '입력+출력'), '|---|---|---|---|---|---|']
    fields = ('input_tokens', 'cached_input_tokens', 'output_tokens')
    totals, measured = dict.fromkeys(fields, 0), dict.fromkeys(fields, 0)
    for record in records:
        usage = record.get('usage')
        values = {}
        for field in fields:
            value = usage.get(field) if isinstance(usage, dict) and usage.get('reported') is not False else None
            values[field] = value if type(value) is int and value >= 0 else None
            if values[field] is not None:
                totals[field] += value
                measured[field] += 1
        complete = values['input_tokens'] is not None and values['output_tokens'] is not None
        total = values['input_tokens'] + values['output_tokens'] if complete else '미보고'
        lines.append(_row(record.get('role', '미기록'), record.get('run_id', '미기록'),
                          *[values[field] if values[field] is not None else '미보고' for field in fields], total))
    if records:
        total = totals['input_tokens'] + totals['output_tokens'] if measured['input_tokens'] and measured['output_tokens'] else '미보고'
        lines.append(_row('보고된 값 합계', '', *[totals[field] if measured[field] else '미보고' for field in fields], total))
        lines.append('')
        lines.append('항목별 보고 호출 수: 입력 {}/{}회, 캐시 {}/{}회, 출력 {}/{}회.'.format(
            measured['input_tokens'], len(records), measured['cached_input_tokens'], len(records), measured['output_tokens'], len(records)))
    else:
        lines += ['', '아직 기록된 모델 호출이 없습니다.']
    lines += ['', '캐시 토큰은 입력 토큰의 일부이므로 합계에 다시 더하지 않습니다.',
              '미보고·null 값은 0으로 바꾸지 않습니다. 누락된 호출이 있으면 위 합계는 보고된 부분만의 합계입니다.']
    return lines


def _assurance_status(value):
    status = value.get('status')
    translations = {'LEGACY': '추가 검사 미설정', 'NOT_APPLICABLE': '해당 없음',
                    'DISCOVERED': '환경 조사 정보', 'PARTIAL': '일부 미확인'}
    return translations.get(status, status or ('PASS' if value.get('passed') is True else 'FAIL' if value.get('passed') is False else '미실행'))


def _counts_text(counts, selected):
    return ' / '.join(label + ' ' + _text(counts.get(key, '미기록')) for key, label in selected)


def _data_check_rows(report):
    lines = []
    for check in report.get('checks', []):
        counts = check.get('counts') or {}
        if check.get('kind') == 'join':
            measured = _counts_text(counts, [('left_rows', '왼쪽 행'), ('right_rows', '오른쪽 행'),
                ('projected_rows', '연결 예상 행'), ('unmatched_left_rows', '미연결 행'),
                ('left_duplicate_keys', '왼쪽 중복 키'), ('right_duplicate_keys', '오른쪽 중복 키')])
        else:
            measured = _counts_text(counts, [('input_rows', '입력 행'), ('output_rows', '대장 행'),
                ('missing_occurrences', '누락'), ('excess_occurrences', '초과'), ('unknown_keys', '미등록 키')])
        reason = check.get('error') or check.get('violations') or '위반 기록 없음'
        lines.append(_row(check.get('id', '미기록'), _assurance_status(check), measured, reason))
        for part in check.get('parts', []):
            lines.append(_row('대장 ' + _text(part.get('file')), part.get('kind'), '행 ' + _text(part.get('rows', '미기록')),
                              'SHA-256 ' + _text(part.get('sha256', '미기록'))))
    return lines


def _assurance_verification(state):
    lines = ['', '## 추가 검사 실행 근거', '',
             '계획에 적힌 검사는 실행 증거가 아닙니다. 아래는 저장된 실제 결과이며, 미설정·해당 없음·BLOCKED를 통과로 바꾸지 않습니다.']
    before = state.get('plan_checks') or {}
    environment = state.get('environment') or before.get('environment') or {}
    if environment:
        lines += ['', '### 실행 환경', '', _row('판정', 'Python', '필수 미준비', '패키지 수', '증거 파일'), '|---|---|---|---|---|',
                  _row(_assurance_status(environment), environment.get('python', '미기록'),
                       environment.get('missing_required', environment.get('missing', [])),
                       environment.get('installed_package_count', '미기록'), environment.get('report_path', '.harness/environment.json')),
                  '', '- 선택 패키지: ' + _text(environment.get('requested_packages', environment.get('python_packages', []))),
                  '- 요청 포트: ' + _text(environment.get('ports', []))]
    readiness = state.get('readiness_reports', [])
    if readiness:
        lines += ['', '### 계획 승인 후 실행 준비 확인', '',
                  '준비 확인은 패키지·실행기·엔진의 사용 가능 여부입니다. 이후 기능·Docker·실제 브라우저 검사를 대신하지 않습니다.', '',
                  _row('시도·판정', '사유', '환경 실측', 'Docker·Compose·브라우저', '증거 폴더'), '|---|---|---|---|---|']
        for index, saved in enumerate(readiness, 1):
            report = saved['report']
            measured = report.get('environment') or {}
            selected = {'Python': measured.get('python', '미기록'),
                        '필수 미준비': measured.get('missing_required', '미기록'),
                        '실행기': measured.get('executables', '미기록'),
                        '선택 패키지': measured.get('python_packages', '미기록'),
                        '포트': measured.get('ports', '미기록')}
            prerequisites = {}
            for key in ('docker', 'compose', 'browser'):
                value = report.get(key)
                if value is None:
                    prerequisites[key] = '미기록' if key == 'docker' else '해당 없음'
                else:
                    prerequisites[key] = {'status': _assurance_status(value)}
                    details = value.get('availability') or value
                    prerequisites[key].update({name: details[name] for name in ('browser', 'version', 'reason') if name in details})
            lines.append(_row(str(index) + ' / ' + _assurance_status(report), report.get('reason', '미기록'),
                              selected, prerequisites, report.get('run_dir', '미기록')))
    joins = before.get('joins') or {}
    if joins:
        lines += ['', '### 계획 검토 전 입력 연결', '', '판정: ' + _assurance_status(joins),
                  '계약 식별: ' + _text(before.get('seal', before.get('contract', '미기록'))), '',
                  _row('검사', '판정', '실제 측정', '위반·근거'), '|---|---|---|---|']
        lines += _data_check_rows(joins)
        lines += ['', '입력 파일 SHA-256: ' + _text(joins.get('inputs', {}))]
    runs = state.get('assurance_runs') or {}
    if not runs:
        lines += ['', '추가 검사 미설정' if (state.get('plan') or {}).get('assurance') is None else '작업 후·최종 추가 검사 실행 기록 없음.']
    for key, aggregate in runs.items():
        lines += ['', '### 실행 ' + _text(key), '', '판정: ' + _assurance_status(aggregate)]
        if aggregate.get('note'):
            lines.append(_text(aggregate['note']))
        for report in aggregate.get('reports', aggregate.get('results', [])):
            details = report.get('details') or report
            directory = report.get('run_dir', details.get('run_dir'))
            outcome = report.get('outcome') or details.get('probe') or {}
            lines += ['', '- 검사: ' + _text(report.get('id', report.get('kind', '미기록'))) +
                      ' / 담당 작업: ' + _text(report.get('task_id', '미기록')) + ' / 판정: ' + _assurance_status(report)]
            if directory:
                lines.append('- 실행 보고서: ' + str(Path(directory) / 'report.json'))
            if outcome:
                lines += ['- 명령 종료 코드: ' + _text(outcome.get('exit_code', '미기록')) +
                          ' / 정리 완료: ' + _text((outcome.get('cleanup') or {}).get('completed', '미기록')),
                          '- 실행 로그: ' + ' / '.join(_text(outcome.get(k, '미기록')) for k in ('stdout_path', 'stderr_path'))]
                if outcome.get('prefix'):
                    lines.append('- 실제 명령 기록: ' + _text(outcome['prefix']) + '.command.json')
            if details.get('reason'):
                lines.append('- 사유: ' + _text(details['reason']))
            if report.get('kind') == 'reconciliation':
                lines += ['', _row('검사', '판정', '실제 측정', '위반·근거'), '|---|---|---|---|'] + _data_check_rows(details)
            elif report.get('kind') == 'algorithm':
                lines += ['- 사례 수: ' + _text(details.get('case_count', '미기록')) + ' / 후보 수: ' + _text(details.get('candidate_count', '미기록')) +
                          ' / 측정 수: ' + _text(details.get('measurement_count', '미기록')), '',
                          _row('사례', '후보별 목적값·제약', '절대 퇴보량', '정확값·상대 격차', '결과·사유'), '|---|---|---|---|---|']
                for case in details.get('cases', []):
                    candidates = [{'id': item.get('id'), 'objective': item.get('objective'),
                                   'feasible': item.get('feasible'), 'violations': item.get('violations', [])}
                                  for item in case.get('candidates', [])]
                    gap = '미측정' if case.get('relative_gap') is None else case['relative_gap']
                    exact = '미측정' if case.get('exact') is None else case['exact']
                    lines.append(_row(case.get('case_id'), candidates,
                        case['absolute_regression'] if case.get('absolute_regression') is not None else '미측정',
                        '정확값 ' + _text(exact) + ' / 격차 ' + _text(gap) + ' / ' + _text(case.get('exact_status')),
                        {'passed': case.get('passed'), 'reasons': case.get('reasons', [])}))
                lines += ['', '원본·측정·계약 해시: ' + _text(details.get('evidence', {})),
                          '일반 최적성 증명으로 해석하지 않습니다. 정확한 값이 없는 사례는 미측정으로 남습니다.']
            elif report.get('kind') == 'browser':
                lines += ['', _row('화면', '주소·크기', '판정·기대값', '측정·문제', 'PNG 경로'), '|---|---|---|---|---|']
                for case in details.get('cases', []):
                    path = str(Path(directory) / case['screenshot']) if directory and case.get('screenshot') else '미생성'
                    issues = case.get('issues', [])
                    lines.append(_row(case.get('id'), _text(case.get('path')) + ' / ' + _text(case.get('width')) + '×' + _text(case.get('height')),
                        {'passed': case.get('passed'), 'assertions': case.get('assertions', [])},
                        {'measurements': case.get('layout', case.get('measurements', {})), 'issue_count': len(issues), 'first_issues': issues[:3]}, path))
                for artifact in details.get('artifacts', []):
                    if isinstance(artifact, dict) and artifact.get('path'):
                        lines.append('- 브라우저 산출물: ' + _text(artifact['path']))
            if report.get('artifacts'):
                lines.append('- 산출물 해시: ' + _text(report['artifacts']))
    return lines


def _verification(state):
    lines = ['# 실행·검증 보고서', '', '실행 기록을 요약합니다. 표시되지 않은 검사는 통과로 간주하지 않습니다.', '',
             '현재 리비전: ' + _text(state.get('revision', '미기록')) + ' / 저장된 단계: ' + _text(state.get('phase', '미기록')),
             '모델 호출 시도: ' + _text(state.get('calls', '미기록')) + ' / 계획 호출 시도: ' + _text(state.get('plan_attempts', '미기록')), '',
             _row('검사', '명령 인수', '종료 코드', '테스트 수', '판정', '로그·증거'), '|---|---|---|---|---|---|']
    for check in _check_entries(state.get('checks', [])):
        outcome = check.get('outcome') or {}
        measurement = check.get('measurement') or {}
        count = measurement.get('tests', '미기록') if measurement.get('measured') is True else check.get(
            'test_count', check.get('tests_run', check.get('tests', '미기록')))
        result = check.get('status', '통과' if check.get('passed') is True else '실패' if check.get('passed') is False else '미검증')
        if check.get('observed_failure') is True:
            result = '의도한 실패 재현 확인'
        if count == '미기록' and (check.get('test_framework') or 'unittest' in check.get('argv', []) or 'pytest' in check.get('argv', [])):
            result = '테스트 수 증거 미기록'
        logs = [check.get(name, outcome.get(name)) for name in ('stdout_path', 'stderr_path', 'run_id')]
        lines.append(_row(check.get('id', check.get('stage', '검사')), check.get('argv', check.get('command', [])),
                          check.get('exit_code', outcome.get('exit_code', '미기록')), count, result,
                          ', '.join(str(value) for value in logs if value)))
    lines += _assurance_verification(state)
    lines += ['', '## 검토 기록', '', _row('대상', '판정', '내용'), '|---|---|---|']
    for review in state.get('reviews', []):
        approved = review.get('approved') is True
        lines.append(_row(review.get('task_id', review.get('id', review.get('stage', review.get('key', '검토')))),
                          '승인' if approved else '승인되지 않음', review.get('summary', review.get('findings', ''))))
    lines += ['', '## 토큰 사용량', ''] + _usage_report(state)
    return '\n'.join(lines) + '\n'


def _run_guide(plan):
    lines = ['# 실행 안내', '', '## 실행 명령', '', '아래 명령은 승인된 구현 계획의 인수 배열입니다. 프로젝트 루트에서 실행합니다.', '']
    commands = plan.get('run_commands', [])
    if not commands:
        lines.append('실행 명령은 아직 확정되지 않았습니다.')
    for command in commands:
        if isinstance(command, dict):
            lines += ['- ' + _text(command.get('description', '실행')),
                      '```json', json.dumps(command.get('argv', []), ensure_ascii=False), '```', '']
        else:
            lines += ['```json', json.dumps(command, ensure_ascii=False), '```', '']
    lines += ['', '## Docker', '', _text(plan.get('docker', 'Docker 구성·검증 대기')),
              '', 'Docker 빌드·컨테이너 기능 검사 결과는 [검증 보고서](VERIFICATION_REPORT.md)에 기록합니다.',
              '호스트 포트·환경 변수·영속 저장소 설정은 승인된 구성에 맞춰 적용하세요.']
    return '\n'.join(lines) + '\n'


def render_documents(app_root, problem_root, state):
    """Return targets only. The caller checks ownership, journals, then publishes.

    Confirmed input documents are copied byte-for-byte by the controller; this
    renderer does not normalize their approval-bound contents.
    """
    if not isinstance(state, dict):
        raise ValueError('개발 상태는 객체여야 합니다')
    plan = state.get('plan') or {}
    if not isinstance(plan, dict):
        raise ValueError('구현 계획은 객체여야 합니다')
    return {
        'doc/IMPLEMENTATION_PLAN.md': _plan(plan),
        'doc/DECISIONS.md': _decisions(state),
        'doc/CHANGELOG.md': _changelog(state),
        'doc/UNDERSTANDING.md': _understanding(app_root, state),
        'doc/VERIFICATION_REPORT.md': _verification(state),
        'doc/RUN_GUIDE.md': _run_guide(plan),
        'README.md': '# 구현 결과\n\n' + _text(plan.get('summary', '기능명세서에 따른 구현 프로젝트')) + '\n\n' +
                     '- [설치·실행 안내](doc/RUN_GUIDE.md)\n' +
                     '- [확정 요구사항](doc/CONFIRMED_REQUIREMENTS.md)\n' +
                     '- [기능명세서](doc/FUNCTIONAL_SPEC.md)\n' +
                     '- [구현 계획](doc/IMPLEMENTATION_PLAN.md)\n' +
                     '- [판단·방향 전환](doc/DECISIONS.md)\n' +
                     '- [변경 이력](doc/CHANGELOG.md)\n' +
                     '- [기능 흐름·내 설명](doc/UNDERSTANDING.md)\n' +
                     '- [검증 보고서](doc/VERIFICATION_REPORT.md)\n\n' +
                     '완료 여부와 미검증 항목은 검증 보고서 및 하네스 상태를 확인하세요.\n',
    }
