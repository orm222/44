"""Bounded CSV relationship checks and exact input-ledger reconciliation.

Checks preserve original ID strings. A reconciliation compares input disposition
ledgers, never an optimizer's expanded route/visit rows. Nothing is repaired or
written by this module; the controller persists reports and approval bindings.
"""
from collections import Counter
import csv
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat


MAX_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_ROWS = 10000
MAX_COLUMNS = 64
MAX_CELL = 2048
MAX_FILES = 32
MAX_CONTRACTS = 8
MAX_PARTS = 8
MAX_KEY_COLUMNS = 8
MAX_EXAMPLES = 5


def _object(**properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


_STRING = {'type': 'string'}
_STRINGS = {'type': 'array', 'items': _STRING}
JOIN_SCHEMA = _object(id=_STRING, left=_STRING, right=_STRING, left_keys=_STRINGS, right_keys=_STRINGS,
    relationship={'type': 'string', 'enum': ['one_to_one', 'many_to_one', 'one_to_many', 'many_to_many']},
    join_type={'type': 'string', 'enum': ['left', 'inner']}, allow_unmatched={'type': 'boolean'},
    allow_blank_keys={'type': 'boolean'}, expected_rows={'anyOf': [{'type': 'integer'}, {'type': 'null'}]})
RECONCILIATION_SCHEMA = _object(id=_STRING, input=_STRING, keys=_STRINGS, parts={'type': 'array', 'items': _object(
    file=_STRING, kind={'type': 'string', 'enum': ['used', 'excluded', 'quarantined']})})


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _text(value, label):
    _require(isinstance(value, str) and bool(value.strip()) and value == value.strip()
             and '\x00' not in value, label + ' must be a nonempty unpadded string')
    return value


def _relative(value):
    _text(value, 'CSV path')
    path = PurePosixPath(value)
    _require(not path.is_absolute() and '\\' not in value and ':' not in value
             and all(part not in ('', '.', '..') and not part.endswith((' ', '.')) for part in value.split('/')),
             'CSV path must be an ordinary project-relative path')
    _require(path.suffix.lower() == '.csv', 'Only explicit UTF-8 CSV files are supported')
    return path.as_posix()


def _keys(value):
    _require(isinstance(value, list) and 0 < len(value) <= MAX_KEY_COLUMNS,
             'Keys must contain one to eight column names')
    for name in value:
        _text(name, 'Key column')
    _require(len(set(value)) == len(value), 'Duplicate key columns are not allowed')
    return list(value)


def _contracts(values, schema):
    _require(isinstance(values, list) and len(values) <= MAX_CONTRACTS, 'At most eight contracts are supported')
    seen = set()
    for value in values:
        _require(isinstance(value, dict) and set(value) == set(schema['properties']), 'Contract fields differ from the schema')
        identifier = _text(value.get('id'), 'Contract id')
        _require(re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', identifier) is not None and identifier not in seen,
                 'Contract ids must be unique safe names')
        seen.add(identifier)


def validate_joins(joins):
    _contracts(joins, JOIN_SCHEMA)
    result = []
    for item in joins:
        left_keys, right_keys = _keys(item['left_keys']), _keys(item['right_keys'])
        _require(len(left_keys) == len(right_keys), 'Join key column counts must match')
        _require(item['relationship'] in ('one_to_one', 'many_to_one', 'one_to_many', 'many_to_many'),
                 'Unknown join relationship')
        _require(item['join_type'] in ('left', 'inner'), 'Only left and inner joins are supported')
        _require(type(item['allow_unmatched']) is bool and type(item['allow_blank_keys']) is bool,
                 'Join blank/unmatched policies must be explicit booleans')
        expected = item['expected_rows']
        _require(expected is None or (type(expected) is int and expected >= 0), 'expected_rows must be null or a nonnegative integer')
        result.append(dict(item, left=_relative(item['left']), right=_relative(item['right']),
                           left_keys=left_keys, right_keys=right_keys))
    return result


def validate_reconciliations(contracts):
    _contracts(contracts, RECONCILIATION_SCHEMA)
    result = []
    for item in contracts:
        parts = item['parts']
        _require(isinstance(parts, list) and 0 < len(parts) <= MAX_PARTS, 'A ledger needs one to eight declared result parts')
        paths, normalized = set(), []
        for part in parts:
            _require(isinstance(part, dict) and set(part) == {'file', 'kind'}, 'Ledger part fields differ from the schema')
            name = _relative(part['file'])
            _require(name.casefold() not in paths, 'The same artifact file cannot be counted twice')
            paths.add(name.casefold())
            _require(part['kind'] in ('used', 'excluded', 'quarantined'), 'Unknown input-ledger disposition')
            normalized.append(dict(file=name, kind=part['kind']))
        result.append(dict(id=item['id'], input=_relative(item['input']), keys=_keys(item['keys']), parts=normalized))
    return result


def _path(root, name):
    root = Path(root).absolute()
    _require(root.is_dir() and root == root.resolve() and not root.is_symlink(), 'CSV root is missing or redirected')
    path = root / _relative(name)
    _require(path.absolute() == path.resolve() and path.is_relative_to(root), 'CSV path is redirected or outside its project')
    for member in (path, *path.parents):
        _require(not member.is_symlink() and not (member.exists() and getattr(member.lstat(), 'st_file_attributes', 0) & 0x400),
                 'CSV path contains a link or redirected directory')
        if member == root:
            break
    _require(path.is_file(), 'Required CSV does not exist: ' + name)
    info = path.lstat()
    _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'CSV must be an ordinary file with one link')
    return path


class _CSVReader:
    def __init__(self):
        self.cache, self.total_bytes = {}, 0

    def load(self, root, name):
        path = _path(root, name)
        identity = os.path.normcase(str(path))
        if identity in self.cache:
            table = self.cache[identity]
            _require(_stamp(path.lstat()) == table['stamp'], 'CSV changed between contract reads: ' + name)
            return table
        before = path.lstat()
        _require(len(self.cache) < MAX_FILES, 'CSV file count exceeds the 32-file assurance limit')
        _require(before.st_size <= MAX_BYTES, 'CSV exceeds the 2 MiB assurance limit: ' + name)
        _require(self.total_bytes + before.st_size <= MAX_TOTAL_BYTES, 'CSV inputs exceed the total 8 MiB assurance limit')
        raw = path.read_bytes()
        after = path.lstat()
        _require(_stamp(before) == _stamp(after) and len(raw) == before.st_size and len(raw) <= MAX_BYTES,
                 'CSV changed while reading: ' + name)
        _path(root, name)
        text = raw.decode('utf-8-sig')
        reader = csv.reader(io.StringIO(text, newline=''), strict=True)
        header = next(reader, None)
        _require(header is not None and 0 < len(header) <= MAX_COLUMNS, 'CSV requires a header of at most 64 columns: ' + name)
        _require(all(isinstance(column, str) and column and column == column.strip() and len(column) <= 100
                     and '\x00' not in column for column in header), 'CSV has an empty, padded, or invalid header: ' + name)
        _require(len(header) == len(set(header)), 'CSV has duplicate headers: ' + name)
        rows = []
        for record, row in enumerate(reader, 2):
            _require(len(rows) < MAX_ROWS, 'CSV exceeds the 10000-row assurance limit: ' + name)
            _require(len(row) == len(header), 'CSV row width differs at record {}: {}'.format(record, name))
            _require(all(len(value) <= MAX_CELL and '\x00' not in value for value in row), 'CSV has an oversized or invalid cell: ' + name)
            rows.append(tuple(row))
        table = dict(header=header, rows=rows, sha256=hashlib.sha256(raw).hexdigest(), stamp=_stamp(after))
        self.cache[identity] = table
        self.total_bytes += len(raw)
        return table


def _stamp(info):
    return tuple(getattr(info, field) for field in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_mode', 'st_nlink'))


def _counts(table, keys):
    missing = [key for key in keys if key not in table['header']]
    _require(not missing, 'CSV is missing key columns: ' + ', '.join(missing))
    indexes = [table['header'].index(key) for key in keys]
    result, blanks = Counter(), 0
    for row in table['rows']:
        key = tuple(row[index] for index in indexes)
        if any(not component.strip() for component in key):
            blanks += 1
        else:
            result[key] += 1
    return result, blanks


def _examples(counter):
    result = []
    for key in sorted(counter)[:MAX_EXAMPLES]:
        shortened = [value if len(value) <= 80 else value[:80] + '…' for value in key]
        result.append(dict(key=shortened, key_truncated=any(len(value) > 80 for value in key), count=counter[key],
            key_sha256=hashlib.sha256(json.dumps(key, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()))
    return result


def _violation(code, count):
    return {'code': code, 'count': count}


def _blocked(identifier, kind, exc, stage=None):
    result = dict(id=identifier, kind=kind, passed=False, status='BLOCKED', error=str(exc))
    if stage is not None:
        result['stage'] = stage
    return result


def _report(checks, inputs, artifacts=None):
    status = ('NOT_APPLICABLE' if not checks else 'BLOCKED' if any(item['status'] == 'BLOCKED' for item in checks)
              else 'FAIL' if any(not item['passed'] for item in checks) else 'PASS')
    value = dict(passed=status in ('PASS', 'NOT_APPLICABLE'), status=status, checks=checks, inputs=inputs,
        counts={'contracts': len(checks), 'passed': sum(item['passed'] for item in checks),
                'failed': sum(item['status'] == 'FAIL' for item in checks),
                'blocked': sum(item['status'] == 'BLOCKED' for item in checks)},
        limits=dict(file_bytes=MAX_BYTES, total_bytes=MAX_TOTAL_BYTES, file_rows=MAX_ROWS,
                    file_columns=MAX_COLUMNS, files=MAX_FILES, examples=MAX_EXAMPLES))
    if artifacts is not None:
        value['artifacts'] = artifacts
    return value


def inspect_joins(problem_root, joins):
    """Audit exact joins. allow_unmatched governs nonblank LEFT input keys.

    Unused right-side master records are reported but are not automatically
    rejected. Blank keys never match one another; allow_blank_keys only permits
    their presence. The relationship contract still applies to all nonblank keys.
    """
    contracts, reader, inputs, results = validate_joins(joins), _CSVReader(), {}, []
    for item in contracts:
        try:
            left = reader.load(problem_root, item['left'])
            inputs[item['left']] = left['sha256']
            right = reader.load(problem_root, item['right'])
            inputs[item['right']] = right['sha256']
            _require(left['rows'], 'Join left input has zero rows; no input cases were inspected')
            lc, lb = _counts(left, item['left_keys'])
            rc, rb = _counts(right, item['right_keys'])
            ld, rd = Counter({key: n for key, n in lc.items() if n > 1}), Counter({key: n for key, n in rc.items() if n > 1})
            missing = Counter({key: n for key, n in lc.items() if key not in rc})
            unused = Counter({key: n for key, n in rc.items() if key not in lc})
            inner = sum(n * rc[key] for key, n in lc.items())
            left_rows = inner + sum(missing.values()) + lb
            projected = inner if item['join_type'] == 'inner' else left_rows
            violations = []
            if ld and item['relationship'] in ('one_to_one', 'one_to_many'):
                violations.append(_violation('left_key_not_unique', len(ld)))
            if rd and item['relationship'] in ('one_to_one', 'many_to_one'):
                violations.append(_violation('right_key_not_unique', len(rd)))
            if missing and not item['allow_unmatched']:
                violations.append(_violation('unmatched_left_key', len(missing)))
            if (lb or rb) and not item['allow_blank_keys']:
                violations.append(_violation('blank_key_rows', lb + rb))
            if item['expected_rows'] is not None and projected != item['expected_rows']:
                violations.append(_violation('projected_row_count_differs', abs(projected - item['expected_rows'])))
            results.append(dict(id=item['id'], kind='join', passed=not violations, status='FAIL' if violations else 'PASS',
                contract=item, counts=dict(left_rows=len(left['rows']), right_rows=len(right['rows']), left_unique_keys=len(lc),
                    right_unique_keys=len(rc), left_blank_rows=lb, right_blank_rows=rb, left_duplicate_keys=len(ld),
                    right_duplicate_keys=len(rd), unmatched_left_keys=len(missing), unmatched_left_rows=sum(missing.values()),
                    unused_right_keys=len(unused), unused_right_rows=sum(unused.values()), inner_rows=inner,
                    left_join_rows=left_rows, projected_rows=projected), violations=violations,
                examples=dict(left_duplicates=_examples(ld), right_duplicates=_examples(rd),
                    unmatched_left=_examples(missing), unused_right=_examples(unused)),
                evidence=dict(left_sha256=left['sha256'], right_sha256=right['sha256'])))
        except (OSError, UnicodeError, csv.Error, ValueError) as exc:
            results.append(_blocked(item['id'], 'join', exc))
    return _report(results, inputs)


def reconcile(problem_root, artifact_root, contracts):
    """Check an exact multiset partition of input IDs into disposition ledgers."""
    contracts, reader, inputs, artifacts, results = validate_reconciliations(contracts), _CSVReader(), {}, {}, []
    for item in contracts:
        stage = 'input'
        try:
            original = reader.load(problem_root, item['input'])
            inputs[item['input']] = original['sha256']
            _require(original['rows'], 'Reconciliation input has zero rows; no input cases were inspected')
            incoming, input_blanks = _counts(original, item['keys'])
            outgoing, output_blanks, parts = Counter(), 0, []
            stage = 'artifact'
            for part in item['parts']:
                table = reader.load(artifact_root, part['file'])
                artifacts[part['file']] = table['sha256']
                counts, blanks = _counts(table, item['keys'])
                outgoing.update(counts)
                output_blanks += blanks
                parts.append(dict(file=part['file'], kind=part['kind'], rows=len(table['rows']),
                                  blank_key_rows=blanks, sha256=table['sha256']))
            deficits, excesses = incoming - outgoing, outgoing - incoming
            unknown = Counter({key: count for key, count in outgoing.items() if key not in incoming})
            violations = []
            if input_blanks or output_blanks:
                violations.append(_violation('blank_ledger_key_rows', input_blanks + output_blanks))
            if deficits:
                violations.append(_violation('input_occurrences_missing', sum(deficits.values())))
            if excesses:
                violations.append(_violation('output_occurrences_excess', sum(excesses.values())))
            results.append(dict(id=item['id'], kind='input_ledger', passed=not violations,
                status='FAIL' if violations else 'PASS', contract=item,
                counts=dict(input_rows=len(original['rows']), output_rows=sum(part['rows'] for part in parts),
                    input_unique_keys=len(incoming), output_unique_keys=len(outgoing), input_blank_rows=input_blanks,
                    output_blank_rows=output_blanks, missing_keys=len(deficits), missing_occurrences=sum(deficits.values()),
                    excess_keys=len(excesses), excess_occurrences=sum(excesses.values()), unknown_keys=len(unknown)),
                parts=parts, violations=violations,
                examples=dict(missing=_examples(deficits), excess=_examples(excesses), unknown=_examples(unknown)),
                evidence=dict(input_sha256=original['sha256'], artifacts={part['file']: part['sha256'] for part in parts})))
        except (OSError, UnicodeError, csv.Error, ValueError) as exc:
            results.append(_blocked(item['id'], 'input_ledger', exc, stage=stage))
    return _report(results, inputs, artifacts)
