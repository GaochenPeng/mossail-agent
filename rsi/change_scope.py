"""在生成候选目录前检查修改单元；行为是否正确仍由独立验收判断。"""
from __future__ import annotations

import ast
import difflib
from pathlib import Path


def symbols(source: str) -> dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]]:
    found = {}

    def visit(body, prefix=''):
        for node in body:
            name = prefix + getattr(node, 'name', '')
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.setdefault(name, []).append(node)
            elif isinstance(node, ast.ClassDef):
                visit(node.body, name + '.')

    visit(ast.parse(source).body)
    return found


def selected(source: str, names: list[str]) -> dict:
    try:
        found = symbols(source)
    except SyntaxError as exc:
        raise ValueError('符号范围检查要求有效的 Python 源码') from exc
    if any(len(found.get(name, [])) != 1 for name in names):
        raise ValueError('计划指定的函数或方法不存在、被改名或有重复定义')
    return {name: found[name][0] for name in names}


def syntax(value):
    if isinstance(value, ast.AST):
        return ast.dump(value, include_attributes=False)
    if isinstance(value, list):
        return [syntax(item) for item in value]
    return value


def signature(node) -> str:
    # 参数默认值、返回注解、装饰器和同步/异步性质均属于签名契约。
    fields = [node.args, node.returns, node.decorator_list,
              getattr(node, 'type_params', []), getattr(node, 'type_comment', None)]
    return repr((type(node).__name__, [syntax(value) for value in fields]))


def outside_symbols(source: str, nodes: dict) -> str:
    lines = source.splitlines(keepends=True)
    for name, node in sorted(nodes.items(), key=lambda item: item[1].lineno, reverse=True):
        start = min([node.lineno] + [item.lineno for item in node.decorator_list]) - 1
        lines[start:node.end_lineno] = [f'<editable-symbol:{name}>\n']
    return ''.join(lines)


def validate_plan(root: Path, allowed_files: list[str], scope: dict | None):
    if scope is None:  # 历史计划保持文件级语义，不冒充已执行函数级限制。
        return
    if not isinstance(scope, dict) or scope.get('unit') not in {'file', 'python_symbol'}:
        raise ValueError('change_scope.unit 只允许 file 或 python_symbol')
    keys = {'unit'} if scope['unit'] == 'file' else {'unit', 'symbols', 'preserve_signatures'}
    if set(scope) != keys:
        raise ValueError('change_scope 字段不完整或存在未支持字段')
    if scope['unit'] == 'file':
        return
    if type(scope['preserve_signatures']) is not bool or not isinstance(scope['symbols'], dict):
        raise ValueError('符号范围需要明确的 symbols 和 preserve_signatures')
    if set(scope['symbols']) != set(allowed_files):
        raise ValueError('每个允许修改的文件必须明确列出符号范围')
    for name, names in scope['symbols'].items():
        if not name.endswith('.py') or not isinstance(names, list) or not names or any(
                not isinstance(item, str) or not item for item in names) or len(names) != len(set(names)):
            raise ValueError('符号范围只支持 Python 文件和非空、不重复的函数/方法名列表')
        selected((root / name).read_text(), names)


def validate_candidate(baseline: Path, changes: list[dict], scope: dict | None) -> dict:
    unit = scope['unit'] if scope else 'file'
    report = {'unit': unit, 'explicit_scope': scope is not None, 'files': [],
              'guarantee': '检查可修改单元和范围外文本；不证明行为正确或没有语义副作用。'}
    for change in changes:
        before, after = (baseline / change['path']).read_text(), change['content']
        if unit == 'python_symbol':
            names = scope['symbols'][change['path']]
            old, new = selected(before, names), selected(after, names)
            if outside_symbols(before, old) != outside_symbols(after, new):
                raise ValueError('候选修改了允许符号之外的内容：' + change['path'])
            if scope['preserve_signatures'] and any(signature(old[name]) != signature(new[name]) for name in names):
                raise ValueError('候选改变了受保护的函数签名：' + change['path'])
        else:
            names = []
        difference = list(difflib.unified_diff(before.splitlines(), after.splitlines()))[2:]
        report['files'].append({'path': change['path'], 'allowed_symbols': names,
            'added_lines': sum(line.startswith('+') for line in difference),
            'removed_lines': sum(line.startswith('-') for line in difference)})
    return report
