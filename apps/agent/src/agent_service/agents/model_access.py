"""模型与认证装配；业务 Agent 的工具和协议不依赖认证方式。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
import os
from pathlib import Path
from urllib.parse import urlsplit

from agents import ModelSettings, OpenAIResponsesModel, tool_namespace
from agents.exceptions import ModelBehaviorError
import httpx
from openai import AsyncOpenAI

from agent_runtime.runtime import AgentRuntime, AgentSpec
from .chatgpt_auth import ChatGPTAuth, CredentialStore, RESOURCE


class SubscriptionResponsesModel(OpenAIResponsesModel):
    async def stream_response(self, *args, **kwargs):
        completed = False
        async for event in super().stream_response(*args, **kwargs):
            completed |= event.type == 'response.completed'
            yield event
        if not completed:
            raise ModelBehaviorError('模型流结束但未收到 response.completed')


@dataclass
class ModelAccess:
    model: OpenAIResponsesModel
    auth_mode: str
    transport: str = 'direct'

    def runtime(self, spec: AgentSpec, execution: dict) -> AgentRuntime:
        settings = None
        if self.auth_mode == 'chatgpt':
            spec = replace(spec, tools=tool_namespace(name='open_repo',
                description='本项目提供的计算或网页研究工具。', tools=spec.tools))
            settings = ModelSettings(store=False, response_include=['reasoning.encrypted_content'])
        return AgentRuntime(self.model, spec, execution, model_settings=settings)


@dataclass
class ModelEndpoint:
    key: str | Callable[[], Awaitable[str]] = field(repr=False)
    base_url: str | None
    transport: str = 'direct'


def subscription_endpoint(auth_http: httpx.Client) -> ModelEndpoint | None:
    url, token = os.getenv('RSI_MODEL_GATEWAY_URL'), os.getenv('RSI_MODEL_GATEWAY_TOKEN')
    if url or token:
        try:
            parsed = urlsplit(url or '')
            valid = (bool(token) and parsed.scheme == 'http' and parsed.hostname == '127.0.0.1'
                     and parsed.port is not None and parsed.port > 0 and not parsed.username
                     and not parsed.password and not parsed.query and not parsed.fragment
                     and parsed.path.rstrip('/') == '/v1')
        except ValueError:
            valid = False
        if not valid:
            raise ValueError('RSI 模型转发必须是完整配置的本机 127.0.0.1 /v1 端点')
        return ModelEndpoint(token, url.rstrip('/'), 'evaluation_gateway')
    store = CredentialStore(Path(os.getenv('CHATGPT_AUTH_DIR', '.data/chatgpt')),
                            os.getenv('CHATGPT_ACCOUNT', 'default'))
    credentials = store.load()
    if not credentials.access_token or not credentials.plan_enabled:
        return None
    return ModelEndpoint(ChatGPTAuth(store, auth_http).async_access_token, RESOURCE)


@asynccontextmanager
async def connect_model():
    mode, model = os.getenv('AGENT_AUTH_MODE', 'api_key'), os.getenv('AGENT_MODEL')
    if mode not in {'api_key', 'chatgpt'}:
        raise ValueError('AGENT_AUTH_MODE 只允许 api_key 或 chatgpt')
    if not model or (mode == 'api_key' and not os.getenv('OPENAI_API_KEY')):
        yield None
        return
    with httpx.Client(timeout=25, follow_redirects=False) as auth_http:
        if mode == 'chatgpt':
            endpoint = subscription_endpoint(auth_http)
            if endpoint is None:
                yield None
                return
            model_class = SubscriptionResponsesModel
        else:
            if os.getenv('RSI_MODEL_GATEWAY_URL') or os.getenv('RSI_MODEL_GATEWAY_TOKEN'):
                raise ValueError('RSI 订阅转发不能与 api_key 认证混用')
            endpoint = ModelEndpoint(os.environ['OPENAI_API_KEY'], os.getenv('OPENAI_BASE_URL') or None)
            model_class = OpenAIResponsesModel
        async with AsyncOpenAI(api_key=endpoint.key, base_url=endpoint.base_url, timeout=90,
                               max_retries=0 if mode == 'chatgpt' else 1,
                               organization='', project='') as client:
            yield ModelAccess(model_class(model, client), mode, endpoint.transport)
