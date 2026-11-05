#!/usr/bin/env python3
"""Run the document harness against the included, separate problem project."""
import importlib.util
from pathlib import Path
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('intake_launcher', ROOT / 'harness-kit/run_harness.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)

if __name__ == '__main__':
    sys.exit(launcher.main(default_root=ROOT / 'problem-project'))
