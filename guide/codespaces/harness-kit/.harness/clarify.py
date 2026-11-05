#!/usr/bin/env python3
"""Ask boundary questions before implementation; never invent a human answer."""
import argparse
import base64
import contextlib
import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode = True
# Resolve helpers from this installed harness, also when imported by a document stage.
_helper_dir = Path(__file__).resolve().parent
_boundary_spec = importlib.util.spec_from_file_location('clarify_boundary', _helper_dir / 'boundary.py')
boundary = importlib.util.module_from_spec(_boundary_spec)
_boundary_spec.loader.exec_module(boundary)
def _helper(name):
    spec = importlib.util.spec_from_file_location('clarify_' + name, _helper_dir / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

operation_lock, lifecycle = _helper('operation_lock'), _helper('lifecycle')
processes, codex_protocol = _helper('processes'), _helper('codex_protocol')
start_process, stop_process = processes.start_process, processes.stop_process
_time_spec = importlib.util.spec_from_file_location('clarify_problem_time', _helper_dir / 'problem_time.py')
problem_time = importlib.util.module_from_spec(_time_spec)
_time_spec.loader.exec_module(problem_time)

BASE = Path(__file__).resolve().parent
ANALYSIS_PENDING = ".harness/work/analysis-pending.json"
ANSWER_PENDING = ".harness/work/answer-publication.json"
ANSWER_DIRECTORY = ".harness/work/answer-publication"
ANALYSIS_FILES = {"prompt.txt", "answer.json", "model.input.txt", "model.command.json",
                  "model.launch.json", "model.running.json", "model.exit.json",
                  "model.stdout.log", "model.stderr.log", "analysis-transaction.json", "result-receipt.json"}
ANALYSIS_TEMP_FILES = {"prompt.txt.tmp", "analysis-transaction.json.tmp", "analysis-pending.json.tmp",
                       "boundary.json.tmp", "BOUNDARY_ANSWERS.md.tmp", "result-receipt.json.tmp"}


def configuration(root, name):
    if name is None:
        path = root / "config.json"
        if not path.exists():
            return {}, None
        name = path.relative_to(root).as_posix()
    candidate = Path(name)
    path = candidate if candidate.is_absolute() else root / candidate
    boundary.require(path.is_file() and path.absolute() == path.resolve() and not path.is_symlink(),
                     "Clarify config must be an ordinary file without redirected paths")
    value = boundary.decode(path.read_text(encoding="utf-8-sig"))
    boundary.require(isinstance(value, dict), "Clarify config must be an object")
    return value, path


def selected_spec(root, explicit, config):
    value = explicit or config.get("spec")
    if value is None and (root / boundary.STATE).exists():
        answer = _load_answer(root)
        pending = _load_analysis(root)
        if answer and answer["stage"] != "completed":
            value = answer["spec"]
        else:
            value = pending["spec"] if pending and pending["stage"] != "completed" else boundary.load_state(root, current=False)["spec"]
    boundary.require(boundary.text(value), "Supply --spec or config.spec before clarification")
    return boundary.project_path(root, value).relative_to(root).as_posix()


def source_snapshot(root, owned_directory=None):
    root = Path(root).absolute()
    boundary.require(root.is_dir() and root == root.resolve(), "Redirected project root")
    files = {}
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode")

    def error(exc):
        raise boundary.BoundaryError("Cannot enumerate clarification source: " + str(exc)) from exc

    def entry(path):
        boundary.require(path.parent.absolute() == path.parent.resolve(), "Redirected project file parent: " + str(path))
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode):
            target = os.readlink(str(path))
            after = path.lstat()
            boundary.require(stat.S_ISLNK(after.st_mode) and os.readlink(str(path)) == target
                             and all(getattr(before, key) == getattr(after, key) for key in fields),
                             "Project link changed during clarification snapshot: " + str(path))
            return {"kind": "symlink", "target": target, "mode": stat.S_IMODE(after.st_mode),
                    "sha256": boundary.digest(os.fsencode(target))}
        boundary.require(not getattr(before, "st_file_attributes", 0) & 0x400
                         and stat.S_ISREG(before.st_mode) and path.absolute() == path.resolve(),
                         "Special or redirected project file: " + str(path))
        data = path.read_bytes()
        after = path.lstat()
        boundary.require(all(getattr(before, key) == getattr(after, key) for key in fields)
                         and not getattr(after, "st_file_attributes", 0) & 0x400,
                         "Project file changed during clarification snapshot: " + str(path))
        return [stat.S_IMODE(after.st_mode), boundary.digest(data)]

    for directory, dirs, names in os.walk(str(root), followlinks=False, onerror=error):
        relative = Path(directory).relative_to(root)
        kept = []
        for name in sorted(dirs):
            path = Path(directory) / name
            rel = (relative / name).as_posix()
            if rel == ".git" or rel == owned_directory:
                continue
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                files[rel] = entry(path)
                continue
            boundary.require(stat.S_ISDIR(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400
                             and path.resolve() == path.absolute(), "Redirected project directory: " + rel)
            kept.append(name)
        dirs[:] = kept
        for name in sorted(names):
            path = Path(directory) / name
            rel = (relative / name).as_posix()
            if rel in (".git", boundary.STATE, boundary.ANSWERS, ANALYSIS_PENDING, ".harness/work/operation.lock"):
                continue
            files[rel] = entry(path)
    return files


def _analysis_path(root, name, exists=True):
    return boundary.project_path(root, name, exists=exists, runtime=True)


def _encoded(data):
    return {"sha256": boundary.digest(data), "base64": base64.b64encode(data).decode("ascii")}


def _decoded(value):
    boundary.require(isinstance(value, dict) and set(value) == {"sha256", "base64"}, "Invalid analysis bytes")
    data = base64.b64decode(value["base64"], validate=True)
    boundary.require(boundary.digest(data) == value["sha256"], "Analysis byte hash differs")
    return data


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _atomic_runtime(path, data, temporary=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = temporary or path.with_name(path.name + ".tmp")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    boundary.require(not temporary.is_symlink() and temporary.absolute() == temporary.resolve(), "Redirected analysis temporary file")
    if temporary.exists():
        boundary.require(data.startswith(temporary.read_bytes()), "Unrelated analysis temporary bytes")
    try:
        with temporary.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
        if os.name != "nt":
            descriptor = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()


def _save_analysis(root, value):
    # boundary.atomic_write intentionally rejects runtime destinations.
    temporary = _analysis_path(root, value["directory"] + "/analysis-pending.json.tmp", exists=False)
    record = dict(value, _integrity=boundary.json_digest(value))
    _atomic_runtime(_analysis_path(root, ANALYSIS_PENDING, exists=False), _json_bytes(record), temporary)


def _load_analysis(root):
    path = _analysis_path(root, ANALYSIS_PENDING, exists=False)
    if not path.exists():
        return None
    value = boundary.decode(path.read_text(encoding="utf-8"))
    boundary.require(isinstance(value, dict), "Invalid analysis journal")
    integrity = value.pop("_integrity", None)
    boundary.require(integrity == boundary.json_digest(value), "Analysis journal checksum differs")
    boundary.require(isinstance(value, dict) and value.get("version") == 1
                     and value.get("project") == str(Path(root).resolve()), "Analysis belongs to another project")
    boundary.require(value.get("stage") in ("starting", "running", "publishing", "completed"), "Invalid analysis phase")
    boundary.require(isinstance(value.get("directory"), str) and re.fullmatch(
        r"\.harness/work/clarify-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}", value["directory"]), "Invalid analysis directory")
    _analysis_path(root, value["directory"] + "/answer.json", exists=False)
    return value


def _runtime_files(root, journal):
    folder = _analysis_path(root, journal["directory"] + "/answer.json", exists=False).parent
    result = {}
    if folder.exists():
        for path in sorted(folder.rglob("*")):
            boundary.require(not path.is_symlink() and path.absolute() == path.resolve(), "Redirected analysis evidence")
            if path.is_dir():
                boundary.require(path.relative_to(folder).as_posix() == "model.pycache", "Unexpected analysis directory")
                continue
            relative = path.relative_to(folder).as_posix()
            boundary.require(relative in ANALYSIS_FILES | ANALYSIS_TEMP_FILES and stat.S_ISREG(path.stat().st_mode), "Unexpected analysis evidence file")
            if relative in ANALYSIS_TEMP_FILES:
                continue
            if relative != "analysis-transaction.json":
                result[path.relative_to(root).as_posix()] = boundary.digest(path.read_bytes())
    return result


def list_analysis_files(root):
    """Return only this journal's owned files; sealed runtime edits are rejected."""
    journal = _load_analysis(root)
    if journal is None:
        return []
    current = _runtime_files(root, journal)
    if journal["stage"] in ("publishing", "completed"):
        boundary.require(current == journal["evidence"], "Analysis execution evidence changed")
    result = [ANALYSIS_PENDING] + list(current)
    for name in ANALYSIS_TEMP_FILES:
        relative = journal["directory"] + "/" + name
        if _analysis_path(root, relative, exists=False).exists():
            result.append(relative)
    archive = journal["directory"] + "/analysis-transaction.json"
    path = _analysis_path(root, archive, exists=False)
    if path.exists():
        completed = dict(journal, stage="completed")
        boundary.require(journal["stage"] in ("publishing", "completed") and path.read_bytes() == _json_bytes(completed), "Analysis history differs")
        result.append(archive)
    return result


def _boundary_bytes(root):
    result = {}
    for name in (boundary.STATE, boundary.ANSWERS):
        path = boundary.project_path(root, name, exists=False)
        result[name] = _encoded(path.read_bytes()) if path.exists() else None
    return result


def _target_bytes(state):
    return {boundary.STATE: _encoded(_json_bytes(state)),
            boundary.ANSWERS: _encoded(boundary.render_answers(state).encode("utf-8"))}


def _load_answer(root):
    path = _analysis_path(root, ANSWER_PENDING, exists=False)
    if not path.exists():
        return None
    value = boundary.decode(path.read_text(encoding="utf-8"))
    boundary.require(isinstance(value, dict), "Invalid actual-answer journal")
    integrity = value.pop("_integrity", None)
    boundary.require(integrity == boundary.json_digest(value), "Actual-answer journal checksum differs")
    keys = {"version", "project", "spec", "stage", "question_id", "answer_text", "before", "target", "target_files"}
    boundary.require(set(value) == keys and type(value["version"]) is int and value["version"] == 1
                     and value["project"] == str(Path(root).resolve())
                     and value["stage"] in ("publishing", "completed"), "Invalid actual-answer journal context")
    return value


def _save_answer(root, journal):
    record = dict(journal, _integrity=boundary.json_digest(journal))
    _atomic_runtime(_analysis_path(root, ANSWER_PENDING, exists=False), _json_bytes(record),
                    _analysis_path(root, ANSWER_DIRECTORY + "/intent.json.tmp", exists=False))


def _answer_target(root, previous, question_id, answer_text):
    boundary.validate_state(root, previous, current=False, document=False)
    boundary.require(question_id in {q["id"] for q in previous["questions"]}, "Unknown boundary question: " + str(question_id))
    boundary.require(boundary.text(answer_text), "Actual answer cannot be blank")
    target = copy.deepcopy(previous)
    target["answers"].append({"question_id": question_id, "text": answer_text})
    target.update(status="needs_analysis", analysis=None, error=None)
    target["answers_document_sha256"] = boundary.digest(boundary.render_answers(target).encode("utf-8"))
    return boundary.validate_state(root, target, current=False, document=False)


def recover_answer(root, spec=None):
    """Finish only a journaled actual answer; caller holds the operation lock."""
    journal = _load_answer(root)
    if journal is None:
        return None
    boundary.require(isinstance(journal["before"], dict)
                     and set(journal["before"]) == {boundary.STATE, boundary.ANSWERS}, "Invalid actual-answer original files")
    previous = boundary.decode(_decoded(journal["before"][boundary.STATE]).decode("utf-8-sig"))
    boundary.require(isinstance(previous, dict) and previous.get("spec") == journal["spec"], "Actual-answer specification differs")
    target = _answer_target(root, previous, journal["question_id"], journal["answer_text"])
    boundary.require(_decoded(journal["before"][boundary.ANSWERS]) == boundary.render_answers(previous).encode("utf-8")
                     and target == journal["target"] and _target_bytes(target) == journal["target_files"],
                     "Actual-answer target differs from the submitted text")
    if journal["stage"] == "completed":
        return journal
    boundary.require(spec is None or spec == journal["spec"], "Pending actual answer belongs to another specification")
    current = _boundary_bytes(root)
    for name in (boundary.ANSWERS, boundary.STATE):
        boundary.require(current[name] in (journal["before"][name], journal["target_files"][name]),
                         "Unrelated boundary change during actual-answer recovery: " + name)
    for name in (boundary.ANSWERS, boundary.STATE):
        if current[name] != journal["target_files"][name]:
            path = boundary.project_path(root, name)
            boundary.require(_encoded(path.read_bytes()) in (journal["before"][name], journal["target_files"][name]),
                             "Boundary changed during actual-answer publication: " + name)
            _atomic_runtime(path, _decoded(journal["target_files"][name]),
                            _analysis_path(root, ANSWER_DIRECTORY + "/" + Path(name).name + ".tmp", exists=False))
    boundary.require(_boundary_bytes(root) == journal["target_files"], "Actual-answer publication is incomplete")
    journal["stage"] = "completed"
    _save_answer(root, journal)
    return journal


def submit_answer(root, spec, question_id, answer_text):
    """Journal before publishing, and replay an unchanged submission only once."""
    journal = recover_answer(root, spec)
    if journal and (journal["spec"], journal["question_id"], journal["answer_text"]) == (spec, question_id, answer_text) \
            and _boundary_bytes(root) == journal["target_files"]:
        return boundary.load_state(root, current=False)
    previous = boundary.load_state(root, current=False)
    boundary.require(previous["spec"] == spec, "Answer belongs to a different specification; reanalyze first")
    before = _boundary_bytes(root)
    boundary.require(boundary.decode(_decoded(before[boundary.STATE]).decode("utf-8-sig")) == previous
                     and _decoded(before[boundary.ANSWERS]) == boundary.render_answers(previous).encode("utf-8"),
                     "Boundary changed while preparing actual answer")
    target = _answer_target(root, previous, question_id, answer_text)
    journal = {"version": 1, "project": str(Path(root).resolve()), "spec": spec, "stage": "publishing",
               "question_id": question_id, "answer_text": answer_text, "before": before,
               "target": target, "target_files": _target_bytes(target)}
    _save_answer(root, journal)
    recover_answer(root, spec)
    return boundary.load_state(root, current=False)


def _publish_boundary(root, before, target, target_files):
    boundary.validate_state(root, target, current=False, document=False)
    boundary.require(_target_bytes(target) == target_files, "Analysis target bytes differ")
    current = _boundary_bytes(root)
    for name in (boundary.STATE, boundary.ANSWERS):
        if before[name] is not None:
            _decoded(before[name])
        _decoded(target_files[name])
        boundary.require(current[name] in (before[name], target_files[name]), "Unrelated boundary change during analysis: " + name)
    if current != target_files:
        journal = _load_analysis(root)
        for name in (boundary.ANSWERS, boundary.STATE):
            temporary = _analysis_path(root, journal["directory"] + "/" + Path(name).name + ".tmp", exists=False)
            _atomic_runtime(boundary.project_path(root, name, exists=False), _decoded(target_files[name]), temporary)
    boundary.require(_boundary_bytes(root) == target_files, "Analysis publication is incomplete")


def _execution_result(root, journal, decode_answer=True):
    folder = _analysis_path(root, journal["directory"] + "/answer.json", exists=False).parent
    def read(name):
        path = _analysis_path(root, journal["directory"] + "/" + name)
        return boundary.decode(path.read_text(encoding="utf-8-sig"))
    outcome, command = read("model.exit.json"), read("model.command.json")
    launch, running = read("model.launch.json"), read("model.running.json")
    boundary.require(type(outcome.get("exit_code")) is int and outcome["exit_code"] == 0
                     and outcome.get("termination_reason") == "completed" and outcome.get("finished_at")
                     and outcome.get("cleanup", {}).get("completed") is True,
                     "Analysis has no successful completed execution and cleanup evidence")
    boundary.require(command.get("argv") == journal["argv"] and command.get("cwd") == str(root)
                     and command.get("timeout") == journal["timeout"]
                     and launch.get("started_at") == running.get("started_at") == outcome.get("started_at")
                     and type(running.get("pid")) is int and running["pid"] > 0,
                     "Analysis execution does not match its saved invocation")
    prompt = (folder / "prompt.txt").read_bytes()
    boundary.require(boundary.digest(prompt) == journal["prompt_sha256"]
                     and (folder / "model.input.txt").read_text(encoding="utf-8") == prompt.decode("utf-8"), "Analysis prompt changed")
    receipt_bytes = _analysis_path(root, journal["directory"] + "/result-receipt.json").read_bytes()
    boundary.require(boundary.digest(receipt_bytes) == journal.get("result_receipt_sha256"), "Analysis result receipt changed or was not saved")
    receipt = boundary.decode(receipt_bytes.decode("utf-8"))
    boundary.require(receipt == {"answer_sha256": boundary.digest((folder / "answer.json").read_bytes()),
                                "invocation_sha256": boundary.json_digest({"argv": journal["argv"], "context": journal["context"],
                                    "source_files": journal["source_files"], "start_files": journal["start_files"],
                                    "prompt_sha256": journal["prompt_sha256"]})}, "Analysis response changed or belongs to another invocation")
    return read("answer.json") if decode_answer else (folder / "answer.json").read_bytes()


def _finish_analysis(root, journal, deadline_check=None):
    list_analysis_files(root)
    boundary.require(_target_bytes(journal["start_state"]) == journal["start_files"], "Analysis starting state differs from its saved bytes")
    if journal.get("report") is not None:
        boundary.require(source_snapshot(root, journal["directory"]) == journal["source_files"], "Source changed during analysis recovery")
        boundary.require(boundary.analysis_state(root, journal["start_state"], journal["report"]) == journal["target"], "Analysis target differs from validated response")
        boundary.require(_execution_result(root, journal) == journal["report"], "Analysis response differs from its execution evidence")
    if deadline_check is not None:
        deadline_check()
    _publish_boundary(root, journal["start_files"], journal["target"], journal["target_files"])
    journal["stage"] = "completed"
    archive = _analysis_path(root, journal["directory"] + "/analysis-transaction.json", exists=False)
    _atomic_runtime(archive, _json_bytes(journal))
    _save_analysis(root, journal)
    return boundary.load_state(root, current=journal.get("report") is not None)


def _prepare_result(root, journal):
    boundary.require(source_snapshot(root, journal["directory"]) == journal["source_files"], "Source changed during analysis recovery")
    boundary.require(_boundary_bytes(root) == journal["start_files"], "Boundary changed during analysis execution")
    evidence = _runtime_files(root, journal)
    report = _execution_result(root, journal)
    target = boundary.analysis_state(root, journal["start_state"], report)
    boundary.require(evidence == _runtime_files(root, journal), "Analysis evidence changed while validating")
    journal.update(stage="publishing", report=report, target=target, target_files=_target_bytes(target), evidence=evidence)
    _save_analysis(root, journal)


def _fail_analysis(root, journal, error):
    target = copy.deepcopy(journal["start_state"])
    target.update(status="failed", analysis=None, error=str(error) or type(error).__name__)
    journal.update(stage="publishing", report=None, target=target, target_files=_target_bytes(target),
                   evidence=_runtime_files(root, journal))
    _save_analysis(root, journal)
    return _finish_analysis(root, journal)


def recover_analysis(root, spec=None, analysis_context=None, deadline_check=None):
    """Recover a bound, validated result before callers load boundary files.

    The caller holds the project operation lock. Unconfirmed running processes
    are never replayed, and a raw response alone is never completion evidence.
    """
    root = Path(root).resolve()
    journal = _load_analysis(root)
    if journal is None:
        return None
    list_analysis_files(root)
    if journal["stage"] == "completed":
        if analysis_context is not None and journal["context"] == analysis_context and journal.get("report") is not None:
            boundary.require(spec is None or spec == journal["spec"], "Completed analysis specification differs")
            boundary.require(source_snapshot(root, journal["directory"]) == journal["source_files"]
                             and _boundary_bytes(root) == journal["target_files"], "Completed analysis inputs changed")
            return boundary.load_state(root)
        return None
    boundary.require((spec is None or spec == journal["spec"]) and analysis_context == journal["context"], "Pending analysis belongs to another specification or request")
    boundary.require(source_snapshot(root, journal["directory"]) == journal["source_files"], "Source changed during analysis recovery")
    if journal["stage"] == "starting":
        _publish_boundary(root, journal["before"], journal["start_state"], journal["start_files"])
        journal["stage"] = "running"
        _save_analysis(root, journal)
    if journal["stage"] == "running":
        folder = _analysis_path(root, journal["directory"] + "/answer.json", exists=False).parent
        if not (folder / "model.exit.json").exists():
            boundary.require(not (folder / "model.launch.json").exists(), "Analysis process cleanup is unconfirmed; preserve evidence before resuming")
            # Keep ownership until this same unstarted invocation is resumed.
            # Deleting the pointer would turn its prompt into an unrelated edit.
            _prelaunch_files(root, journal)
            return None
        _prepare_result(root, journal)
    return _finish_analysis(root, journal, deadline_check=deadline_check)



def _prelaunch_files(root, journal, cleanup=False):
    """Only an invocation without a published launch marker can be restarted."""
    boundary.require(_boundary_bytes(root) == journal['start_files'], 'Boundary changed before analysis launch')
    folder = _analysis_path(root, journal['directory'] + '/answer.json', exists=False).parent
    current = _runtime_files(root, journal)
    allowed = {'prompt.txt', 'model.input.txt', 'model.command.json', 'model.stdout.log', 'model.stderr.log'}
    boundary.require(all(Path(name).name in allowed for name in current), 'Unexpected evidence before analysis launch')
    prompt = journal.get('prompt')
    if prompt is None and (folder / 'prompt.txt').exists():
        prompt = (folder / 'prompt.txt').read_bytes().decode('utf-8')
    boundary.require(isinstance(prompt, str) and boundary.digest(prompt.encode('utf-8')) == journal['prompt_sha256'],
                     'Saved prelaunch analysis prompt differs')
    for name in ('prompt.txt', 'model.input.txt'):
        path = folder / name
        if path.exists():
            actual = path.read_bytes()
            expected = prompt.encode('utf-8') if name == 'prompt.txt' else prompt.replace('\n', os.linesep).encode('utf-8')
            boundary.require(expected.startswith(actual), 'Unexpected prelaunch prompt/input bytes')
    for name in ('model.stdout.log', 'model.stderr.log'):
        path = folder / name
        boundary.require(not path.exists() or not path.read_bytes(), 'Worker output exists without a launch marker')
    command = folder / 'model.command.json'
    if command.exists():
        value = boundary.decode(command.read_text(encoding='utf-8'))
        boundary.require(value == {'argv': journal['argv'], 'cwd': str(root), 'timeout': journal['timeout'],
            'python_environment': {'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPYCACHEPREFIX': str((folder / 'model.pycache').resolve())}},
            'Saved prelaunch command differs')
    pycache = folder / 'model.pycache'
    if pycache.exists():
        boundary.require(pycache.is_dir() and not pycache.is_symlink() and pycache.absolute() == pycache.resolve()
                         and not getattr(pycache.lstat(), 'st_file_attributes', 0) & 0x400
                         and not any(pycache.iterdir()), 'Unexpected prelaunch cache contents')
        if cleanup:
            pycache.rmdir()  # The operation lock owns this verified empty runtime directory.
    return prompt


def codex_command(config):
    try:
        return codex_protocol.resolve_command(config.get("codex_command", ["codex"]))
    except ValueError as exc:
        raise boundary.BoundaryError(str(exc)) from exc


@contextlib.contextmanager
def state_lock(root, allow_nested=False):
    try:
        with operation_lock.operation_lock(root, "clarify", allow_nested=allow_nested):
            yield
    except operation_lock.OperationBusy as exc:
        raise boundary.BoundaryError(str(exc)) from exc


def show(state):
    print("Boundary status: " + state["status"])
    answers = boundary.actual_answers(state)
    for question in state["questions"]:
        if question["id"] not in answers:
            print("\n[{} | {}]\n질문: {}\n확정할 내용: {}".format(
                question["id"], question["template"], question["question"], question["decides"]))
    if state["status"] == "needs_analysis":
        print("Actual answers were recorded. Run clarify again to check whether anything remains ambiguous.")
    if state["error"]:
        print("Clarification needs attention: " + state["error"])


def analyze(root, spec, config, model, timeout, allow_nested=False, analysis_context=None, *, budget_root=None):
    boundary.require(boundary.text(model) and not model.startswith("SET_"), "Supply an actual model ID with --model or config.model")
    boundary.require(type(timeout) is int and timeout > 0, "Clarify timeout must be a positive integer")
    budget_root = root if budget_root is None else budget_root
    clock_required = problem_time.required(config)
    with state_lock(root, allow_nested=allow_nested):
        interview = boundary.interview_reference(root, spec)
        required = boundary.required_references(root, spec)
        if "interview" in config:
            configured = boundary.project_path(root, config["interview"]).relative_to(Path(root).resolve()).as_posix()
            boundary.require(configured == interview,
                             "Set config.interview in the project config.json before using an alternate config")
        if "data_files" in config or "data_baseline" in config:
            project_config = boundary.project_configuration(root)
            boundary.require(all(config.get(key) == project_config.get(key) for key in ("data_files", "data_baseline")),
                             "Set data inputs in the project config.json before using an alternate config")
        recover_answer(root, spec)
        clock_required = problem_time.load(budget_root, required=clock_required) is not None
        timeout = problem_time.budget(budget_root, timeout, required=clock_required)
        def deadline_check():
            return problem_time.budget(budget_root, timeout, required=clock_required)
        recovered = recover_analysis(root, spec, analysis_context, deadline_check=deadline_check)
        if recovered is not None:
            problem_time.budget(budget_root, timeout, required=clock_required)
            show(recovered)
            return 0 if recovered["status"] == "ready" else 2
        state = (boundary.load_state(root, current=False) if (root / boundary.STATE).exists()
                 else boundary.create_state(root, spec))
        unanswered = {q["id"] for q in state["questions"]} - set(boundary.actual_answers(state))
        if unanswered:
            show(state)
            raise boundary.BoundaryError("Submit actual answers for existing questions before reanalysis: " + ", ".join(sorted(unanswered)))
        before = _boundary_bytes(root)
        state["spec"] = spec
        # Start each analysis unready. Failure cannot leave an older ready decision usable.
        state["status"], state["analysis"], state["error"] = "analyzing", None, None
        state["references"] = [name for name in state["references"] if (root / name).is_file()]
        for name in required:
            if name not in state["references"]:
                state["references"].append(name)
        state["input_hashes"] = boundary.input_hashes(root, spec, state["references"])
        state["answers_document_sha256"] = boundary.digest(boundary.render_answers(state).encode("utf-8"))
        boundary.validate_state(root, state, current=True, document=False)
        command = codex_command(config)
        pending = _load_analysis(root)
        prelaunch = pending if pending and pending['stage'] == 'running' else None
        run_id = "clarify-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
        relative_directory = prelaunch['directory'] if prelaunch else ".harness/work/" + run_id
        answer = _analysis_path(root, relative_directory + "/answer.json", exists=False)
        context = {"spec": spec, "template": boundary.TEMPLATE, "previous_questions": state["questions"],
                   "actual_answers": state["answers"], "previous_references": state["references"]}
        if interview is not None:
            context["interview"] = interview
        summary = boundary.data_context(root, state["input_hashes"])
        if summary is not None:
            context["data_checks"] = summary
        prompt = (BASE / "clarify_prompt.md").read_text(encoding="utf-8")
        prompt += "\n\nBOUNDARY_CLARIFICATION_CONTEXT\n" + json.dumps(context, ensure_ascii=False)
        argv = command + ["--ask-for-approval", "never", "exec", "--cd", str(root), "--model", model,
                          "--sandbox", "read-only", "--skip-git-repo-check", "--json",
                          "--output-schema", str(BASE / "clarify.schema.json"), "--output-last-message", str(answer),
                          "--config", 'shell_environment_policy.set.PYTHONDONTWRITEBYTECODE="1"', "-"]
        if prelaunch:
            boundary.require(argv == prelaunch['argv'], 'Pending analysis invocation changed')
            prompt = _prelaunch_files(root, prelaunch, cleanup=True)
            journal = prelaunch
            timeout = min(timeout, journal['timeout'])
            # No process started, so shrink its recorded timeout to the remaining budget.
            # Remove only validated unused launch preparation before changing the timeout.
            for name in ('model.command.json', 'model.input.txt', 'model.stdout.log', 'model.stderr.log'):
                path = answer.parent / name
                if path.exists():
                    path.unlink()
            journal['timeout'] = timeout
        else:
            journal = {"version": 1, "project": str(Path(root).resolve()), "spec": spec,
                       "context": copy.deepcopy(analysis_context), "directory": relative_directory, "stage": "starting",
                       "before": before, "start_state": state, "start_files": _target_bytes(state),
                       "source_files": source_snapshot(root, relative_directory), "argv": argv, "timeout": timeout,
                       "prompt": prompt, "prompt_sha256": boundary.digest(prompt.encode("utf-8")), "evidence": {}}
        boundary.require(all(journal["source_files"].get(name, [None, None])[1] == checksum
                             for name, checksum in journal["start_state"]["input_hashes"].items()),
                         "Source input changed while preparing analysis")
        _save_analysis(root, journal)
        _publish_boundary(root, journal['before'], journal['start_state'], journal["start_files"])
        answer.parent.mkdir(parents=True, exist_ok=True)
        _atomic_runtime(answer.parent / "prompt.txt", prompt.encode("utf-8"))
        journal["stage"] = "running"
        _save_analysis(root, journal)
        def guard():
            problem_time.budget(budget_root, timeout, required=clock_required)
            boundary.require(source_snapshot(root, relative_directory) == journal["source_files"],
                             "Project source changed during read-only clarification")
            boundary.require(_boundary_bytes(root) == journal["start_files"], "Boundary changed during read-only clarification")
        def stop_analysis_process(process):
            stop_process(process)
            if process.poll() == 0 and answer.is_file():
                receipt = {"answer_sha256": boundary.digest(_analysis_path(root, relative_directory + "/answer.json").read_bytes()),
                           "invocation_sha256": boundary.json_digest({"argv": journal["argv"], "context": journal["context"],
                               "source_files": journal["source_files"], "start_files": journal["start_files"],
                               "prompt_sha256": journal["prompt_sha256"]})}
                data = _json_bytes(receipt)
                _atomic_runtime(answer.parent / "result-receipt.json", data)
                journal["result_receipt_sha256"] = boundary.digest(data)
                _save_analysis(root, journal)
        try:
            guard()
            code = lifecycle.execute(argv, root, answer.parent / "model", timeout, prompt, guard,
                deadline_guard=deadline_check,
                start_process=start_process, stop_process=stop_analysis_process, error_type=boundary.BoundaryError)
            boundary.require(code == 0, "Clarification CLI failed; see " + str(answer.parent))
        except BaseException as exc:
            outcome_path = answer.parent / "model.exit.json"
            outcome = boundary.decode(outcome_path.read_text(encoding="utf-8")) if outcome_path.is_file() else {}
            # A completed successful result remains recoverable even if the
            # controller is interrupted immediately after execute returns.
            if outcome.get("cleanup", {}).get("completed") is True and not (
                    outcome.get("termination_reason") == "completed" and outcome.get("exit_code") == 0):
                _fail_analysis(root, journal, exc)
            if isinstance(exc, boundary.BoundaryError) and str(exc).startswith("Timeout:"):
                raise boundary.BoundaryError("Clarification timed out; see " + str(answer.parent)) from exc
            raise
        try:
            _prepare_result(root, journal)
        except boundary.BoundaryError as exc:
            _fail_analysis(root, journal, exc)
            raise
        problem_time.budget(budget_root, timeout, required=clock_required)
        state = _finish_analysis(root, journal, deadline_check=deadline_check)
        problem_time.budget(budget_root, timeout, required=clock_required)
        show(state)
        return 0 if state["status"] == "ready" else 2


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(BASE.parent))
    parser.add_argument("--spec")
    parser.add_argument("--config")
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=int, default=600)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--answer", metavar="Q-ID")
    modes.add_argument("--status", action="store_true")
    modes.add_argument("--check-ready", action="store_true")
    parser.add_argument("--answer-file")
    args = parser.parse_args(arguments)
    try:
        boundary.require(sys.version_info[:2] >= (3, 9), "Clarify requires Python 3.9 or later")
        root = Path(args.root).resolve()
        config, config_path = configuration(root, args.config)
        if "project" in config:
            boundary.require(boundary.text(config["project"]), "Config project must be a path")
            project = config_path.parent / config["project"]
            boundary.require(project.is_dir(), "Config project does not exist")
            root = project.resolve()
        spec = selected_spec(root, args.spec, config)
        clock_required = problem_time.required(config)
        boundary.require(bool(args.answer) == bool(args.answer_file), "Use --answer Q-ID together with --answer-file UTF8_FILE")
        if args.answer:
            answer_file = Path(args.answer_file).expanduser()
            boundary.require(answer_file.is_file() and not answer_file.is_symlink()
                             and answer_file.absolute() == answer_file.resolve(), "Answer file must be an ordinary UTF-8 file")
            with state_lock(root):
                recover_answer(root, spec)
                clock = problem_time.status(root, required=clock_required)
                if not clock.get("expired", False):
                    recover_analysis(root, spec, deadline_check=lambda: problem_time.budget(root, args.timeout, required=clock_required))
                # Actual human answers remain durable even after the deadline.
                state = submit_answer(root, spec, args.answer, answer_file.read_text(encoding="utf-8-sig"))
            show(state)
            return 0
        if args.status or args.check_ready:
            clock = problem_time.status(root, required=clock_required)
            if args.status and clock.get("expired", False):
                saved = boundary.decode(boundary.project_path(root, boundary.STATE).read_text(encoding="utf-8-sig"))
                boundary.validate_state(root, saved, current=False, document=False)
                print("Problem deadline expired; displaying stored boundary JSON without recovery or ready approval.")
                show(saved)
                return 0
            with state_lock(root):
                recover_answer(root, spec)
                problem_time.budget(root, args.timeout, required=clock_required)
                recover_analysis(root, spec, deadline_check=lambda: problem_time.budget(root, args.timeout, required=clock_required))
                state = boundary.load_state(root)
            problem_time.budget(root, args.timeout, required=clock_required)
            if args.status:
                show(state)
            boundary.require_ready(root, spec)
            if args.check_ready:
                print("Boundary questions ready")
            return 0
        return analyze(root, spec, config, args.model or config.get("model") or config.get("builder_model"), args.timeout)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        print("Boundary clarification stopped: " + str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Boundary clarification interrupted; analysis is not ready", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
