"""固定合成问题的真实服务回归；按会话保留多轮依赖，保存通过和失败证据。"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def judge(task: dict, expected: dict) -> dict[str, bool]:
    items = list(task['snapshots'].values())
    answers = [v['meta_data'] for v in items if v['type']=='markdown' and v['sender']=='assistant' and v['status']=='success']
    tools = [v for v in items if v['type']=='tool_call']
    content = '\n'.join(v.get('content','') for v in answers)
    checks = {'task_complete': task['status']=='complete',
              'tools_complete': bool(tools) and all(v['status']=='success' for v in tools),
              'required_tools': set(expected['tool_names']) <= {v['meta_data'].get('name') for v in tools}}
    for pattern in expected.get('answer_patterns', []):
        checks['answer:'+pattern] = bool(re.search(pattern, content, re.I))
    if 'tool_number' in expected:
        outputs = [v['meta_data'].get('output','') for v in tools if v['meta_data'].get('name')=='calculate']
        def matches(value):
            try: return Decimal(value)==Decimal(expected['tool_number'])
            except InvalidOperation: return False
        checks['exact_tool_number'] = any(matches(v) for v in outputs)
    if 'source_url' in expected:
        sources = [s for answer in answers for s in answer.get('source', [])]
        official = [s for s in sources if s['url'].rstrip('/')==expected['source_url'].rstrip('/')]
        checks['expected_primary_source'] = bool(official)
        checks['citations_resolve_to_read_sources'] = bool(sources) and all(
            s.get('content') and s.get('retrieved_at') and f"[{s['id']}]({s['url']})" in content for s in sources)
        evidence = '\n'.join(s.get('content','') for s in official)
        for pattern in expected.get('evidence_patterns', []):
            checks['evidence:'+pattern] = bool(re.search(pattern, evidence, re.I))
    return checks


def source_manifest(root=ROOT) -> dict:
    files = [p for base in ['apps','packages'] for p in (root/base).rglob('*')
             if p.is_file() and '.venv' not in p.parts and '__pycache__' not in p.parts
             and p.suffix in {'.py','.js','.html','.toml','.lock'}]
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8010')
    parser.add_argument('--suite', type=Path, default=ROOT/'evals/reference_cases.json')
    parser.add_argument('--case', action='append', dest='cases')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--source-root',type=Path,default=ROOT)
    args = parser.parse_args()
    suite = json.loads(args.suite.read_text())
    selected = [c for c in suite['cases'] if not args.cases or c['id'] in args.cases]
    if not selected or any(not c['steps'] for c in selected) or (args.cases and set(args.cases)-{c['id'] for c in selected}):
        parser.error('未找到指定用例')
    output = args.output or ROOT/'.data/evals'/(datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid4().hex[:6])
    output.mkdir(parents=True, exist_ok=False)

    def request(path, body=None):
        req = Request(args.url+path, data=json.dumps(body).encode() if body is not None else None,
                      headers={'Content-Type': 'application/json'})
        with urlopen(req, timeout=10) as response: return json.load(response)

    health = request('/health')
    manifest = source_manifest(args.source_root)
    report = {'created_at': datetime.now(timezone.utc).isoformat(), 'suite_sha256':hashlib.sha256(args.suite.read_bytes()).hexdigest(),
              'runtime': health, 'source_files': manifest,'evaluator_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'expected_steps':sum(len(c['steps']) for c in selected), 'results': [],
              'boundary':'固定小样本的工程与事实检查，不代表开放域整体质量、医学质量或优化收益。'}
    (output/'suite.json').write_text(json.dumps(suite,ensure_ascii=False,indent=2)+'\n')
    for case in selected:
        thread = request('/threads', {})['thread_id']
        for index, step in enumerate(case['steps']):
            started = time.monotonic()
            accepted = request(f'/threads/{thread}/tasks', {'agent':step.get('agent',case['agent']),'question':step['question']})
            while True:
                task = request('/tasks/'+accepted['task_id'])
                if task['status'] in {'complete','failed','abandon'} or time.monotonic()-started>=180:
                    break
                time.sleep(.5)
            if task['status'] not in {'complete','failed','abandon'}:
                request('/tasks/'+task['id']+'/cancel', {})
                task = request('/tasks/'+task['id'])
            checks = judge(task, step['expect'])
            name = case['id']+'-'+str(index+1)
            result = {'case_id':case['id'],'step':index+1,'thread_id':thread,'task_id':task['id'],
                'status':task['status'],'passed':all(checks.values()),'checks':checks,
                'elapsed_seconds':round(time.monotonic()-started,2),'tags':case['tags'],'artifact':name+'.json'}
            (output/(name+'.json')).write_text(json.dumps(task,ensure_ascii=False,indent=2)+'\n')
            report['results'].append(result)
            print(json.dumps(result,ensure_ascii=False),flush=True)
            if task['status']!='complete': break  # 保留会话依赖，不将失败前提下的追问作为独立题继续运行。
    report['source_unchanged_during_run'] = manifest==source_manifest(args.source_root)
    report['passed'] = (len(report['results'])==report['expected_steps'] and
                        all(r['passed'] for r in report['results']) and report['source_unchanged_during_run'])
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print('验收报告：'+str(output/'report.json'),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':
    sys.exit(main())
