"""把已评审案例交给实验；这里只校验和保存材料，不诊断根因或生成代码。"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from scripts.review_case import reviewed_case
from .workspace import digest, write_json


EVIDENCE_PATH = 'evals/rsi-source-case-evidence'
CASE_FILES = ('case.json', 'raw-tasks.json', 'criteria.json', 'suite.json')


@dataclass(frozen=True)
class CaseEvidence:
    documents: dict

    @classmethod
    def load(cls, directory: Path) -> CaseEvidence:
        documents = {}
        for name in CASE_FILES:
            path = directory / name
            if path.is_symlink() or not path.is_file():
                raise ValueError('案例需要普通文件：' + name)
            documents[name] = json.loads(path.read_text())
        case, raw, criteria, suite = [documents[name] for name in CASE_FILES]
        review = case.get('review', {})
        if review.get('rating') != 'fail':
            raise ValueError('最小修复流程只接收已评审为 fail 的案例')
        if not case.get('case_id') or case.get('origin') not in {'synthetic', 'online'}:
            raise ValueError('案例需要明确身份和来源')
        if not raw or any(task['status'] not in {'complete', 'failed', 'abandon'} for task in raw):
            raise ValueError('案例必须保留已结束的任务，不能冻结仍在运行的会话')
        if any(task['thread_id'] != case['thread_id'] for task in raw):
            raise ValueError('案例与原始任务的会话身份不一致')
        if case.get('evaluation_criteria', {}).get('sha256') != digest(criteria):
            raise ValueError('案例绑定的独立验收要求发生变化')
        # 复用已有案例校验：重算原始快照、各轮材料和自动评价，保留人工评价的来源。
        _, expected_suite = reviewed_case(case, raw, criteria, kind=review.get('kind'),
            rating=review['rating'], reviewer=review.get('reviewer', ''), notes=review.get('notes', ''))
        if suite != expected_suite:
            raise ValueError('案例测试集与原始输入、独立要求不一致')
        return cls(documents)

    @property
    def reference(self) -> dict:
        case = self.documents['case.json']
        return {'case_id': case['case_id'], 'origin': case['origin'], 'thread_id': case['thread_id'],
            'task_ids': [turn['task_id'] for turn in case['turns']],
            'review_kind': case['review']['kind'], 'rating': case['review']['rating'],
            'raw_tasks_sha256': case['raw_tasks_sha256'],
            'criteria_sha256': digest(self.documents['criteria.json']),
            'evidence_directory': EVIDENCE_PATH}

    def freeze(self, controller: Path):
        destination = controller / EVIDENCE_PATH
        if destination.exists():
            raise ValueError('控制副本已占用案例证据目录，不能覆盖')
        for name, document in self.documents.items():
            write_json(destination / name, document)

    def candidate_request(self, plan: dict) -> dict:
        return {'schema_version': 1, 'source_case': self.reference,
            'objective': plan['objective'], 'planning': plan['planning'],
            'development_note': plan.get('development_note'),
            'allowed_files': plan['allowed_files'], 'change_scope': plan['change_scope'],
            'baseline_directory': 'baseline', 'controller_evidence_directory': 'controller/' + EVIDENCE_PATH,
            'verification_contract': {'source_case_suite': 'frozen_evidence_only',
                'defect_reproduction': plan['target_check'], 'preservation_regression': plan['business_suite']},
            'proposal_contract': {'source_case_id': self.reference['case_id'],
                'required_fields': ['source_case_id', 'rationale', 'changes', 'provenance'],
                'changes': '每项为允许路径 path 和完整文件文本 content',
                'provenance': '声明 producer 和 external_source_request=false'},
            'generation': {'mode': 'local_import', 'automatic_generation': False,
                'scope': '由本地候选提供方诊断并编写；程序不证明修改与案例之间的因果关系。'}}


def validate_planning(plan: dict):
    planning = plan.get('planning', {})
    if not isinstance(plan.get('objective'), str) or not plan['objective'].strip():
        raise ValueError('案例修复计划需要明确 objective')
    if not isinstance(planning, dict) or planning.get('mode') != 'local_manual':
        raise ValueError('当前计划由本地提供方编写，需要 planning.mode=local_manual')
    if not isinstance(planning.get('producer'), str) or not planning['producer'].strip():
        raise ValueError('需要 planning.producer 声明诊断和计划提供方')


def validate_proposal_source(plan: dict, proposal: dict):
    source = plan.get('source_case')
    if source is None:
        return
    if proposal.get('source_case_id') != source['case_id']:
        raise ValueError('候选必须关联本轮冻结的 source_case_id')
    provenance = proposal.get('provenance', {})
    if not isinstance(provenance, dict):
        raise ValueError('候选需要结构化的 provenance')
    if not isinstance(proposal.get('rationale'), str) or not proposal['rationale'].strip():
        raise ValueError('候选需要明确修改理由')
    if not isinstance(provenance.get('producer'), str) or not provenance['producer'].strip():
        raise ValueError('候选需要声明本地提供方')
    if provenance.get('external_source_request') is not False:
        raise ValueError('当前案例实验只接收声明未新增源码外发的本地候选')
