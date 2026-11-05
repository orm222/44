"""Small, strict model contracts; semantic checks remain in the controller."""
import re
import math
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from pathlib import PurePosixPath


def obj(**properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


def array(items):
    return {'type': 'array', 'items': items}


STRING = {'type': 'string'}
INTEGER = {'type': 'integer'}
BOOLEAN = {'type': 'boolean'}
STRINGS = array(STRING)


def nullable(schema):
    return {'anyOf': [schema, {'type': 'null'}]}


CHECK = obj(id=STRING, argv=STRINGS, cwd=STRING, timeout_seconds=INTEGER,
            test_framework={'type': 'string', 'enum': ['unittest', 'pytest', 'node', 'command']},
            min_tests=INTEGER, requirement=STRING, oracle=STRING)
DECISION = obj(before=STRING, after=STRING, reason=STRING, discarded=STRINGS, expected_minutes=INTEGER)
FLOW = obj(id=STRING, feature=STRING, entry=STRING, chain=array(obj(file=STRING, function=STRING)),
           reads=STRINGS, writes=STRINGS)
DOCKER = obj(dockerfile=STRING, context=STRING, command=STRINGS, checks=array(CHECK),
             container_port=INTEGER, host_port=INTEGER, compose_file=nullable(STRING), service=nullable(STRING))
TASK = obj(id=STRING, title=STRING, requirements=STRINGS, depends_on=STRINGS, files=STRINGS,
           test_files=STRINGS, checks=array(CHECK), expected_minutes=INTEGER)
PLAN = obj(summary=STRING, architecture=obj(framework=STRING, ports=array(INTEGER), storage=STRING, algorithm=STRING),
           decisions=array(obj(decision=STRING, reason=STRING, discarded=STRINGS)), tasks=array(TASK),
           docker=DOCKER, docker_files=STRINGS, run_commands=array(STRINGS), limitations=STRINGS,
           stage_minutes=obj(plan=INTEGER, implementation=INTEGER, docker=INTEGER))
LEGACY_PLAN = PLAN
_spec = importlib.util.spec_from_file_location('contracts_assurance', Path(__file__).with_name('assurance.py'))
assurance = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(assurance)
PLAN = obj(**dict(LEGACY_PLAN['properties'], assurance=assurance.schema(SimpleNamespace(
    obj=obj, array=array, STRING=STRING, STRINGS=STRINGS, INTEGER=INTEGER, nullable=nullable))))
PATCH = obj(summary=STRING, files=array(obj(path=STRING, content=nullable(STRING))),
            flows=array(FLOW), decision=nullable(DECISION))
REVIEW = obj(approved=BOOLEAN, findings=STRINGS, decision=nullable(DECISION),
             verified_claims=array(obj(claim=STRING, method=STRING, result=STRING)), limitations=STRINGS)


def validate(value, schema, at='result'):
    if 'anyOf' in schema:
        for choice in schema['anyOf']:
            try:
                validate(value, choice, at)
                return value
            except ValueError:
                pass
        raise ValueError(at + ': invalid nullable value')
    kind = schema['type']
    valid = {'object': isinstance(value, dict), 'array': isinstance(value, list),
             'string': isinstance(value, str), 'integer': type(value) is int,
             'number': type(value) is int or (type(value) is float and math.isfinite(value)),
             'boolean': type(value) is bool, 'null': value is None}[kind]
    if not valid or ('enum' in schema and value not in schema['enum']):
        raise ValueError(at + ': invalid ' + kind)
    if kind == 'object':
        if set(value) != set(schema['properties']):
            raise ValueError(at + ': missing or unexpected fields')
        for key, child in schema['properties'].items():
            validate(value[key], child, at + '.' + key)
    elif kind == 'array':
        for i, child in enumerate(value):
            validate(child, schema['items'], at + '[' + str(i) + ']')
    return value


def relative_path(value):
    if (not isinstance(value, str) or not value.strip() or '\\' in value or ':' in value
            or '\x00' in value or value != value.strip()):
        raise ValueError('Invalid relative file path: ' + str(value))
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ('', '.', '..') for part in value.split('/')):
        raise ValueError('Invalid relative file path: ' + value)
    if any(part.endswith((' ', '.')) for part in path.parts):
        raise ValueError('Ambiguous file path: ' + value)
    if path.parts[0].lower() in ('.harness', '.git', 'doc', '.agents', '.codex') or value.lower() == 'readme.md':
        raise ValueError('Controller-owned path: ' + value)
    return value


def validate_plan(plan, checks_module, enforce_new_policy=False):
    validate(plan, PLAN if 'assurance' in plan else LEGACY_PLAN)
    if not plan['summary'].strip() or not plan['tasks'] or not plan['decisions']:
        raise ValueError('Plan needs tasks, summary, and reasoned decisions')
    if enforce_new_policy:
        for index, decision in enumerate(plan['decisions']):
            for field in ('decision', 'reason'):
                if not decision[field].strip():
                    raise ValueError('decisions[{}].{} must explain the actual choice'.format(index, field))
    ids, owners, test_owners = set(), {}, {}
    executions = {}
    for task in plan['tasks']:
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', task['id']) or task['id'] in ids:
            raise ValueError('Invalid or duplicate task id')
        if not set(task['depends_on']) <= ids:
            raise ValueError('Task dependencies must precede the task')
        if not task['requirements'] or not task['title'].strip() or task['expected_minutes'] <= 0:
            raise ValueError('Task must trace requirements and estimate time')
        if not task['files'] or not task['test_files'] or set(task['files']) & set(task['test_files']):
            raise ValueError('Separate production files and new test files are required')
        for name in task['files'] + task['test_files']:
            relative_path(name)
            folded = name.casefold()
            if folded in owners and owners[folded] != name:
                raise ValueError('Case-colliding file names')
            owners[folded] = name
        for name in task['test_files']:
            if name in test_owners:
                raise ValueError('Each task owns distinct new test files')
            test_owners[name] = task['id']
        normalized = checks_module.validate_checks(task['checks'])
        if enforce_new_policy:
            for check in normalized:
                signature = (tuple(check['argv']), check['cwd'], check['test_framework'])
                label = task['id'] + '/' + check['id']
                if signature in executions:
                    raise ValueError('Duplicate task check execution: {} and {}. '
                        'Use task-owned test scopes; if both contracts intentionally share one execution, '
                        'combine them during plan review while preserving every requirement, oracle and '
                        'min_tests obligation. Do not delete a required contract or reduce test coverage.'.format(
                            executions[signature], label))
                executions[signature] = label
        ids.add(task['id'])
    checks_module.validate_checks([check for task in plan['tasks'] for check in task['checks']])
    if any(name in test_owners for task in plan['tasks'] for name in task['files']):
        raise ValueError('Production patch cannot alter a test contract')
    if not plan['docker_files']:
        raise ValueError('Docker configuration files are required')
    for name in plan['docker_files']:
        relative_path(name)
        if name in test_owners:
            raise ValueError('Docker patch cannot alter task tests')
    checks_module.validate_docker(plan['docker'])
    if any(value <= 0 for value in plan['stage_minutes'].values()):
        raise ValueError('Stage estimates must be positive minutes')
    if 'assurance' in plan:
        assurance.validate(plan['assurance'], plan)
    return plan
