"""目录之外也约束依赖方向，避免职责再次堆回一个 Controller。"""
import ast
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2] / 'apps/agent/src/agent_service'
FORBIDDEN = {
    'models': ('agent_service.controllers', 'agent_service.services', 'agent_service.repositories',
               'agent_service.agents', 'fastapi', 'asyncpg', 'redis', 'agents', 'agent_runtime'),
    'controllers': ('agent_service.repositories', 'agent_runtime', 'asyncpg', 'redis', 'agents'),
    'services': ('agent_service.controllers', 'agent_service.repositories', 'fastapi', 'asyncpg', 'redis'),
    'repositories': ('agent_service.controllers', 'agent_service.services', 'agent_service.agents',
                     'fastapi', 'agent_runtime'),
    'agents': ('agent_service.controllers', 'agent_service.services', 'agent_service.repositories'),
}


def test_mvc_modules_keep_dependency_direction():
    for layer, forbidden in FORBIDDEN.items():
        for path in (ROOT / layer).rglob('*.py'):
            package = 'agent_service.' + '.'.join(path.relative_to(ROOT).parts[:-1])
            for node in ast.walk(ast.parse(path.read_text())):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    name = '.' * node.level + (node.module or '')
                    names = [importlib.util.resolve_name(name, package) if node.level else name]
                for name in names:
                    assert not any(name == prefix or name.startswith(prefix + '.') for prefix in forbidden), (
                        f'{path.relative_to(ROOT)}:{node.lineno} 反向依赖 {name}')
