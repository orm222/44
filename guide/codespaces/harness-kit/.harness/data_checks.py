"""Bounded, read-only CSV questions for intake, not a detector application.

The new 1.5-IQR screen is exploratory. I/MR formulae are used only with an
explicit baseline; observed spacing is not a confirmed sampling contract.
No row is repaired, dropped from the source, or declared a domain error.
"""
import csv
from datetime import datetime
import hashlib
import io
import json
import math
from pathlib import Path, PureWindowsPath
import stat
import statistics

MAX_FILES = 8
MAX_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_ROWS = 10000
MAX_COLUMNS = 64
MAX_GROUPS = 128
MAX_FINDINGS = 40
MAX_EXAMPLES = 3
MISSING = {'', 'null', 'none'}
# Keep comparison/time-series findings ahead of general column screens.
FINDING_PRIORITY = {'nonfinite_value': 1, 'mixed_numeric_text': 1, 'iqr_candidate': 2, 'missing_value': 2}
DISCLAIMER = ('Candidates require human confirmation; no domain error, normal-operation certification, '
              'precision, recall or equipment diagnosis is inferred. Confirm units, grouping, sampling '
              'interval, measurement precision and baseline suitability. Files are separate batches.')


def _require(condition, message):
    if not condition:
        raise ValueError('CSV intake: ' + message)


def _pairs(items):
    result = {}
    for key, value in items:
        _require(key not in result, 'duplicate configuration key: ' + key)
        result[key] = value
    return result


def _path(root, name, csv_only=False):
    _require(isinstance(name, str) and name.strip() and len(name) <= 512 and '\0' not in name, 'invalid input path')
    relative = Path(name)
    _require(not relative.anchor and not PureWindowsPath(name).anchor and '\\' not in name
             and '..' not in relative.parts and not any(':' in part for part in relative.parts),
             'input paths must stay inside the project: ' + name)
    _require(not set(relative.parts) & {'.harness', '.git', 'work', '.venv', 'venv', 'node_modules', '__pycache__'},
             'runtime or dependency directory is not a CSV input: ' + name)
    if csv_only:
        _require(relative.suffix.lower() == '.csv', 'configured data inputs must be CSV files: ' + name)
    path = root / relative
    _require(path.absolute() == path.resolve() and path.resolve().is_relative_to(root), 'redirected input: ' + name)
    for part in (path, *path.parents):
        if part == root:
            break
        _require(not part.is_symlink() and not (part.exists() and getattr(part.lstat(), 'st_file_attributes', 0) & 0x400),
                 'redirected input: ' + name)
    _require(path.is_file(), 'missing input: ' + name)
    info = path.lstat()
    _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'input must be an ordinary unlinked file: ' + name)
    return path


def _read(path):
    before = path.lstat()
    _require(before.st_size <= MAX_BYTES, 'file exceeds the 2 MiB intake limit: ' + path.name)
    raw = path.read_bytes()
    after = path.lstat()
    _require(len(raw) <= MAX_BYTES and all(getattr(before, key) == getattr(after, key)
             for key in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_mode'))
             and path.absolute() == path.resolve(), 'input changed while reading: ' + path.name)
    return raw


def _settings(root):
    root = Path(root).absolute()
    _require(root.is_dir() and root == root.resolve(), 'project root is missing or redirected')
    config_path = root / 'config.json'
    if not config_path.exists():
        return root, {}, None
    raw = _read(_path(root, 'config.json'))
    config = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_pairs)
    _require(isinstance(config, dict), 'config.json must be an object')
    names = config.get('data_files', [])
    _require(isinstance(names, list) and len(names) <= MAX_FILES, 'data_files must contain at most eight paths')
    csv_paths = [_path(root, name, True).relative_to(root).as_posix() for name in names]
    _require(len(csv_paths) == len(set(csv_paths)), 'duplicate CSV inputs')
    baseline = config.get('data_baseline')
    if baseline is not None:
        _require(csv_paths, 'data_baseline requires data_files')
        baseline = _path(root, baseline, True).relative_to(root).as_posix()
        _require(baseline not in csv_paths, 'baseline must be separate from monitored data')
    return root, {'data_files': csv_paths, 'data_baseline': baseline}, raw


def configured_files(root):
    """Return configured data and optional baseline paths; do not read CSV contents."""
    _, config, _ = _settings(root)
    return config.get('data_files', []) + ([config['data_baseline']] if config.get('data_baseline') else [])


def _parse_number(value):
    try:
        result = float(value)
    except (ValueError, OverflowError):
        return None
    return result


def _interpolate_quantile(values, fraction):
    index = (len(values) - 1) * fraction
    low = int(index)
    weight = index - low
    return values[low] * (1 - weight) + values[min(low + 1, len(values) - 1)] * weight


def _finding(findings, kind, name, examples, description, **extra):
    if examples:
        findings.append(dict(kind=kind, file=name, count=len(examples), examples=examples[:MAX_EXAMPLES],
                             description=description, **extra))


def _csv(raw, name, findings):
    try:
        reader = csv.reader(io.StringIO(raw.decode('utf-8-sig'), newline=''), strict=True)
        header = next(reader)
        _require(0 < len(header) <= MAX_COLUMNS and all(item.strip() == item and item and len(item) <= 100 for item in header),
                 'header must have 1..64 nonempty, trimmed names of at most 100 characters: ' + name)
        _require(len(header) == len(set(header)), 'duplicate CSV headers: ' + name)
        rows = []
        for row in reader:
            _require(len(rows) < MAX_ROWS, 'CSV exceeds the 10000-row intake limit: ' + name)
            _require(len(row) == len(header), 'CSV row width differs at record ' + str(len(rows) + 2) + ': ' + name)
            _require(all(len(value) <= 2048 and '\0' not in value for value in row), 'unsupported CSV cell size/content: ' + name)
            rows.append(row)
        _require(rows, 'CSV has no data rows: ' + name)
    except (UnicodeError, csv.Error, StopIteration) as exc:
        raise ValueError('CSV intake: invalid UTF-8 CSV ' + name + ': ' + str(exc)) from exc
    numeric_fields = []
    for index, column in enumerate(header):
        missing, nonfinite_cells, numeric_samples, text = [], [], [], []
        for offset, row in enumerate(rows, 2):
            value = row[index].strip()
            example = {'record': offset, 'value': value[:120]}
            if value.lower() in MISSING:
                missing.append(example)
            else:
                number = _parse_number(value)
                if number is None:
                    text.append(example)
                elif not math.isfinite(number):
                    nonfinite_cells.append(example)
                else:
                    numeric_samples.append((offset, number))
        _finding(findings, 'missing_value', name, missing, 'Blank/null/none values; confirm whether absence is permitted.', column=column)
        _finding(findings, 'nonfinite_value', name, nonfinite_cells, 'NaN or infinity cannot be treated as a finite measurement.', column=column)
        if numeric_samples and text:
            _finding(findings, 'mixed_numeric_text', name, text, 'Numeric and text values share a column; confirm its intended type.', column=column)
        if not numeric_samples or text:
            continue
        numeric_fields.append(column)
        if len(numeric_samples) < 4:
            continue
        values = sorted(value for _, value in numeric_samples)
        first_quartile, third_quartile = _interpolate_quantile(values, .25), _interpolate_quantile(values, .75)
        spread = third_quartile - first_quartile
        low, high = first_quartile - 1.5 * spread, third_quartile + 1.5 * spread
        if not all(math.isfinite(value) for value in (first_quartile, third_quartile, spread, low, high)):
            _finding(findings, 'calculation_unavailable', name, [{'column': column}], 'Numeric range is too large for a finite IQR calculation.')
            continue
        candidate_rows = [{'record': record, 'value': value} for record, value in numeric_samples if value < low or value > high]
        _finding(findings, 'iqr_candidate', name, candidate_rows,
                 'Exploratory 1.5-IQR screen over finite values in this file; not a domain threshold or confirmed error.',
                 column=column, lower=low, upper=high, finite_samples=len(numeric_samples))
    return header, rows, numeric_fields


def _timestamp(value):
    number = _parse_number(value)
    if number is not None:
        _require(math.isfinite(number), 'timestamp must be finite')
        return number, 'numeric_seconds'
    parsed_time = datetime.fromisoformat(value[:-1] + '+00:00' if value.endswith('Z') else value)
    if parsed_time.tzinfo is None or parsed_time.utcoffset() is None:
        # Subtraction avoids applying this machine's time zone to an unspecified one.
        return (parsed_time - datetime(1970, 1, 1)).total_seconds(), 'naive_iso'
    absolute_time = parsed_time.timestamp()
    _require(math.isfinite(absolute_time), 'timestamp must be finite')
    return absolute_time, 'offset_iso'


def _parse_time_series(header, rows, name, findings):
    if not {'timestamp', 'value'} <= set(header):
        return None
    records, issues, previous, time_axes = [], [], {}, set()
    for offset, values in enumerate(rows, 2):
        row = dict(zip(header, values))
        try:
            time, axis = _timestamp(row['timestamp'].strip())
            group, regime = row.get('group', 'default').strip(), row.get('regime', 'default').strip()
            _require(group and regime and len(group) <= 100 and len(regime) <= 100, 'group/regime is missing or too long')
            _require(group not in previous or time > previous[group], 'duplicate or reversed group timestamp')
            previous[group] = time
            time_axes.add(axis)
            _require(len(time_axes) == 1, 'mixed timestamp axes')
            raw = row['value'].strip()
            value = None if raw.lower() in MISSING else _parse_number(raw)
            _require(raw.lower() in MISSING or (value is not None and math.isfinite(value)), 'value is not a finite measurement')
            records.append({'record': offset, 'sample_time': time, 'value': value, 'partition_key': (group, regime)})
        except (ValueError, OverflowError, OSError) as exc:
            issues.append({'record': offset, 'reason': str(exc)[:180]})
    _finding(findings, 'timeseries_input_issue', name, issues,
             'Time/order/measurement issue; rows were not sorted or repaired. Fixed-baseline comparison is withheld for this file.')
    _require(len({row['partition_key'] for row in records}) <= MAX_GROUPS, 'too many group/regime combinations: ' + name)
    return {'parsed_rows': records, 'well_formed': not issues, 'clock_axis': next(iter(time_axes), None)}


def _baseline(series, name, findings):
    if series is None or not series['well_formed']:
        _finding(findings, 'baseline_unavailable', name, [{'reason': 'valid timestamp/value columns are required'}],
                 'Cannot establish a fixed time-series baseline; confirm input format and measurement contract.')
        return {}, {}
    groups, previous, baseline_tail = {}, {}, {}
    for row in series['parsed_rows']:
        key = row['partition_key']
        item = groups.setdefault(key, {'observations': [], 'consecutive_deltas': [], 'sample_intervals': []})
        previous_sample = previous.get(key[0])
        if row['value'] is not None:
            item['observations'].append(row['value'])
        if previous_sample and previous_sample['partition_key'] == key and row['value'] is not None and previous_sample['value'] is not None:
            item['consecutive_deltas'].append(abs(row['value'] - previous_sample['value']))
            item['sample_intervals'].append(row['sample_time'] - previous_sample['sample_time'])
        previous[key[0]] = row
        baseline_tail[key[0]] = row
    control_limits = {}
    for key, item in groups.items():
        reason = None
        if len(item['observations']) < 3 or not item['consecutive_deltas']:
            reason = 'fewer than three finite samples or no adjacent valid pair'
        elif not all(math.isfinite(gap) and gap > 0 for gap in item['sample_intervals']):
            reason = 'timestamp range prevents a finite positive sampling interval'
        elif not all(math.isclose(gap, item['sample_intervals'][0], rel_tol=1e-9, abs_tol=1e-9) for gap in item['sample_intervals']):
            reason = 'baseline spacing is irregular; sampling contract needs confirmation'
        else:
            try:
                mean = statistics.mean(item['observations'])
                mean_range = statistics.mean(item['consecutive_deltas'])
                low, high, range_limit = mean - 3 * mean_range / 1.128, mean + 3 * mean_range / 1.128, 3.267 * mean_range
                if not all(math.isfinite(value) for value in (mean, mean_range, low, high, range_limit)) or mean_range <= 0:
                    reason = 'zero variation or nonfinite limits; no normality conclusion is possible'
                else:
                    control_limits[key] = {'lower': low, 'upper': high, 'range_ceiling': range_limit, 'cadence_seconds': item['sample_intervals'][0],
                                   'baseline_samples': len(item['observations'])}
            except (OverflowError, ValueError):
                reason = 'numeric range prevents finite baseline limits'
        if reason:
            _finding(findings, 'baseline_unavailable', name, [{'group': key[0], 'regime': key[1], 'reason': reason}],
                     'Fixed I/MR comparison is unavailable for this group; this is not a normal result.')
    return control_limits, baseline_tail


def _compare_to_baseline(series, name, baseline_series, control_limits, baseline_tail, findings):
    if series is None or not series['well_formed'] or baseline_series is None or not baseline_series['well_formed']:
        _finding(findings, 'comparison_unavailable', name, [{'reason': 'valid timestamp/value data and baseline are required'}],
                 'Only general CSV checks were possible; no fixed-baseline comparison was made.')
        return
    if series['clock_axis'] != baseline_series['clock_axis']:
        _finding(findings, 'comparison_unavailable', name, [{'reason': 'baseline and data use different time axes'}],
                 'Numeric seconds, offset timestamps and timestamps without offsets require an agreed conversion before mixing.')
        return
    previous = dict(baseline_tail)
    events = {'baseline_overlap': [], 'missing_baseline': [], 'i_limit_candidate': [], 'mr_limit_candidate': [],
              'sampling_gap': [], 'calculation_unavailable': []}
    for row in series['parsed_rows']:
        key, value = row['partition_key'], row['value']
        example = {'record': row['record'], 'group': key[0], 'regime': key[1]}
        previous_sample = previous.get(key[0])
        if key[0] in baseline_tail and row['sample_time'] <= baseline_tail[key[0]]['sample_time']:
            events['baseline_overlap'].append(example)
            continue
        previous[key[0]] = row
        model = control_limits.get(key)
        if model is None:
            events['missing_baseline'].append(example)
            continue
        if value is None:
            continue
        if value < model['lower'] or value > model['upper']:
            events['i_limit_candidate'].append(dict(example, value=value, lower=model['lower'], upper=model['upper']))
        if previous_sample and previous_sample['partition_key'] == key and previous_sample['value'] is not None:
            if not math.isclose(row['sample_time'] - previous_sample['sample_time'], model['cadence_seconds'], rel_tol=1e-9, abs_tol=1e-9):
                events['sampling_gap'].append(example)
            else:
                moving_range = abs(value - previous_sample['value'])
                if not math.isfinite(moving_range):
                    events['calculation_unavailable'].append(example)
                elif moving_range > model['range_ceiling']:
                    events['mr_limit_candidate'].append(dict(example, moving_range=moving_range, upper=model['range_ceiling']))
    descriptions = {
        'baseline_overlap': 'Monitoring must be after the entire baseline period of its group; overlapping rows were not compared.',
        'missing_baseline': 'No usable explicitly supplied baseline exists for this group/regime; do not substitute an overall average.',
        'i_limit_candidate': 'Candidate outside baseline-only mean +/- 3*MRbar/1.128; baseline suitability remains unconfirmed.',
        'mr_limit_candidate': 'Candidate above baseline-only 3.267*MRbar, using consecutive matching group/regime and observed baseline spacing.',
        'sampling_gap': 'Observed spacing differs from baseline spacing; MR comparison is withheld across this boundary.',
        'calculation_unavailable': 'Numeric range prevents a finite moving-range calculation; confirm measurement scale.',
    }
    for kind, examples in events.items():
        _finding(findings, kind, name, examples, descriptions[kind])


def inspect(root):
    """Read all configured CSVs consistently and return a bounded deterministic summary."""
    root, config, settings_bytes = _settings(root)
    if not config.get('data_files'):
        return None
    names = config['data_files'] + ([config['data_baseline']] if config.get('data_baseline') else [])
    findings, files, data, file_payloads = [], [], {}, {}
    total = 0
    for name in names:
        raw = _read(_path(root, name, True))
        total += len(raw)
        _require(total <= MAX_TOTAL_BYTES, 'combined CSV size exceeds the 8 MiB intake limit')
        file_payloads[name] = raw
        header, rows, numeric_fields = _csv(raw, name, findings)
        data[name] = _parse_time_series(header, rows, name, findings)
        files.append({'file': name, 'role': 'baseline' if name == config.get('data_baseline') else 'data',
                      'rows': len(rows), 'columns': header, 'numeric_columns': numeric_fields, 'bytes': len(raw)})
    if config.get('data_baseline'):
        baseline_path = config['data_baseline']
        control_limits, baseline_tail = _baseline(data[baseline_path], baseline_path, findings)
        for name in config['data_files']:
            _compare_to_baseline(data[name], name, data[baseline_path], control_limits, baseline_tail, findings)
    for name, raw in file_payloads.items():
        _require(_read(_path(root, name, True)) == raw, 'input changed during inspection: ' + name)
    _require(_read(_path(root, 'config.json')) == settings_bytes, 'configuration changed during inspection')
    findings.sort(key=lambda item: FINDING_PRIORITY.get(item['kind'], 0))
    return {'version': 1, 'status': 'review_required' if findings else 'no_candidates',
            'input_hashes': {name: hashlib.sha256(file_payloads[name]).hexdigest() for name in sorted(names)},
            'files': files, 'findings': findings[:MAX_FINDINGS],
            'findings_omitted_count': max(0, len(findings) - MAX_FINDINGS), 'disclaimer': DISCLAIMER}
