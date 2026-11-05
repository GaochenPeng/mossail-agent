from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from rsi.experiment import Experiment, assess
from rsi.workspace import create_candidate, manifest, snapshot, validate_changes
from rsi.review import git_review
from rsi.model_gateway import evaluation_model

POLICY = {'require_existing_regressions':True,'require_target_failure_on_baseline':True,
    'require_target_success_on_candidate':True,'require_live_business_success':True,'merge':'maintainer_only'}


def test_subscription_rsi_cannot_silently_fall_back_to_api_key(tmp_path):
    with pytest.raises(ValueError, match='不会回退'):
        with evaluation_model(tmp_path,
                {'AGENT_AUTH_MODE': 'chatgpt', 'OPENAI_API_KEY': 'unused-test-key'}, max_requests=1):
            pytest.fail('缺少订阅凭证时不应建立模型访问')


def test_candidate_cannot_modify_tests_or_escape_the_allowlist():
    for name in ['../outside.py','tests/test.py','/tmp/escape.py','apps/../apps/ok.py']:
        with pytest.raises(ValueError):
            validate_changes({'changes':[{'path':name,'content':'x'}]},['apps/ok.py'])


def test_snapshot_omits_credentials_and_keeps_candidate_separate(tmp_path):
    source = tmp_path/'source';(source/'apps').mkdir(parents=True)
    (source/'apps/ok.py').write_text('original\n');(source/'.env.local').write_text('synthetic credential')
    (source/'apps/.venv').mkdir();(source/'apps/.venv/secret.py').write_text('must not copy')
    baseline = tmp_path/'baseline';snapshot(source,baseline)
    candidate = tmp_path/'candidate'
    diff = create_candidate(baseline,candidate,{'changes':[{'path':'apps/ok.py','content':'candidate\n'}]},['apps/ok.py'])
    assert '+candidate' in diff
    assert (source/'apps/ok.py').read_text()=='original\n'
    assert (baseline/'apps/ok.py').read_text()=='original\n'
    assert not (candidate/'.env.local').exists() and not (candidate/'apps/.venv').exists()


def test_sealed_controller_or_plan_changes_are_detected(tmp_path):
    for name,content in [('apps/ok.py','x=1'),('rsi/checks/check.py','pass'),('evals/cases.json','{}')]:
        p=tmp_path/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(content)
    plan={'allowed_files':['apps/ok.py'],'change_scope':{'unit':'file'},'policy':POLICY,'target_check':'rsi/checks/check.py',
          'business_suite':'evals/cases.json','target_expected_checks':['valid']}
    path=tmp_path/'plan.json';path.write_text(json.dumps(plan))
    experiment=Experiment.prepare(tmp_path,path)
    experiment.verify()
    (experiment.directory/'controller/rsi/checks/check.py').write_text('changed')
    with pytest.raises(ValueError,match='验收器'):
        experiment.verify()


def runs():
    def run(passed):
        return {'engineering':{'backend':{'passed':True},'agent':{'passed':True},
            'target':{'returncode':0 if passed else 1,'observation':{'checks':{'target':passed},'passed':passed}}},
            'business':{'passed':True,'results':[{'case_id':'one','step':1}]}}
    return {'baseline':run(False),'candidate':run(True)}


def test_recommendation_requires_observed_improvement_and_equal_tasks():
    assert assess(runs(),POLICY,['target'])['decision']=='recommend_accept'
    examples=[]
    missing=runs();missing['baseline']['engineering']['target']['observation']=None;examples.append(missing)
    mismatch=runs();mismatch['candidate']['business']['results']=[];examples.append(mismatch)
    no_gain=runs();no_gain['baseline']=deepcopy(no_gain['candidate']);examples.append(no_gain)
    for evidence in examples:
        assert assess(evidence,POLICY,['target'])['decision']=='inconclusive'


def test_candidate_regression_or_unfixed_target_is_rejected():
    regressed=runs();regressed['candidate']['engineering']['backend']['passed']=False
    assert assess(regressed,POLICY,['target'])['decision']=='reject'
    unfixed=runs();unfixed['candidate']['engineering']['target']=deepcopy(unfixed['baseline']['engineering']['target'])
    assert assess(unfixed,POLICY,['target'])['decision']=='reject'


def test_review_commits_are_separate_and_tampering_is_detected(tmp_path):
    baseline=tmp_path/'baseline';(baseline/'apps').mkdir(parents=True)
    (baseline/'apps/change.py').write_text('before\n')
    create_candidate(baseline,tmp_path/'candidate',{'changes':[{'path':'apps/change.py','content':'after\n'}]},['apps/change.py'])
    proof=git_review(tmp_path,['apps/change.py'])
    assert proof['baseline_commit']!=proof['candidate_commit']
    assert (tmp_path/'candidate.patch').exists() and (tmp_path/'candidate.bundle').exists()
    assert (baseline/'apps/change.py').read_text()=='before\n'
    (tmp_path/'git-review/apps/change.py').write_text('unexpected edit')
    with pytest.raises(ValueError,match='已变化'):
        git_review(tmp_path,['apps/change.py'])


def test_local_proposal_never_calls_external_generation(tmp_path,monkeypatch):
    for name,content in [('apps/ok.py','x=1'),('rsi/checks/check.py','pass'),('evals/cases.json','{}')]:
        p=tmp_path/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(content)
    plan={'allowed_files':['apps/ok.py'],'change_scope':{'unit':'file'},'policy':POLICY,'target_check':'rsi/checks/check.py',
          'business_suite':'evals/cases.json','target_expected_checks':['valid']}
    path=tmp_path/'plan.json';path.write_text(json.dumps(plan))
    experiment=Experiment.prepare(tmp_path,path)
    proposal=tmp_path/'proposal.json';proposal.write_text(json.dumps({'changes':[{'path':'apps/ok.py','content':'x=2'}]}))
    def forbidden(*args): raise AssertionError('不得调用外部模型')
    monkeypatch.setattr('rsi.experiment.generate',forbidden)
    experiment.propose({},proposal)
    assert experiment.verify()['status']=='candidate_frozen'
    assert (tmp_path/'apps/ok.py').read_text()=='x=1'
    assert json.loads((experiment.directory/'generation.json').read_text())['external_request_made'] is False


def test_cli_requires_local_proposal_before_preparing_or_loading_model_configuration(tmp_path):
    root=Path(__file__).resolve().parents[2]
    output=tmp_path/'must-not-be-created'
    result=subprocess.run([sys.executable,str(root/'scripts/rsi.py'),'run','--output',str(output)],
        cwd=root,capture_output=True,text=True)
    assert result.returncode==2
    assert '--proposal' in result.stderr and '不执行源码外发' in result.stderr
    assert not output.exists()
