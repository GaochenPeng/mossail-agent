"""固定源码、验证修改范围并形成独立候选目录。"""
from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil

from .change_scope import validate_candidate, validate_plan


def digest(value) -> str:
    data = value if isinstance(value, bytes) else json.dumps(value,sort_keys=True,ensure_ascii=False).encode()
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


def source_files(root: Path, runtime_only=False) -> list[Path]:
    files = []
    directories = ['apps','packages'] if runtime_only else ['apps','packages','scripts','tests','evals','rsi']
    for directory in directories:
        for path in (root/directory).rglob('*'):
            relative = path.relative_to(root)
            if any(part in {'.venv','__pycache__','node_modules','.pytest_cache'} for part in relative.parts):
                continue
            if path.is_symlink():
                raise ValueError('源码快照不接受符号链接：'+str(relative))
            if path.is_file() and path.suffix in {'.py','.js','.html','.toml','.json','.lock','.md'}:
                files.append(path)
    for name in ['pytest.ini','.python-version']:
        if (root/name).is_file(): files.append(root/name)
    return sorted(files)


def manifest(root: Path) -> dict[str,str]:
    return {str(p.relative_to(root)):digest(p.read_bytes()) for p in source_files(root)}


def snapshot(root: Path, target: Path, runtime_only=False) -> dict[str,str]:
    target.mkdir(parents=True,exist_ok=False)
    for path in source_files(root,runtime_only):
        destination = target/path.relative_to(root)
        destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(path,destination)
    return manifest(target)


def validate_changes(proposal: dict, allowed_files: list[str]) -> list[dict]:
    changes = proposal.get('changes')
    if not isinstance(changes,list) or not changes:
        raise ValueError('候选没有代码修改')
    seen = set()
    for change in changes:
        name = change['path']
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or str(path)!=name or name not in allowed_files or name in seen:
            raise ValueError('候选超出冻结的修改范围或路径重复')
        if not isinstance(change.get('content'),str): raise ValueError('候选文件内容必须是文本')
        seen.add(name)
    return changes


def create_candidate(baseline: Path, target: Path, proposal: dict, allowed_files: list[str],
                     change_scope: dict | None = None) -> str:
    changes = validate_changes(proposal,allowed_files)
    validate_plan(baseline, allowed_files, change_scope)
    validate_candidate(baseline, changes, change_scope)
    snapshot(baseline,target)
    diff = []
    for change in changes:
        path = target/change['path']
        before = path.read_text()
        after = change['content']
        if before==after: raise ValueError('候选没有改变文件内容')
        path.write_text(after)
        diff.extend(difflib.unified_diff(before.splitlines(keepends=True),after.splitlines(keepends=True),
            fromfile='a/'+change['path'],tofile='b/'+change['path']))
    return ''.join(diff)
