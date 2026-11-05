import json
from pathlib import Path

import pytest

from rsi.workspace import create_candidate
from rsi.experiment import Experiment

SOURCE = '''import math

def target(value: str) -> bool:
    return bool(value)

def other():
    return 1
'''
SCOPE = {'unit': 'python_symbol', 'symbols': {'apps/example.py': ['target']},
         'preserve_signatures': True}


def candidate(tmp_path: Path, source: str, after: str, scope=SCOPE):
    baseline = tmp_path / 'baseline'
    (baseline / 'apps').mkdir(parents=True)
    (baseline / 'apps/example.py').write_text(source)
    target = tmp_path / 'candidate'
    return target, lambda: create_candidate(baseline, target,
        {'changes': [{'path': 'apps/example.py', 'content': after}]}, ['apps/example.py'], scope)


def test_only_declared_function_body_can_change(tmp_path):
    target, run = candidate(tmp_path, SOURCE, SOURCE.replace('bool(value)', 'bool(value.strip())'))
    assert 'value.strip()' in run()
    assert 'def other()' in (target / 'apps/example.py').read_text()


@pytest.mark.parametrize('after', [
    SOURCE.replace('return 1', 'return 2'),
    SOURCE.replace('import math', 'import os'),
    SOURCE + '\nSHARED_STATE = {}\n',
    SOURCE.replace('target(value: str)', 'renamed(value: str)'),
    SOURCE.replace('value: str', 'value: int'),
    SOURCE.replace('-> bool', '-> str'),
    SOURCE.replace('def target', '@staticmethod\ndef target'),
    SOURCE.replace('def target', 'async def target'),
    'raise RuntimeError("replace everything")\n',
    'invalid python !!!',
])
def test_scope_violation_is_rejected_before_candidate_directory_exists(tmp_path, after):
    target, run = candidate(tmp_path, SOURCE, after)
    with pytest.raises(ValueError):
        run()
    assert not target.exists()


def test_method_scope_and_signature_defaults_are_preserved(tmp_path):
    source = 'class Tool:\n    def read(self, value=1):\n        return value\n'
    scope = {'unit': 'python_symbol', 'symbols': {'apps/example.py': ['Tool.read']},
             'preserve_signatures': True}
    target, run = candidate(tmp_path, source, source.replace('return value', 'return value + 1'), scope)
    assert '+ 1' in run()


def test_duplicate_function_definition_cannot_hide_a_replacement(tmp_path):
    target, run = candidate(tmp_path, SOURCE, SOURCE + '\ndef target(value: str) -> bool:\n    return True\n')
    with pytest.raises(ValueError, match='重复定义'):
        run()
    assert not target.exists()


def test_unchanged_decorator_is_not_mistaken_for_signature_change(tmp_path):
    source = 'class Tool:\n    @staticmethod\n    def read(value=1):\n        return value\n'
    scope = {'unit': 'python_symbol', 'symbols': {'apps/example.py': ['Tool.read']},
             'preserve_signatures': True}
    target, run = candidate(tmp_path, source, source.replace('return value', 'return value + 1'), scope)
    assert '+ 1' in run()


def test_changed_default_argument_is_a_signature_change(tmp_path):
    source = 'def target(value=1):\n    return value\n'
    target, run = candidate(tmp_path, source, source.replace('value=1', 'value=2'))
    with pytest.raises(ValueError, match='函数签名'):
        run()
    assert not target.exists()


def test_unknown_plan_scope_field_cannot_be_silently_ignored(tmp_path):
    scope = {**SCOPE, 'maximum_lines': 1}
    target, run = candidate(tmp_path, SOURCE, SOURCE.replace('bool(value)', 'True'), scope)
    with pytest.raises(ValueError, match='未支持字段'):
        run()
    assert not target.exists()


def test_new_experiment_requires_explicit_granularity_before_snapshot(tmp_path):
    (tmp_path/'apps').mkdir()
    (tmp_path/'apps/example.py').write_text(SOURCE)
    plan = tmp_path/'plan.json'
    plan.write_text(json.dumps({'allowed_files':['apps/example.py']}))
    with pytest.raises(ValueError, match='必须明确 change_scope'):
        Experiment.prepare(tmp_path, plan)
    assert not (tmp_path/'.data').exists()
