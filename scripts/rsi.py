"""离线代码候选 CLI：所有修改落入实验目录，主项目不被自动改写。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from rsi.experiment import Experiment
from rsi.execution import ExecutionUnavailable, require_isolated_execution
from scripts.dev import configuration


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action',choices=['prepare','propose','evaluate','review','decision','run'])
    parser.add_argument('--plan',type=Path)
    parser.add_argument('--case',type=Path,help='导入已评审失败案例；必须显式指定修复计划，不自动诊断')
    parser.add_argument('--experiment',type=Path)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--proposal',type=Path,help='导入当前Codex或人工准备的本地候选，不发送源码给其他模型')
    parser.add_argument('--decision',choices=['accept','reject'])
    parser.add_argument('--reviewer')
    parser.add_argument('--notes')
    args = parser.parse_args()
    if args.action in {'propose','run'} and not args.proposal:
        parser.error('当前项目只使用本地候选；请传 --proposal，不执行源码外发')
    if args.case and (args.action not in {'prepare','run'} or not args.plan):
        parser.error('--case 仅用于 prepare/run，并且需要显式 --plan；程序不自动选择修复目标')
    if args.action in {'evaluate', 'run'}:
        # 在读取模型配置、创建实验目录或访问外部资源之前拒绝未适配执行。
        require_isolated_execution()
    if args.action in {'prepare','run'}:
        plan = args.plan or ROOT/'rsi/plans/url-robustness.json'
        experiment = Experiment.prepare(ROOT,plan,args.output,case_directory=args.case)
        print('实验目录：'+str(experiment.directory),flush=True)
    elif args.experiment:
        experiment = Experiment(args.experiment.resolve())
    else: parser.error('此操作需要 --experiment')
    if args.action in {'propose','run'}:
        experiment.propose({},args.proposal);print('本地候选已导入并固定',flush=True)
    if args.action in {'evaluate','run'}:
        experiment.evaluate(configuration());print('固定检查与真实任务比较已结束',flush=True)
    if args.action in {'review','run'}:
        review = experiment.review()
        print(json.dumps(review,ensure_ascii=False,indent=2),flush=True)
        print('评审材料：'+str(experiment.directory/'review.md'),flush=True)
    if args.action=='decision':
        if not args.decision or not args.reviewer or not args.notes: parser.error('需要 --decision、--reviewer、--notes')
        experiment.record_decision(args.decision,args.reviewer,args.notes)
        print('人工决定已记录；主项目未自动应用或合并',flush=True)


if __name__=='__main__':
    try: main()
    except ExecutionUnavailable as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        print('实验操作失败：'+type(exc).__name__+'；请查看实验目录中的阶段记录。',file=sys.stderr)
        sys.exit(1)
