"""本项目独立的 ChatGPT OAuth；不读取 Codex 的登录文件。"""
from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
from urllib.parse import urlencode
from uuid import uuid4

import httpx
import jwt

AUTHORITY = 'https://auth.openai.com'
RESOURCE = 'https://api.openai.com/v1'
INITIAL_CLIENT = 'dynamic_agent_client'
PLAN_SCOPE = 'chatgpt.tokens.use.direct'
SCOPES = 'openid profile email offline_access resource.invoke ' + PLAN_SCOPE
UNUSABLE_REFRESH = {'invalid_grant', 'invalid_refresh_token', 'token_expired',
                    'refresh_token_expired', 'refresh_token_invalidated', 'refresh_token_reused'}


class AuthError(RuntimeError):
    """只包含可公开诊断信息，绝不拼接令牌或完整响应正文。"""
    def __init__(self, message: str, *, code: str = '', status: int | None = None,
                 request_id: str = ''):
        super().__init__(message)
        self.code, self.status, self.request_id = code, status, request_id


@dataclass(frozen=True)
class Registration:
    client_id: str = ''
    subject: str = ''
    email: str = ''


@dataclass
class Credentials:
    registration: Registration = field(default_factory=Registration)
    access_token: str = field(default='', repr=False)
    refresh_token: str = field(default='', repr=False)
    id_token: str = field(default='', repr=False)
    scopes: list[str] = field(default_factory=list)
    expires_at: float = 0
    earliest_refresh_at: float = 0

    @property
    def plan_enabled(self) -> bool:
        return PLAN_SCOPE in self.scopes and 'resource.invoke' in self.scopes

    def without_tokens(self) -> Credentials:
        return Credentials(registration=self.registration)


class CredentialStore:
    def __init__(self, directory: Path, account: str = 'default'):
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,47}', account):
            raise ValueError('账号标签只允许小写字母、数字、下划线和短横线，最长 48 字符')
        self.directory, self.account = directory, account
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        self.path = directory / f'{account}.json'

    @contextmanager
    def locked(self, name: str | None = None):
        path = self.directory / f'{name or self.account}.lock'
        with os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR, 0o600), 'r+') as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @staticmethod
    def write(path: Path, value: dict):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as output:
                temporary = Path(output.name)
                os.fchmod(output.fileno(), 0o600)
                json.dump(value, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def load(self) -> Credentials:
        if not self.path.exists():
            return Credentials()
        data = json.loads(self.path.read_text())
        return Credentials(**{**data, 'registration': Registration(**data['registration'])})

    def save(self, value: Credentials):
        self.write(self.path, asdict(value))

    def host_id(self) -> str:
        with self.locked('_host'):
            path = self.directory / '_host.json'
            if not path.exists():
                self.write(path, {'id': f'urn:uuid:{uuid4()}'})
            return json.loads(path.read_text())['id']


@dataclass(frozen=True)
class LoginAttempt:
    redirect_uri: str
    host_id: str
    previous: Credentials = field(repr=False)
    state: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    nonce: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    verifier: str = field(default_factory=lambda: secrets.token_urlsafe(48), repr=False)

    def authorize_url(self) -> str:
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).decode().rstrip('=')
        client_id = self.previous.registration.client_id
        params = {'client_id': client_id or INITIAL_CLIENT, 'redirect_uri': self.redirect_uri,
                  'ext_agent_host_id': self.host_id, 'response_type': 'code', 'scope': SCOPES,
                  'resource': RESOURCE, 'state': self.state, 'nonce': self.nonce,
                  'code_challenge_method': 'S256', 'code_challenge': challenge}
        if not client_id:
            params['agent_name_hint'] = 'Open Repo'
        return AUTHORITY + '/api/accounts/authorize?' + urlencode(params)

    def callback_client(self, query: dict[str, str]) -> str:
        if not secrets.compare_digest(query.get('state', ''), self.state):
            raise AuthError('授权回调 state 不匹配', code='invalid_state')
        if query.get('error'):
            raise AuthError('OpenAI 授权未完成，请重新登录', code='authorization_declined')
        client_id = query.get('client_id') or self.previous.registration.client_id
        if not query.get('code') or not client_id or client_id == INITIAL_CLIENT:
            raise AuthError('授权回调缺少 code 或应用 client_id', code='invalid_callback')
        if self.previous.registration.client_id and client_id != self.previous.registration.client_id:
            raise AuthError('回调的应用 client_id 与本次登录不匹配', code='client_mismatch')
        return client_id


class ChatGPTAuth:
    def __init__(self, store: CredentialStore, http: httpx.Client):
        self.store, self.http = store, http

    def begin(self, redirect_uri: str) -> LoginAttempt:
        return LoginAttempt(redirect_uri, self.store.host_id(), self.store.load())

    def _json(self, response: httpx.Response) -> dict:
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.is_error:
            error = data.get('error', {}) if isinstance(data, dict) else {}
            code = error.get('code', '') if isinstance(error, dict) else error
            code = code if isinstance(code, str) and re.fullmatch(r'[a-z0-9_]{1,100}', code) else 'http_error'
            raise AuthError(f'OpenAI 请求失败：HTTP {response.status_code}，{code}', code=code,
                            status=response.status_code, request_id=response.headers.get('x-request-id', ''))
        if not isinstance(data, dict):
            raise AuthError('OpenAI 返回了非预期 JSON', code='invalid_response')
        return data

    def _exchange(self, form: dict) -> dict:
        return self._json(self.http.post(AUTHORITY + '/api/accounts/oauth/token',
                                        data={**form, 'resource': RESOURCE}))

    def _identity(self, token: str, client_id: str, nonce: str | None = None) -> dict:
        try:
            header = jwt.get_unverified_header(token)
            jwks = self._json(self.http.get(AUTHORITY + '/.well-known/jwks.json'))
            key = next(item for item in jwks['keys'] if item.get('kid') == header.get('kid'))
            identity = jwt.decode(token, jwt.PyJWK.from_dict(key).key, algorithms=['RS256'],
                                  issuer=AUTHORITY, audience=client_id,
                                  options={'require': ['iss', 'aud', 'sub', 'exp', 'iat']})
            if nonce is not None and not secrets.compare_digest(identity.get('nonce', ''), nonce):
                raise AuthError('ID token 的 nonce 不匹配', code='invalid_nonce')
            return identity
        except (jwt.PyJWTError, KeyError, StopIteration, TypeError, ValueError) as exc:
            raise AuthError('OpenAI 身份签名或声明校验失败', code='invalid_identity') from exc

    @staticmethod
    def _tokens(data: dict, previous: Credentials) -> Credentials:
        if not data.get('access_token') or data.get('token_type', '').lower() != 'bearer':
            raise AuthError('令牌响应缺少 Bearer 凭证', code='invalid_tokens')
        expires_in = float(data.get('expires_in', 0))
        if expires_in <= 0:
            raise AuthError('令牌响应缺少有效过期时间', code='invalid_tokens')
        return replace(previous, access_token=data['access_token'],
                       refresh_token=data.get('refresh_token', previous.refresh_token),
                       id_token=data.get('id_token', previous.id_token),
                       scopes=data['scope'].split() if 'scope' in data else previous.scopes,
                       expires_at=time.time() + expires_in,
                       earliest_refresh_at=float(data.get('earliest_refresh_at', 0)))

    def finish(self, attempt: LoginAttempt, query: dict[str, str]) -> Credentials:
        client_id = attempt.callback_client(query)
        with self.store.locked():
            current = self.store.load()
            if current.registration.client_id != attempt.previous.registration.client_id:
                raise AuthError('该账号标签已被另一登录更新，请重试', code='login_conflict')
            # 即使一次性 code 已失效，也保留已分配的 client_id 供下次重新登录。
            registered = replace(current, registration=replace(current.registration, client_id=client_id))
            self.store.save(registered)
            data = self._exchange({'grant_type': 'authorization_code', 'client_id': client_id,
                                  'code': query['code'], 'code_verifier': attempt.verifier,
                                  'redirect_uri': attempt.redirect_uri})
            identity = self._identity(data.get('id_token', ''), client_id, attempt.nonce)
            if current.registration.subject and identity['sub'] != current.registration.subject:
                raise AuthError('登录身份与账号标签不匹配，请使用新的账号标签', code='identity_mismatch')
            credentials = self._tokens(data, Credentials(registration=Registration(
                client_id, identity['sub'], identity.get('email', ''))))
            self.store.save(credentials)
            return credentials

    def access_token(self) -> str:
        # 服务和命令行共用文件锁；刷新后重新读取，避免并发使用旧 refresh token。
        with self.store.locked():
            credentials = self.store.load()
            if not credentials.access_token or not credentials.plan_enabled:
                raise AuthError('请先为本项目登录并授权 ChatGPT 订阅调用', code='login_required')
            now = time.time()
            if credentials.expires_at > now + 60:
                return credentials.access_token
            if credentials.earliest_refresh_at > now:
                if credentials.expires_at > now:
                    return credentials.access_token
                raise AuthError('尚未到允许续期的时间，请稍后重试', code='refresh_not_ready')
            if not credentials.refresh_token:
                raise AuthError('登录已过期，请重新登录', code='login_required')
            try:
                data = self._exchange({'grant_type': 'refresh_token',
                    'client_id': credentials.registration.client_id, 'refresh_token': credentials.refresh_token})
            except AuthError as exc:
                if exc.code in UNUSABLE_REFRESH:
                    self.store.save(credentials.without_tokens())
                raise
            if data.get('id_token'):
                identity = self._identity(data['id_token'], credentials.registration.client_id)
                if identity['sub'] != credentials.registration.subject:
                    raise AuthError('续期返回的身份不匹配', code='identity_mismatch')
            renewed = self._tokens(data, credentials)
            self.store.save(renewed)
            if not renewed.plan_enabled:
                raise AuthError('ChatGPT 订阅调用权限未获授权', code='plan_disabled')
            return renewed.access_token

    async def async_access_token(self) -> str:
        return await asyncio.to_thread(self.access_token)

    def models(self) -> list[dict]:
        data = self._json(self.http.get(RESOURCE + '/models',
                                       headers={'Authorization': 'Bearer ' + self.access_token()}))
        return [{'id': item['slug'], 'name': item.get('display_name', item['slug'])}
                for item in data.get('models', []) if item.get('visibility') == 'list']

    def logout(self):
        with self.store.locked():
            credentials = self.store.load()
            if credentials.refresh_token:
                response = self.http.post(AUTHORITY + '/api/accounts/oauth/revoke', data={
                    'token': credentials.refresh_token, 'token_type_hint': 'refresh_token',
                    'client_id': credentials.registration.client_id})
                if response.is_error:
                    self._json(response)
            self.store.save(credentials.without_tokens())
