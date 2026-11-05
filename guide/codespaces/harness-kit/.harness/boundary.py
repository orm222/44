"""Project boundary questions and actual human answers; Python 3.9 stdlib only."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import uuid

STATE = ".harness/boundary.json"
ANSWERS = "BOUNDARY_ANSWERS.md"
TEMPLATE = "BOUNDARY_QUESTION_TEMPLATE.md"
GENERATED = {STATE, ANSWERS, ".harness/requirements.json", ".harness/functional-spec.json",
             "CONFIRMED_REQUIREMENTS.md", "FUNCTIONAL_SPEC.md"}
MAX_QUESTIONS = 8
STATUSES = {"needs_analysis", "needs_answers", "analyzing", "failed", "ready"}
_DATA_CHECKS = None


class BoundaryError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise BoundaryError(message)


def pairs(items):
    result = {}
    for key, value in items:
        require(key not in result, "Duplicate boundary JSON key: " + key)
        result[key] = value
    return result


def decode(text):
    def invalid(value):
        raise BoundaryError("Invalid boundary JSON constant: " + value)
    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, TypeError) as exc:
        raise BoundaryError("Invalid boundary JSON: " + str(exc)) from exc


def text(value):
    return isinstance(value, str) and bool(value.strip()) and "\0" not in value


def project_path(root, name, exists=True, runtime=False):
    root = Path(root).resolve()
    require(text(name), "Boundary path must be a nonempty project-relative path")
    relative = Path(name)
    require(not relative.anchor and not PureWindowsPath(name).anchor and ".." not in relative.parts and "\\" not in name,
            "Boundary paths must be portable project-relative paths: " + name)
    require(relative.parts and relative.parts[0] not in (".git", ".venv", "venv", "work"),
            "Boundary inputs cannot use Git/environment/runtime directories: " + name)
    require(not set(relative.parts) & {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"},
            "Boundary paths cannot use excluded environment/cache directories: " + name)
    require(runtime or relative.parts[:2] != (".harness", "work"),
            "Boundary inputs cannot use runtime directories: " + name)
    path = root / relative
    require(path.absolute() == path.resolve() and path.resolve().is_relative_to(root),
            "Boundary path is outside or redirected: " + name)
    for parent in (path, *path.parents):
        if parent == root:
            break
        require(not parent.is_symlink(), "Boundary symlink is not allowed: " + name)
    if exists:
        require(path.is_file() and stat.S_ISREG(path.stat().st_mode), "Missing boundary file: " + name)
    elif path.exists():
        require(path.is_file(), "Boundary destination must be a file: " + name)
    return path


def digest(value):
    return hashlib.sha256(value).hexdigest()


def json_digest(value):
    return digest(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def project_configuration(root):
    config_path = project_path(root, "config.json", exists=False)
    if not config_path.exists():
        return {}
    config = decode(config_path.read_text(encoding="utf-8-sig"))
    require(isinstance(config, dict), "Project config must be an object")
    return config


def interview_reference(root, spec):
    """The project config, not a model response, decides whether an interview is mandatory."""
    config = project_configuration(root)
    if "interview" not in config:
        return None
    name = config["interview"]
    require(text(name), "config.interview must name a project-relative interview file")
    path = project_path(root, name)
    name = path.relative_to(Path(root).resolve()).as_posix()
    selected = project_path(root, spec).relative_to(Path(root).resolve()).as_posix()
    require(name not in GENERATED | {TEMPLATE, selected, "config.json"},
            "Interview must be separate from the problem, generated documents and metadata")
    source = project_path(root, spec).read_text(encoding="utf-8-sig")
    require(re.search(r"(?<![\w./\\-])(?:\./)?" + re.escape(name) + r"(?![\w./\\-])", source) is not None,
            "Problem document must explicitly reference the interview file: " + name)
    require(text(path.read_text(encoding="utf-8-sig")), "Interview file is empty")
    return name


def _data_module(root):
    config = project_configuration(root)
    if "data_files" not in config and "data_baseline" not in config:
        return None
    global _DATA_CHECKS
    if _DATA_CHECKS is None:
        loader = importlib.util.spec_from_file_location("boundary_data_checks", Path(__file__).with_name("data_checks.py"))
        module = importlib.util.module_from_spec(loader)
        loader.loader.exec_module(module)
        _DATA_CHECKS = module
    return _DATA_CHECKS


def required_references(root, spec):
    interview = interview_reference(root, spec)
    references = [interview] if interview is not None else []
    module = _data_module(root)
    if module is not None:
        for name in module.configured_files(root):
            name = project_path(root, name).relative_to(Path(root).resolve()).as_posix()
            require(name not in GENERATED | {TEMPLATE, spec, "config.json"},
                    "Data input must be separate from problem documents and metadata")
            if name not in references:
                references.append(name)
    return references


def data_context(root, input_hashes):
    """Keep numeric observations bound to the same bytes as the model's source inputs."""
    module = _data_module(root)
    if module is None:
        return None
    names = module.configured_files(root)
    summary = module.inspect(root)
    if not names:
        require(summary is None, "Unexpected data summary without configured inputs")
        return None
    require(isinstance(summary, dict) and isinstance(summary.get("input_hashes"), dict),
            "Data inspection did not return input hashes")
    require(all(name in input_hashes for name in names)
            and summary["input_hashes"] == {name: input_hashes[name] for name in names},
            "Data changed while preparing the model context")
    return summary


def input_hashes(root, spec, references):
    return {name: digest(project_path(root, name).read_bytes())
            for name in sorted(set([spec, TEMPLATE] + references))}


def actual_answers(state):
    return {item["question_id"]: item["text"] for item in state["answers"]}


def _render_answers(state, korean):
    title, spec, notice, question_label, decides, answer_label, pending, empty = (
        ("# 확인 질문과 실제 답변", "문제 설명: ", "사용자가 명시적으로 제출한 실제 답변만 아래에 기록합니다.",
         "질문: ", "결정할 사항: ", "실제 답변 ", "실제 답변: 대기 중", "아직 확인 질문이 생성되지 않았습니다.")
        if korean else
        ("# Boundary questions and actual answers", "Specification: ",
         "Only answers explicitly submitted by the user appear below.",
         "Question: ", "Determines: ", "User answer ", "User answer: pending", "No boundary questions have been generated."))
    lines = [title, "", spec + state["spec"], "", notice, ""]
    for question in state["questions"]:
        lines += ["## " + question["id"] + " | " + question["template"], "",
                  question_label + question["question"], "", decides + question["decides"], ""]
        submitted = [item for item in state["answers"] if item["question_id"] == question["id"]]
        if submitted:
            for revision, answer in enumerate(submitted, 1):
                lines += [answer_label + str(revision) + ":", "", answer["text"], ""]
        else:
            lines += [pending, ""]
    if not state["questions"]:
        lines += [empty, ""]
    return "\n".join(lines)


def render_answers(state):
    # Existing records and recovery journals retain their exact, verified bytes.
    legacy = _render_answers(state, False)
    if state.get("answers_document_sha256") == digest(legacy.encode("utf-8")):
        return legacy
    return _render_answers(state, True)


def validate_report(root, report, state):
    require(isinstance(report, dict) and set(report) == {"version", "questions", "unresolved", "references"},
            "Question analysis allows only version/questions/unresolved/references; never model answers")
    require(type(report["version"]) is int and report["version"] == 1, "Question analysis version must be 1")
    questions, unresolved, references = (report[key] for key in ("questions", "unresolved", "references"))
    require(isinstance(questions, list) and len(questions) <= MAX_QUESTIONS, "Ask at most eight boundary questions at once")
    require(isinstance(unresolved, list) and all(text(item) for item in unresolved), "Invalid unresolved boundary list")
    require(bool(questions) == bool(unresolved), "Unresolved boundaries require one to eight actionable questions")
    require(isinstance(references, list) and all(text(item) for item in references)
            and len(set(references)) == len(references), "Invalid referenced document list")
    require(not set(references) & GENERATED, "Generated documents/state cannot be source references")
    required = required_references(root, state["spec"])
    require(set(required) <= set(references),
            "Analysis must include every configured interview/data input in references: " + ", ".join(required))
    sources = "\n".join(project_path(root, name).read_text(encoding="utf-8-sig")
                        for name in set([state["spec"]] + references))
    prior_answers = actual_answers(state)
    for question in questions:
        require(isinstance(question, dict) and set(question) == {"template", "question", "decides", "follow_up_to"},
                "Each question needs template/question/decides/follow_up_to only")
        require(isinstance(question["template"], str) and re.fullmatch(r"T(?:0[1-9]|1[0-5])", question["template"]),
                "Question template must be T01 through T15")
        require(text(question["question"]) and text(question["decides"]), "Question and determines text cannot be empty")
        prior = question["follow_up_to"]
        require(prior is None or (isinstance(prior, str) and prior in prior_answers and question["template"] == "T12"),
                "A follow-up must use T12 and refer to an actual user answer")
        for value in (question["question"], question["decides"]):
            require(not re.search(r"(?:^|\n)\s*(?:근거|출처(?:\s*위치)?|source\s*location)\s*[:：]", value, re.I)
                    and not re.search(r"(?:페이지|\bpage)\s*\d+|\d+\s*(?:페이지|쪽)\b", value, re.I),
                    "Questions must not append source-location labels or page references")
            for token in re.findall(r"\{[\w가-힣]+\}", value):
                require(token in sources, "Unsubstituted question placeholder: " + token)
    return report


def create_state(root, spec):
    spec = project_path(root, spec).relative_to(Path(root).resolve()).as_posix()
    require(spec not in GENERATED | {TEMPLATE}, "The problem must be separate from generated documents and metadata")
    references = required_references(root, spec)
    return {"version": 1, "spec": spec, "references": references, "input_hashes": input_hashes(root, spec, references),
            "status": "needs_analysis", "questions": [], "answers": [], "analysis": None,
            "error": None, "answers_document_sha256": ""}


def validate_state(root, state, current=True, document=True):
    keys = {"version", "spec", "references", "input_hashes", "status", "questions", "answers",
            "analysis", "error", "answers_document_sha256"}
    require(isinstance(state, dict) and set(state) == keys and type(state["version"]) is int and state["version"] == 1,
            "Invalid boundary state structure; run clarify")
    require(isinstance(state["status"], str) and state["status"] in STATUSES
            and (state["error"] is None or text(state["error"])), "Invalid boundary status")
    project_path(root, state["spec"], exists=current)
    require(state["spec"] not in GENERATED | {TEMPLATE}, "Generated documents and metadata are not problem inputs")
    require(isinstance(state["references"], list) and all(text(name) for name in state["references"])
            and len(set(state["references"])) == len(state["references"]), "Invalid boundary references")
    require(not set(state["references"]) & GENERATED, "Generated documents/state cannot be source references")
    if current:
        required = required_references(root, state["spec"])
        require(set(required) <= set(state["references"]),
                "Boundary state is missing a configured interview/data input; reanalyze the problem")
    expected = set([state["spec"], TEMPLATE] + state["references"])
    require(isinstance(state["input_hashes"], dict) and set(state["input_hashes"]) == expected,
            "Boundary input bindings are incomplete")
    for name, value in state["input_hashes"].items():
        project_path(root, name, exists=current)
        require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value), "Invalid boundary input hash")
    require(isinstance(state["questions"], list) and isinstance(state["answers"], list), "Invalid boundary history")
    ids = set()
    for number, item in enumerate(state["questions"], 1):
        require(isinstance(item, dict) and set(item) == {"id", "template", "question", "decides", "follow_up_to"},
                "Invalid saved boundary question")
        require(item["id"] == "Q-{:03d}".format(number) and text(item["question"]) and text(item["decides"])
                and isinstance(item["template"], str) and re.fullmatch(r"T(?:0[1-9]|1[0-5])", item["template"]),
                "Invalid saved question ID/template/text")
        require(item["follow_up_to"] is None or (isinstance(item["follow_up_to"], str)
                and item["follow_up_to"] in ids and item["template"] == "T12"),
                "Invalid saved follow-up")
        ids.add(item["id"])
    for item in state["answers"]:
        require(isinstance(item, dict) and set(item) == {"question_id", "text"}
                and isinstance(item["question_id"], str) and item["question_id"] in ids
                and text(item["text"]), "Invalid actual answer history")
    analysis = state["analysis"]
    if analysis is not None:
        require(isinstance(analysis, dict) and set(analysis) == {"report", "answers_sha256"}, "Invalid boundary analysis")
        require(analysis["answers_sha256"] == json_digest(state["answers"]), "Answers changed after boundary analysis")
        if current:
            validate_report(root, analysis["report"], state)
        else:
            report = analysis["report"]
            require(isinstance(report, dict) and set(report) == {"version", "questions", "unresolved", "references"},
                    "Invalid saved boundary analysis")
        require(analysis["report"]["references"] == state["references"], "Boundary reference bindings differ")
    if current:
        require(state["input_hashes"] == input_hashes(root, state["spec"], state["references"]),
                "Boundary inputs changed; run clarify again")
    if document:
        rendered = render_answers(state).encode("utf-8")
        require(state["answers_document_sha256"] == digest(rendered)
                and project_path(root, ANSWERS).read_bytes() == rendered,
                "BOUNDARY_ANSWERS.md differs from actual submitted answers; use clarify --answer")
    return state


def load_state(root, current=True):
    return validate_state(root, decode(project_path(root, STATE).read_text(encoding="utf-8-sig")), current=current)


def atomic_write(root, name, content):
    path = project_path(root, name, exists=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
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


def save_state(root, state):
    rendered = render_answers(state).encode("utf-8")
    state["answers_document_sha256"] = digest(rendered)
    validate_state(root, state, current=False, document=False)
    # Publish the state last: interruption cannot leave an apparently ready new answer.
    atomic_write(root, ANSWERS, rendered)
    atomic_write(root, STATE, (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return state


def analysis_state(root, state, report):
    """Validate and prepare analysis without publishing either boundary file."""
    state = copy.deepcopy(state)
    require(not (set(q["id"] for q in state["questions"]) - set(actual_answers(state))),
            "Existing boundary questions still need actual user answers")
    validate_report(root, report, state)
    for question in report["questions"]:
        state["questions"].append(dict(question, id="Q-{:03d}".format(len(state["questions"]) + 1)))
    state["references"] = report["references"][:]
    state["input_hashes"] = input_hashes(root, state["spec"], state["references"])
    state["analysis"] = {"report": report, "answers_sha256": json_digest(state["answers"])}
    state["status"] = "needs_answers" if report["questions"] else "ready"
    state["error"] = None
    state["answers_document_sha256"] = digest(render_answers(state).encode("utf-8"))
    return validate_state(root, state, current=True, document=False)


def complete_analysis(root, state, report):
    target = analysis_state(root, state, report)
    state.clear()
    state.update(target)
    return save_state(root, state)


def submit_answer(root, question_id, answer):
    state = load_state(root, current=False)
    require(question_id in {q["id"] for q in state["questions"]}, "Unknown boundary question: " + question_id)
    require(text(answer), "Actual answer cannot be blank")
    state["answers"].append({"question_id": question_id, "text": answer})
    state["status"], state["analysis"], state["error"] = "needs_analysis", None, None
    return save_state(root, state)


def require_ready(root, spec=None):
    root = Path(root).resolve()
    state = load_state(root)
    if spec is not None:
        selected = Path(spec)
        if selected.is_absolute():
            require(selected.resolve().is_relative_to(root), "Boundary specification is outside the project")
            selected = selected.resolve().relative_to(root)
        require(selected.as_posix() == state["spec"], "Boundary answers belong to a different specification")
    require(state["status"] == "ready" and state["error"] is None and state["analysis"] is not None,
            "Boundary questions are not ready; use clarify --status and submit actual answers")
    report = state["analysis"]["report"]
    require(not report["questions"] and not report["unresolved"]
            and {q["id"] for q in state["questions"]} <= set(actual_answers(state)),
            "Unresolved boundary questions must not be treated as ready")
    paths = sorted(set([STATE, ANSWERS] + list(state["input_hashes"])))
    return {"context": render_answers(state), "protected_files": [project_path(root, name) for name in paths],
            "answered_question_ids": sorted(actual_answers(state)), "spec": state["spec"],
            "state_path": project_path(root, STATE)}
