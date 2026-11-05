"""从本地在线任务记录导出可复用案例；默认保存在本地待审目录。"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from urllib.request import urlopen
from uuid import uuid4


def build_case(tasks: list[dict], origin: str) -> dict:
    if not tasks:
        raise ValueError('会话没有任务')
    tags = {'multi_turn'} if len(tasks)>1 else {'single_turn'}
    turns = []
    for task in tasks:
        items = list(task['snapshots'].values())
        tools = [v['meta_data'] for v in items if v['type']=='tool_call']
        answers = [v['meta_data'] for v in items if v['type']=='markdown' and v['sender']=='assistant' and v['status']=='success']
        execution = next((v['meta_data']['execution'] for v in items if v['sender']=='user' and 'execution' in v['meta_data']), None)
        if task['agent']=='deep_search': tags.add('web_research')
        if any(t.get('name')=='calculate' for t in tools): tags.add('arithmetic')
        if task['status']!='complete': tags.add('incomplete_or_failed')
        if any(a.get('source') for a in answers): tags.add('citations')
        turns.append({'task_id':task['id'],'agent':task['agent'],'status':task['status'],
            'created_at':task.get('created_at'),'input':task['question'],
            'answer':'\n'.join(a.get('content','') for a in answers),'error':task['error'],
            'tools':tools,'sources':[s for a in answers for s in a.get('source',[])],
            'execution':execution})
    raw = json.dumps(tasks,ensure_ascii=False,sort_keys=True).encode()
    return {'schema_version':1,'case_id':uuid4().hex,'thread_id':tasks[0]['thread_id'],
        'origin':origin,'exported_at':datetime.now(timezone.utc).isoformat(),
        'tags':sorted(tags),'classification_method':'按场景、工具和任务状态的确定性规则',
        'publication_status':'local_draft','review':{'rating':'unreviewed','notes':None},
        'evaluation_criteria':None,'raw_tasks_sha256':hashlib.sha256(raw).hexdigest(),'turns':turns}


def render_case(case: dict) -> str:
    review = case['review']
    if review.get('rating')=='unreviewed':
        review_line = '尚未提供人工评价或独立验收标准。'
    else:
        review_line = ('自动核验' if review.get('kind')=='automated' else '人工反馈')+'：'+review['rating']+'；'+review.get('notes','')
        if review.get('kind')=='automated': review_line += '；人工评价仍待提供。'
    lines = ['# Agent 任务案例','',f"案例：{case['case_id']}",f"来源：{case['origin']}",
             '分类：'+', '.join(case['tags']),'状态：本地草稿，尚未发表；'+review_line,'']
    for index,turn in enumerate(case['turns'],1):
        lines += [f'## 第 {index} 轮','',f"任务：{turn['task_id']}；场景：{turn['agent']}；状态：{turn['status']}",'',
                  '输入：','',turn['input'],'','回答：','',turn['answer'] or turn['error'] or '没有成功回答','']
        if turn['sources']:
            lines += ['来源：','']+[f"- [{s['id']}: {s['title']}]({s['url']})" for s in turn['sources']]+['']
        lines += ['工具：'+(', '.join(t.get('name','unknown') for t in turn['tools']) or '无'),'']
        if turn['execution']:
            lines += ['执行信息：`'+json.dumps(turn['execution'],ensure_ascii=False,sort_keys=True)+'`','']
    lines += ['完整工具输入输出、读取正文与执行信息见 `case.json`；原始消息快照见 `raw-tasks.json`。','',
              '历史模型回答属于执行结果。进入评测集前应另外补充独立验收要求，不能将这份回答直接当作标准答案。','']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url',default='http://127.0.0.1:8010')
    parser.add_argument('--thread-id',required=True)
    parser.add_argument('--origin',required=True,choices=['synthetic','online'])
    parser.add_argument('--output',type=Path)
    args = parser.parse_args()
    with urlopen(args.url+'/threads/'+args.thread_id+'/history',timeout=10) as response:
        tasks = json.load(response)
    case = build_case(tasks,args.origin)
    output = args.output or Path('.data/cases')/case['case_id']
    output.mkdir(parents=True,exist_ok=False)
    (output/'raw-tasks.json').write_text(json.dumps(tasks,ensure_ascii=False,indent=2)+'\n')
    (output/'case.json').write_text(json.dumps(case,ensure_ascii=False,indent=2)+'\n')
    (output/'case.md').write_text(render_case(case))
    print(json.dumps({'case_id':case['case_id'],'origin':case['origin'],'tags':case['tags'],
                     'publication_status':case['publication_status'],'output':str(output)},ensure_ascii=False))


if __name__=='__main__':
    main()
