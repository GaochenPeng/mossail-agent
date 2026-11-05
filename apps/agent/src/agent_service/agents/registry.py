"""每次启动装配模型与场景工厂；运行时状态仍按任务创建。"""
from __future__ import annotations

import hashlib
import importlib.metadata
import inspect
import os
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from agent_runtime.research import ResearchSession
from agent_runtime.research_providers import SerperReader, SerperSearch
from agent_runtime.runtime import AgentRuntime, AgentSpec
from agent_runtime.tools import calculate
from open_protocol import AgentInput

from .model_access import connect_model


@dataclass
class AgentRegistry:
    model: str | None
    research_configured: bool
    execution: dict
    factories: dict = field(default_factory=dict)


def source_hash():
    inputs = []
    for name, root in [('runtime', Path(inspect.getfile(AgentRuntime)).parent),
                       ('server', Path(__file__).resolve().parents[1])]:
        for path in sorted(root.rglob('*.py')):
            inputs.append(f'{name}/{path.relative_to(root)}'.encode() + path.read_bytes())
    inputs.append(Path(inspect.getfile(AgentInput)).read_bytes())
    return hashlib.sha256(b''.join(inputs)).hexdigest()


@asynccontextmanager
async def model_resources():
    model = os.getenv('AGENT_MODEL')
    execution = {
        'model': model, 'sdk_version': importlib.metadata.version('openai-agents'),
        'source_sha256': source_hash(), 'protocol_version': 2,
        'auth_mode': os.getenv('AGENT_AUTH_MODE', 'api_key'),
    }
    registry = AgentRegistry(model, bool(os.getenv('SERPER_API_KEY')), execution)
    async with connect_model() as access, AsyncExitStack() as stack:
        if access is None:
            yield registry
            return
        execution['model_transport'] = access.transport
        spec = AgentSpec('assistant',
            '你是一个简洁的中文助手。涉及算术必须调用 calculate 工具，不能心算替代。'
            '回答时说明工具结果，不编造检索、文件或工具执行。', [calculate])
        registry.factories['assistant'] = lambda: access.runtime(spec, execution)
        if registry.research_configured:
            http = await stack.enter_async_context(httpx.AsyncClient(timeout=35, follow_redirects=False))
            search = SerperSearch(http, os.environ['SERPER_API_KEY'],
                                  os.getenv('SERPER_BASE_URL', 'https://google.serper.dev'))
            reader = SerperReader(http, os.environ['SERPER_API_KEY'],
                                  os.getenv('SERPER_SCRAPE_URL', 'https://scrape.serper.dev'))

            def research_runtime():
                session = ResearchSession(search, reader)
                return access.runtime(session.specification(), execution)

            registry.factories['deep_search'] = research_runtime
        yield registry
