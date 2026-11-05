"""Resolve the model CLI without invoking shell wrapper scripts."""
import os
from pathlib import Path
import shutil


def resolve_command(argv):
    if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) or not arg.strip() for arg in argv):
        raise ValueError("Invalid codex_command")
    executable = shutil.which(argv[0])
    if not executable:
        raise ValueError("Codex CLI not found; install it and sign in before running the harness")
    if os.name == "nt" and Path(executable).suffix.lower() in (".cmd", ".bat"):
        entry = Path(executable).parent / "node_modules/@openai/codex/bin/codex.js"
        node = shutil.which("node.exe")
        if Path(executable).stem.lower() != "codex" or not node or not entry.is_file():
            raise ValueError("Use native codex.exe or npm Codex with node.exe; unknown batch wrappers are not executed")
        return [node, str(entry)] + argv[1:]
    return [executable] + argv[1:]
