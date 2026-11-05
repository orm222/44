#!/usr/bin/env python3
"""Prepare and explicitly confirm requirements and a functional specification."""
import importlib.util
import argparse
from pathlib import Path
import sys

sys.dont_write_bytecode = True
_path = Path(__file__).resolve().parent / '.harness' / 'requirements.py'
_spec = importlib.util.spec_from_file_location('document_harness', _path)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)


def main(arguments=None, default_root=None):
    if sys.version_info[:2] < (3, 9):
        print('Harness requires Python 3.9 or later', file=sys.stderr)
        return 2
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    root = str(Path(default_root) if default_root is not None else Path(__file__).resolve().parent)
    command = arguments.pop(0) if arguments and arguments[0] in ('start', 'time-status', 'requirements', 'develop', 'run') else 'requirements'
    if command in ('start', 'time-status'):
        parser = argparse.ArgumentParser(description='Start or inspect the original 150-minute problem clock.')
        parser.add_argument('--root', default=root)
        if command == 'start':
            parser.add_argument('--received-at')
        args = parser.parse_args(arguments)
        forwarded = ['--root', args.root, 'start' if command == 'start' else 'status']
        if command == 'start' and args.received_at is not None:
            forwarded += ['--received-at', args.received_at]
        return _module.problem_time.main(forwarded)
    if command == 'develop':
        spec = importlib.util.spec_from_file_location('development_launcher', _path.with_name('development.py'))
        development = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(development)
        return development.main(arguments, default_root=root)
    if command == 'run':
        if any(flag in arguments for flag in ('--approve', '--refresh-inputs', '--app-root', '--request-change')):
            return main(['develop'] + arguments, default_root=root)
        result = _module.main(['--root', root] + arguments)
        if result != 0 or '--status' in arguments:
            return result
        forwarded = []
        for flag in ('--root', '--model', '--timeout'):
            if flag in arguments:
                index = arguments.index(flag)
                forwarded += arguments[index:index + 2]
        return main(['develop'] + forwarded, default_root=root)
    if arguments in (['-h'], ['--help']):
        print('Commands: start, time-status, requirements (documents only), develop, run (documents through delivery).')
    return _module.main(['--root', root] + arguments)


if __name__ == '__main__':
    sys.exit(main())
