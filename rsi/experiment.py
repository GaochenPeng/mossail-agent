"""实验用例：固定输入 → 生成候选 → 执行 → 按预设规则形成评审材料。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

from .execution import require_isolated_execution
from .proposal import generate
from .review import git_review
from .change_scope import validate_candidate, validate_plan
from .case_input import CaseEvidence, validate_planning, validate_proposal_source
from .workspace import create_candidate, digest, manifest, snapshot, write_json


def read(path):
    return json.loads(Path(path).read_text())


def kernel_manifest():
    root = Path(__file__).resolve().parents[1]
    files = sorted((root/'rsi').glob('*.py'))+[root/'scripts/rsi.py']
    return {str(p.relative_to(root)):digest(p.read_bytes()) for p in files}


@dataclass
class Experiment:
    directory: Path

    @classmethod
    def prepare(cls, project: Path, plan_path: Path, output: Path | None = None,
                *, case_directory: Path | None = None):
        plan = read(plan_path)
        if 'source_case' in plan:
            raise ValueError('source_case 由案例材料自动绑定，不能只手填身份；请使用 --case')
        evidence = CaseEvidence.load(case_directory) if case_directory else None
        if evidence:
            validate_planning(plan)
        allowed = plan['allowed_files']
        files = manifest(project)
        if not allowed or any(not name.startswith(('apps/','packages/')) or name not in files for name in allowed):
            raise ValueError('每轮必须显式指定现有 apps/packages 文件，不能修改本轮控制器、测试和接受规则')
        if plan.get('change_scope') is None:
            raise ValueError('新实验必须明确 change_scope：文件级 file 或 Python 符号级 python_symbol')
        validate_plan(project, allowed, plan.get('change_scope'))
        if plan['target_check'] not in files or not plan['target_check'].startswith(('rsi/checks/','tests/')):
            raise ValueError('目标检查必须是本项目封存的检查文件')
        if plan['business_suite'] not in files or not plan['business_suite'].startswith('evals/'):
            raise ValueError('业务测试集必须预先固定在 evals')
        checks = plan['target_expected_checks']
        if not checks or len(checks)!=len(set(checks)) or any(not isinstance(c,str) for c in checks):
            raise ValueError('目标检查身份必须非空且不重复')
        for key in ['require_existing_regressions','require_target_failure_on_baseline',
                    'require_target_success_on_candidate','require_live_business_success']:
            if type(plan['policy'].get(key)) is not bool: raise ValueError('接受规则缺少明确布尔设置：'+key)
        if plan['policy'].get('merge')!='maintainer_only': raise ValueError('本工具不支持自动合并')
        directory = (output or project/'.data/rsi/experiments'/(datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid4().hex[:6])).resolve()
        if not directory.is_relative_to(project/'.data/rsi'): raise ValueError('实验输出必须位于本项目 .data/rsi')
        directory.mkdir(parents=True,exist_ok=False)
        baseline = snapshot(project,directory/'baseline',runtime_only=True)
        controller = snapshot(project,directory/'controller')
        request = None
        if evidence:
            evidence.freeze(directory/'controller')
            controller = manifest(directory/'controller')
            plan['source_case'] = evidence.reference
            request = evidence.candidate_request(plan)
            write_json(directory/'candidate-request.json', request)
        write_json(directory/'plan.json',plan)
        write_json(directory/'state.json',{'project':str(project),'status':'prepared','plan_sha256':digest(plan),
            'baseline_manifest':baseline,'controller_manifest':controller,'kernel_manifest':kernel_manifest(),
            'candidate_manifest':None,'candidate_request_sha256':digest(request) if request else None})
        return cls(directory)

    def state(self): return read(self.directory/'state.json')

    def verify(self):
        state = self.state()
        if kernel_manifest()!=state['kernel_manifest']: raise ValueError('执行中的实验控制代码已变化；请创建新实验')
        for directory,key in [('baseline','baseline_manifest'),('controller','controller_manifest')]:
            if manifest(self.directory/directory)!=state[key]: raise ValueError('封存的源码或验收器已变化：'+directory)
        if digest(read(self.directory/'plan.json'))!=state['plan_sha256']: raise ValueError('封存计划已变化')
        if state.get('candidate_request_sha256') and digest(read(self.directory/'candidate-request.json'))!=state['candidate_request_sha256']:
            raise ValueError('冻结的候选输入已变化')
        if state['candidate_manifest'] is not None and manifest(self.directory/'candidate')!=state['candidate_manifest']:
            raise ValueError('候选在固定后发生变化')
        return state

    def propose(self, configuration, local_proposal: Path | None = None):
        state = self.verify()
        if state['candidate_manifest'] is not None: raise ValueError('已有固定候选；不能在看到验收结果后覆盖候选')
        plan = read(self.directory/'plan.json')
        try:
            if plan.get('source_case') and local_proposal is None:
                raise ValueError('案例实验需要本地 --proposal，不执行源码外发')
            if local_proposal:
                proposal = read(local_proposal)
                validate_proposal_source(plan, proposal)
                write_json(self.directory/'proposal.json',proposal)
                write_json(self.directory/'generation.json',{'producer':'local_import',
                    'declared_provenance':proposal.get('provenance'),
                    'external_request_made':False,'proposal_sha256':digest(proposal)})
            else:
                proposal = generate(plan,self.directory/'baseline',configuration,self.directory)
        except Exception as exc:
            write_json(self.directory/'failure.json',{'type':type(exc).__name__,'stage':'proposal'})
            raise
        try:
            difference = create_candidate(self.directory/'baseline',self.directory/'candidate',proposal,
                                          plan['allowed_files'],plan.get('change_scope'))
        except ValueError as exc:
            write_json(self.directory/'failure.json',{'type':type(exc).__name__,
                'stage':'candidate_validation','message':str(exc)})
            raise
        scope = validate_candidate(self.directory/'baseline',proposal['changes'],plan.get('change_scope'))
        write_json(self.directory/'scope-audit.json',scope)
        (self.directory/'candidate.diff').write_text(difference)
        state.update(status='candidate_frozen',candidate_manifest=manifest(self.directory/'candidate'))
        write_json(self.directory/'state.json',state)

    def evaluate(self, configuration):
        # 入口保持明确不可执行；不能将历史双服务评测当作新架构验证。
        require_isolated_execution()

    def review(self):
        state = self.verify()
        plan = read(self.directory/'plan.json')
        results = read(self.directory/'results.json') if (self.directory/'results.json').exists() else {}
        decision = assess(results,plan['policy'],plan['target_expected_checks'])
        commits = git_review(self.directory,plan['allowed_files']) if state['candidate_manifest'] is not None else None
        human = read(self.directory/'human-decision.json') if (self.directory/'human-decision.json').exists() else 'pending'
        review = {'plan_sha256':state['plan_sha256'],'baseline_sha256':digest(state['baseline_manifest']),
            'candidate_sha256':digest(state['candidate_manifest']),'controller_sha256':digest(state['controller_manifest']),
            'recommendation':decision,'generation':read(self.directory/'generation.json') if (self.directory/'generation.json').exists() else None,
            'human_decision':human,'commits':commits,'merged':False,
            'change_scope':read(self.directory/'scope-audit.json') if (self.directory/'scope-audit.json').exists() else None,
            'source_case':plan.get('source_case'),
            'source_case_replay':'not_automatic_use_target_check' if plan.get('source_case') else None,
            'planning':plan.get('planning'),
            'candidate_request_sha256':state.get('candidate_request_sha256'),
            'scope':'单个健壮性修复、固定回归和真实小任务集；无开放域整体质量或性能收益结论。'}
        write_json(self.directory/'review.json',review)
        lines = ['# 代码候选评审包','',plan['objective'],'',
            *(['来源案例：`'+plan['source_case']['case_id']+'`；来源、反馈与独立要求见封存 controller/'+
               plan['source_case']['evidence_directory']+'。',''] if plan.get('source_case') else []),
            '建议：**'+decision['decision']+'**','',*['- '+reason for reason in decision['reasons']],'',
            '源码差异：`candidate.diff`；候选说明：`proposal.json`；修改范围：`scope-audit.json`；逐项执行证据：`runs/`。','',
            '基线 SHA256：`'+review['baseline_sha256']+'`','候选 SHA256：`'+review['candidate_sha256']+'`','',
            '人工决定：'+('待维护者评审' if human=='pending' else human['decision'])+'。主项目源码没有被应用、提交或合并。',
            '独立候选提交见 `git-review/`，可交付补丁 `candidate.patch` 和离线提交包 `candidate.bundle`。',
            '这是一轮具名代码候选的实验，不证明其他场景、任意仓库或持续自动优化已经完成。','']
        (self.directory/'review.md').write_text('\n'.join(lines))
        return review

    def record_decision(self, decision: str, reviewer: str, notes: str):
        self.verify()
        if decision not in {'accept','reject'} or not reviewer.strip() or not notes.strip():
            raise ValueError('人工决定需要 accept/reject、维护者和理由')
        path = self.directory/'human-decision.json'
        if path.exists(): raise ValueError('已有人工决定，不能静默覆盖审阅记录')
        write_json(path,{'decision':decision,'reviewer':reviewer,'notes':notes,
                        'recorded_at':datetime.now(timezone.utc).isoformat()})
        return self.review()


def target_passed(run, expected_checks):
    target = run['engineering']['target']
    observation = target.get('observation')
    return bool(target_observed(run,expected_checks) and
                all(value is True for value in observation['checks'].values()) and observation['passed'] and target['returncode']==0)


def target_observed(run, expected_checks):
    observation = run['engineering']['target'].get('observation')
    return bool(observation and set(observation.get('checks',{}))==set(expected_checks) and
                all(type(v) is bool for v in observation['checks'].values()))


def assess(results: dict, policy: dict, expected_checks: list[str]) -> dict:
    # backend/agent是历史封存报告的字段；读取兼容不代表当前仍启动双服务。
    if set(results)!={'baseline','candidate'}:
        return {'decision':'inconclusive','reasons':['基线或候选执行证据缺失']}
    baseline,candidate = results['baseline'],results['candidate']
    if policy['require_existing_regressions'] and not all(baseline['engineering'][s]['passed'] for s in ['backend','agent']):
        return {'decision':'inconclusive','reasons':['基线既有回归没有全部通过，暂不能比较候选']}
    if policy['require_existing_regressions'] and not all(candidate['engineering'][s]['passed'] for s in ['backend','agent']):
        return {'decision':'reject','reasons':['候选造成既有工程回归失败']}
    if policy['require_target_failure_on_baseline'] and target_passed(baseline,expected_checks):
        return {'decision':'inconclusive','reasons':['基线没有复现目标缺口，不能宣称本轮修复有效']}
    if not target_observed(baseline,expected_checks):
        return {'decision':'inconclusive','reasons':['基线目标检查缺少有效观测，不把环境失败当作已复现缺口']}
    if policy['require_target_success_on_candidate'] and not target_passed(candidate,expected_checks):
        return {'decision':'reject','reasons':['候选没有通过预先固定的目标检查']}
    if policy['require_live_business_success'] and not all(r.get('business',{}).get('passed') for r in [baseline,candidate]):
        return {'decision':'inconclusive','reasons':['真实业务任务缺少完整配对通过证据']}
    left,right = [r.get('business',{}).get('results',[]) for r in [baseline,candidate]]
    if [(r['case_id'],r['step']) for r in left]!=[(r['case_id'],r['step']) for r in right]:
        return {'decision':'inconclusive','reasons':['基线与候选任务集合不一致']}
    return {'decision':'recommend_accept','reasons':['本轮预先固定的检查规则已满足',
        '满足本轮预先固定的工程与业务规则；具体执行证据见 results.json','建议交维护者评审；不自动应用或合并']}
