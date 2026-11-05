"""把实验源码形成独立 Git 评审提交；不触碰主项目 Git 或推送远端。"""
from __future__ import annotations

import os
import json
from pathlib import Path
import shutil
import subprocess

from .workspace import manifest, write_json


def git_review(directory: Path, allowed_files: list[str]) -> dict:
    metadata = directory/'review-commits.json'
    repository = directory/'git-review'
    if not metadata.exists(): shutil.copytree(directory/'baseline',repository)
    home = directory/'git-home';home.mkdir(exist_ok=True)
    env = {'PATH':os.environ.get('PATH','/usr/bin:/bin'),'HOME':str(home),
           'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null','GIT_TERMINAL_PROMPT':'0'}

    def git(*args):
        result = subprocess.run(['git','-c','user.name=RSI candidate','-c','user.email=rsi@example.invalid',
            '-c','commit.gpgSign=false','-c','core.hooksPath=/dev/null',*args],cwd=repository,env=env,
            check=True,capture_output=True,text=True)
        return result.stdout

    if metadata.exists():
        proof = json.loads(metadata.read_text())
        if git('rev-parse','HEAD').strip()!=proof['candidate_commit'] or manifest(repository)!=manifest(directory/'candidate'):
            raise ValueError('评审 Git 仓库已变化，不能复用旧提交证明')
        return proof

    git('init','-q','-b','baseline')
    git('add','--all');git('commit','-qm','Baseline source snapshot')
    baseline_commit = git('rev-parse','HEAD').strip()
    git('checkout','-qb','candidate')
    changed = []
    baseline_files = manifest(directory/'baseline')
    for name,sha in manifest(directory/'candidate').items():
        if sha!=baseline_files.get(name):
            if name not in allowed_files: raise ValueError('评审提交包含未授权文件')
            shutil.copy2(directory/'candidate'/name,repository/name)
            changed.append(name)
    git('add','--all');git('commit','-qm','Candidate code change for maintainer review')
    candidate_commit = git('rev-parse','HEAD').strip()
    names = git('diff','--name-only',baseline_commit,candidate_commit).splitlines()
    if sorted(names)!=sorted(changed): raise ValueError('Git 差异与固定候选不一致')
    if manifest(repository)!=manifest(directory/'candidate'): raise ValueError('评审 Git 源码与候选不一致')
    (directory/'candidate.patch').write_text(git('format-patch','-1','--stdout'))
    git('bundle','create',str(directory/'candidate.bundle'),'--all')
    proof = {'repository':str(repository),'baseline_commit':baseline_commit,'candidate_commit':candidate_commit,
             'changed_files':names,'scope':'独立实验仓库的本地提交；主项目未提交、未合并，未推送远端'}
    write_json(metadata,proof)
    return proof
