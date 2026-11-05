"""将独立验收与案例绑定，保存新版本并生成可运行测试集，不改原始任务。"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.evaluate import judge
from scripts.export_case import build_case, render_case


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def reviewed_case(case, raw_tasks, criteria, *, kind, rating, reviewer, notes):
    if kind not in {'human','automated'} or rating not in {'pass','fail'} or not reviewer.strip() or not notes.strip():
        raise ValueError('需要明确反馈来源、评价、审阅者和理由')
    if digest(raw_tasks)!=case['raw_tasks_sha256']:
        raise ValueError('原始任务快照发生变化')
    if build_case(raw_tasks,case['origin'])['turns']!=case['turns']:
        raise ValueError('案例执行材料与原始快照不一致')
    steps = criteria.get('steps',[])
    if len(steps)!=len(case['turns']): raise ValueError('每一轮都需要独立验收要求，不能丢弃多轮前提')
    if not criteria.get('reference'): raise ValueError('需要注明验收依据，不能默认历史答案即标准答案')
    observations=[]
    permitted={'tool_names','answer_patterns','tool_number','source_url','evidence_patterns'}
    for task,step in zip(raw_tasks,steps):
        expected=step['expect']
        if set(expected)-permitted or not expected.get('tool_names'):
            raise ValueError('验收字段无效或缺少工具要求')
        if not expected.get('answer_patterns') and 'tool_number' not in expected:
            raise ValueError('需要独立结果要求，不能只凭工具成功就通过')
        for pattern in expected.get('answer_patterns',[])+expected.get('evidence_patterns',[]): re.compile(pattern)
        checks=judge(task,expected)
        observations.append({'task_id':task['id'],'checks':checks,'passed':all(checks.values())})
    observed_rating='pass' if all(o['passed'] for o in observations) else 'fail'
    if kind=='automated' and rating!=observed_rating:
        raise ValueError('自动评价与实际确定性检查不一致')
    result=deepcopy(case)
    result['review']={'kind':kind,'rating':rating,'reviewer':reviewer,'notes':notes,
        'recorded_at':datetime.now(timezone.utc).isoformat(),'deterministic_observations':observations}
    result['evaluation_criteria']={'sha256':digest(criteria),'reference':criteria['reference']}
    suite={'version':1,'purpose':'由案例输入与独立验收绑定的回归，不以历史答案作为标准答案',
        'cases':[{'id':'case-'+case['case_id'],'agent':case['turns'][0]['agent'],'tags':case['tags'],
            'reference':criteria['reference'],'source_case':{'case_id':case['case_id'],'origin':case['origin'],
                'raw_tasks_sha256':case['raw_tasks_sha256'],'criteria_sha256':digest(criteria)},
            'steps':[{'agent':turn['agent'],'question':turn['input'],'expect':step['expect']}
                     for turn,step in zip(case['turns'],steps)]}]}
    return result,suite


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--case',type=Path,required=True)
    parser.add_argument('--criteria',type=Path,required=True)
    parser.add_argument('--kind',choices=['human','automated'],required=True)
    parser.add_argument('--rating',choices=['pass','fail'],required=True)
    parser.add_argument('--reviewer',required=True)
    parser.add_argument('--notes',required=True)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    case=json.loads((args.case/'case.json').read_text());raw=json.loads((args.case/'raw-tasks.json').read_text())
    criteria=json.loads(args.criteria.read_text())
    reviewed,suite=reviewed_case(case,raw,criteria,kind=args.kind,rating=args.rating,reviewer=args.reviewer,notes=args.notes)
    output=args.output or ROOT/'.data/cases/reviewed'/(case['case_id']+'-'+uuid4().hex[:6])
    output.mkdir(parents=True,exist_ok=False)
    for name,value in [('case.json',reviewed),('raw-tasks.json',raw),('criteria.json',criteria),('suite.json',suite)]:
        (output/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    (output/'case.md').write_text(render_case(reviewed))
    print(json.dumps({'case_id':reviewed['case_id'],'review_kind':args.kind,'rating':args.rating,
        'publication_status':reviewed['publication_status'],'suite':str(output/'suite.json')},ensure_ascii=False))


if __name__=='__main__': main()
