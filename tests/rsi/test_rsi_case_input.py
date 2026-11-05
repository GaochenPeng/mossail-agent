from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from rsi.case_input import CASE_FILES, EVIDENCE_PATH
from rsi.experiment import Experiment
from rsi.workspace import write_json
from scripts.export_case import build_case
from scripts.review_case import reviewed_case


@pytest.fixture
def case_workflow(tmp_path):
    # 明确的测试替身：错误算术结果由独立要求判 fail，不伪造人工反馈或线上任务。
    tasks = [{'id': 'fixture-task', 'thread_id': 'fixture-thread', 'agent': 'assistant',
        'status': 'complete', 'error': None, 'question': '计算 6*7', 'snapshots': {
            'tool': {'type': 'tool_call', 'status': 'success', 'sender': 'assistant',
                'meta_data': {'name': 'calculate', 'output': '43'}},
            'answer': {'type': 'markdown', 'status': 'success', 'sender': 'assistant',
                'meta_data': {'content': '43'}}}}]
    criteria = {'reference': {'basis': '独立算术：6*7=42'}, 'steps': [
        {'expect': {'tool_names': ['calculate'], 'tool_number': '42', 'answer_patterns': ['42']}}]}
    case, suite = reviewed_case(build_case(tasks, 'synthetic'), tasks, criteria,
        kind='automated', rating='fail', reviewer='fixture-check', notes='结果不等于 42')
    case_directory = tmp_path / '.data/cases/fixture'
    documents = dict(zip(CASE_FILES, [case, tasks, criteria, suite]))
    for name, document in documents.items():
        write_json(case_directory / name, document)
    for name, content in [('apps/ok.py', 'value=1\n'), ('rsi/checks/check.py', 'pass\n'),
                          ('evals/cases.json', '{"cases": []}')]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    plan = {'objective': '修复测试替身中的错误结果',
        'planning': {'mode': 'local_manual', 'producer': 'test-fixture'}, 'allowed_files': ['apps/ok.py'],
        'change_scope': {'unit': 'file'}, 'target_check': 'rsi/checks/check.py',
        'target_expected_checks': ['result'], 'business_suite': 'evals/cases.json',
        'policy': {'require_existing_regressions': True, 'require_target_failure_on_baseline': True,
            'require_target_success_on_candidate': True, 'require_live_business_success': True,
            'merge': 'maintainer_only'}}
    write_json(tmp_path / 'plan.json', plan)
    return tmp_path, case_directory, documents


def prepare(workflow):
    project, case_directory, _ = workflow
    return Experiment.prepare(project, project / 'plan.json', case_directory=case_directory)


def test_case_is_frozen_without_replacing_the_business_regression_suite(case_workflow):
    project, case_directory, documents = case_workflow
    experiment = prepare(case_workflow)
    plan = json.loads((experiment.directory / 'plan.json').read_text())
    request = json.loads((experiment.directory / 'candidate-request.json').read_text())
    assert plan['source_case']['case_id'] == documents['case.json']['case_id']
    assert plan['source_case']['task_ids'] == ['fixture-task']
    assert plan['source_case']['review_kind'] == 'automated'
    assert plan['business_suite'] == 'evals/cases.json'
    assert request['generation']['automatic_generation'] is False
    assert request['planning']['producer'] == 'test-fixture'
    assert request['source_case'] == plan['source_case']
    assert request['verification_contract']['source_case_suite'] == 'frozen_evidence_only'
    assert json.loads((project / 'plan.json').read_text()).get('source_case') is None
    for name, document in documents.items():
        frozen = experiment.directory / 'controller' / EVIDENCE_PATH / name
        assert json.loads(frozen.read_text()) == document
    # 后续输入目录变化不影响已冻结实验；不能把活目录当作评审依据。
    write_json(case_directory / 'case.json', {})
    experiment.verify()


@pytest.mark.parametrize('damage', ['unreviewed', 'pass', 'raw', 'criteria', 'suite', 'active'])
def test_invalid_or_changed_case_is_rejected_before_experiment_creation(case_workflow, damage):
    project, directory, original = case_workflow
    documents = deepcopy(original)
    if damage in {'unreviewed', 'pass'}:
        documents['case.json']['review']['rating'] = damage
    elif damage == 'raw':
        documents['raw-tasks.json'][0]['question'] = '改过的输入'
    elif damage == 'criteria':
        documents['criteria.json']['reference']['basis'] = '换过的要求'
    elif damage == 'suite':
        documents['suite.json']['cases'][0]['steps'][0]['question'] = '换过的题目'
    else:
        documents['raw-tasks.json'][0]['status'] = 'running'
    for name, document in documents.items():
        write_json(directory / name, document)
    with pytest.raises(ValueError):
        prepare(case_workflow)
    assert not (project / '.data/rsi').exists()


@pytest.mark.parametrize('field', ['objective', 'mode', 'producer'])
def test_case_plan_cannot_hide_the_manual_planning_step(case_workflow, field):
    project, _, _ = case_workflow
    path = project / 'plan.json'
    plan = json.loads(path.read_text())
    if field == 'objective':
        plan['objective'] = ''
    else:
        plan['planning'].pop(field)
    write_json(path, plan)
    with pytest.raises(ValueError):
        prepare(case_workflow)
    assert not (project / '.data/rsi').exists()


@pytest.mark.parametrize('target', ['candidate-request.json', 'controller/' + EVIDENCE_PATH + '/criteria.json'])
def test_frozen_case_and_generation_input_cannot_change(case_workflow, target):
    experiment = prepare(case_workflow)
    write_json(experiment.directory / target, {'changed': True})
    with pytest.raises(ValueError, match='变化'):
        experiment.verify()


def test_bound_candidate_needs_source_identity_and_local_provenance(case_workflow, monkeypatch):
    project, _, documents = case_workflow
    experiment = prepare(case_workflow)
    proposal = {'rationale': '本地测试候选', 'source_case_id': 'another-case',
        'changes': [{'path': 'apps/ok.py', 'content': 'value=2\n'}],
        'provenance': {'producer': 'test-fixture', 'external_source_request': False}}
    path = project / 'proposal.json'
    write_json(path, proposal)
    monkeypatch.setattr('rsi.experiment.generate', lambda *args: pytest.fail('不得新增源码外发'))
    with pytest.raises(ValueError, match='source_case_id'):
        experiment.propose({}, path)
    assert not (experiment.directory / 'candidate').exists()
    proposal['source_case_id'] = documents['case.json']['case_id']
    proposal['provenance']['external_source_request'] = True
    write_json(path, proposal)
    with pytest.raises(ValueError, match='源码外发'):
        experiment.propose({}, path)
    proposal['provenance']['external_source_request'] = False
    write_json(path, proposal)
    experiment.propose({}, path)
    assert experiment.verify()['status'] == 'candidate_frozen'
    assert (project / 'apps/ok.py').read_text() == 'value=1\n'
    # 没有执行证据时只能形成证据不足建议；来源仍完整进入评审包。
    review = experiment.review()
    assert review['recommendation']['decision'] == 'inconclusive'
    assert review['source_case']['case_id'] == proposal['source_case_id']
    assert review['human_decision'] == 'pending'
    assert review['source_case_replay'] == 'not_automatic_use_target_check'
    assert proposal['source_case_id'] in (experiment.directory / 'review.md').read_text()
    # 测试维护者拒绝无执行证据的候选；不把测试记录冒充真实用户决定。
    decided = experiment.record_decision('reject', 'fixture-maintainer', '缺少执行证据，不采用')
    assert decided['human_decision']['decision'] == 'reject'
    assert decided['source_case'] == review['source_case']
    assert (project / 'apps/ok.py').read_text() == 'value=1\n'


def test_cli_case_input_requires_explicit_plan_before_loading_configuration(tmp_path):
    root = Path(__file__).resolve().parents[2]
    output = tmp_path / 'must-not-be-created'
    result = subprocess.run([sys.executable, str(root / 'scripts/rsi.py'), 'prepare',
        '--case', str(tmp_path / 'missing-case'), '--output', str(output)],
        cwd=root, capture_output=True, text=True)
    assert result.returncode == 2 and '显式 --plan' in result.stderr
    assert not output.exists()
