"""实验期间的模型转发；个人 OAuth 令牌始终由可信控制进程持有。"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
import time
from typing import Callable

import httpx

OFFICIAL_RESPONSES = 'https://api.openai.com/v1/responses'
MAX_BODY_BYTES = 1024 * 1024
ALLOWED_FIELDS = {'model', 'input', 'instructions', 'include', 'tools', 'tool_choice',
                  'parallel_tool_calls', 'stream', 'store', 'reasoning', 'text'}


class GatewayRejected(ValueError):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code, self.status = code, status


@dataclass
class ModelGateway:
    model: str
    token_provider: Callable[[], str] = field(repr=False)
    http: httpx.Client = field(repr=False)
    max_requests: int = 32
    ttl_seconds: float = 900
    credential: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)

    def __post_init__(self):
        if self.max_requests < 1 or self.ttl_seconds <= 0:
            raise ValueError('实验请求预算和有效期必须为正')
        self.deadline = time.monotonic() + self.ttl_seconds
        self.requests, self.denials = [], []
        self.lock = threading.Lock()
        self.server = None
        self.closed = False

    def admit(self, path: str, authorization: str, body: bytes) -> dict:
        if path != '/v1/responses':
            raise GatewayRejected('evaluation_route_denied', 404)
        if not secrets.compare_digest(authorization.encode(), ('Bearer ' + self.credential).encode()):
            raise GatewayRejected('evaluation_credential_invalid', 401)
        if self.closed or time.monotonic() >= self.deadline:
            raise GatewayRejected('evaluation_credential_expired', 403)
        if len(body) > MAX_BODY_BYTES:
            raise GatewayRejected('evaluation_body_too_large', 413)
        try:
            data = json.loads(body)
        except (ValueError, UnicodeError) as exc:
            raise GatewayRejected('evaluation_invalid_json') from exc
        if not isinstance(data, dict) or set(data) - ALLOWED_FIELDS:
            raise GatewayRejected('evaluation_unsupported_fields')
        if data.get('model') != self.model:
            raise GatewayRejected('evaluation_model_denied')
        if data.get('store') is not False or data.get('stream') is not True or not isinstance(data.get('input'), list):
            raise GatewayRejected('evaluation_request_contract')
        tools = data.get('tools', [])
        if not isinstance(tools, list) or any(not isinstance(tool, dict) or tool.get('type') != 'namespace'
                or not isinstance(tool.get('tools'), list) or any(not isinstance(inner, dict)
                or inner.get('type') not in {'function', 'custom'} for inner in tool['tools']) for tool in tools):
            raise GatewayRejected('evaluation_tool_denied')
        with self.lock:
            if self.closed or time.monotonic() >= self.deadline:
                raise GatewayRejected('evaluation_credential_expired', 403)
            if len(self.requests) >= self.max_requests:
                raise GatewayRejected('evaluation_request_limit', 429)
            receipt = {'index': len(self.requests) + 1, 'http_status': None, 'request_id': None}
            self.requests.append(receipt)
        return receipt

    def _handler(self):
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def error(self, rejection: GatewayRejected):
                with gateway.lock:
                    gateway.denials.append(rejection.code)
                body = json.dumps({'error': {'message': rejection.code, 'code': rejection.code,
                                            'type': 'invalid_request_error'}}).encode()
                self.send_response(rejection.status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self.error(GatewayRejected('evaluation_method_denied', 405))

            def do_POST(self):
                self.connection.settimeout(5)
                try:
                    size = int(self.headers.get('Content-Length', '-1'))
                    if size < 0 or size > MAX_BODY_BYTES:
                        raise GatewayRejected('evaluation_body_too_large', 413)
                    body = self.rfile.read(size)
                    receipt = gateway.admit(self.path, self.headers.get('Authorization', ''), body)
                except GatewayRejected as exc:
                    self.error(exc)
                    return
                except (ValueError, OSError):
                    self.error(GatewayRejected('evaluation_invalid_body'))
                    return
                headers_sent = False
                try:
                    token = gateway.token_provider()
                    with gateway.http.stream('POST', OFFICIAL_RESPONSES,
                            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
                            content=body) as upstream:
                        receipt.update(http_status=upstream.status_code,
                                       request_id=upstream.headers.get('x-request-id'))
                        self.send_response(upstream.status_code)
                        self.send_header('Content-Type', upstream.headers.get('content-type', 'application/json'))
                        for key in ['x-request-id', 'retry-after']:
                            if key in upstream.headers:
                                self.send_header(key, upstream.headers[key])
                        self.send_header('Connection', 'close')
                        self.end_headers()
                        headers_sent = True
                        for chunk in upstream.iter_bytes():
                            self.wfile.write(chunk)
                            self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    receipt['transport_error'] = 'client_disconnected'
                except Exception as exc:
                    receipt['transport_error'] = type(exc).__name__
                    if not headers_sent:
                        self.error(GatewayRejected('evaluation_upstream_unavailable', 502))
                finally:
                    self.close_connection = True

        return Handler

    def __enter__(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), self._handler())
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.05}, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.closed = True
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def environment(self) -> dict:
        return {'AGENT_AUTH_MODE': 'chatgpt', 'AGENT_MODEL': self.model,
                'RSI_MODEL_GATEWAY_URL': f'http://127.0.0.1:{self.server.server_port}/v1',
                'RSI_MODEL_GATEWAY_TOKEN': self.credential}

    @property
    def proof(self) -> dict:
        return {'transport': 'evaluation_gateway', 'model': self.model,
                'upstream': OFFICIAL_RESPONSES, 'max_requests': self.max_requests,
                'ttl_seconds': self.ttl_seconds, 'max_body_bytes': MAX_BODY_BYTES,
                'requests': list(self.requests), 'denials': list(self.denials), 'closed': self.closed,
                'scope': '路由、模型、次数、大小和时效受限；HTTP 成功不代表模型或任务完成。'}


@dataclass
class EvaluationModel:
    environment: dict = field(repr=False)
    denied_reads: list[Path] = field(default_factory=list)
    gateway: ModelGateway | None = field(default=None, repr=False)

    @property
    def proof(self) -> dict:
        return self.gateway.proof if self.gateway else {'transport': 'configured_api_key'}


@contextmanager
def evaluation_model(project: Path, configuration: dict, max_requests: int):
    mode = configuration.get('AGENT_AUTH_MODE', 'api_key')
    if mode == 'api_key':
        fields = ['OPENAI_API_KEY', 'OPENAI_BASE_URL', 'AGENT_MODEL']
        yield EvaluationModel({name: configuration[name] for name in fields if configuration.get(name)})
        return
    if mode != 'chatgpt':
        raise ValueError('不支持的实验认证方式；不会回退到 API Key')
    directory = Path(configuration.get('CHATGPT_AUTH_DIR', '.data/chatgpt'))
    if not directory.is_absolute():
        directory = project / directory
    account = configuration.get('CHATGPT_ACCOUNT', 'default')
    path = directory / f'{account}.json'
    if not path.is_file():
        raise ValueError('未找到项目订阅凭证；不会回退到旧 API Key 配置')
    # 仅可信父进程导入主项目认证；不从候选目录导入或向子进程复制令牌。
    from agent_service.agents.chatgpt_auth import ChatGPTAuth, CredentialStore

    with httpx.Client(timeout=25, follow_redirects=False) as auth_http:
        store = CredentialStore(directory, account)
        auth = ChatGPTAuth(store, auth_http)
        if not store.load().plan_enabled:
            raise ValueError('项目订阅调用未获授权；不会回退到旧 API Key 配置')
        with httpx.Client(timeout=90, follow_redirects=False) as upstream:
            with ModelGateway(configuration['AGENT_MODEL'], auth.access_token, upstream,
                              max_requests=max_requests) as gateway:
                yield EvaluationModel(gateway.environment, [path.resolve()], gateway)
