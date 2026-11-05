"""为本项目登录 ChatGPT、查看模型及撤销授权；令牌只保存在本地。"""
from __future__ import annotations

import argparse
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import time
from urllib.parse import parse_qs, urlsplit
import webbrowser

import httpx

from agent_service.agents.chatgpt_auth import AuthError, ChatGPTAuth, CredentialStore

ROOT = Path(__file__).resolve().parents[1]


def login(auth: ChatGPTAuth, *, open_browser: bool, timeout: int):
    result = {'received': False, 'error': None}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # 默认访问日志会把一次性 code 写入终端。

        def page(self, status: int, content: str):
            body = ('<!doctype html><html lang="zh"><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width, initial-scale=1">'
                    '<title>Open Repo · ChatGPT 登录</title>'
                    '<body style="font:18px/1.7 system-ui;max-width:620px;margin:12vh auto;padding:24px">'
                    '<h1>Open Repo</h1>' + content + '</body></html>').encode()
            self.send_response(status)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Frame-Options', 'DENY')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.headers.get('Host') != f'127.0.0.1:{server.server_port}':
                self.page(400, '<p>请求地址不匹配。</p>')
                return
            url = urlsplit(self.path)
            if url.path == '/':
                self.page(200, '<p>为当前本地项目连接 ChatGPT。</p>'
                    '<p>符合条件的 AI 请求将使用你的 ChatGPT 订阅额度，'
                    '你可以在 ChatGPT 设置中管理用量和访问权限。</p>'
                    '<p><a href="/start">Continue with ChatGPT</a></p>'
                    '<p><a href="https://chatgpt.com/settings/usage">管理 ChatGPT 用量</a></p>')
                return
            if url.path == '/start':
                self.send_response(302)
                self.send_header('Location', attempt.authorize_url())
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if url.path != '/auth/callback' or result['received']:
                self.page(404, '<p>页面不存在。</p>')
                return
            values = parse_qs(url.query, keep_blank_values=True)
            if any(len(value) != 1 for value in values.values()):
                self.page(400, '<p>授权回调参数重复。</p>')
                return
            query = {key: value[0] for key, value in values.items()}
            try:
                attempt.callback_client(query)
            except AuthError as exc:
                if exc.code != 'invalid_state':
                    result.update(received=True, error=exc)
                self.page(400, '<p>' + escape(str(exc)) + '</p>')
                return
            try:
                credentials = auth.finish(attempt, query)
                result['received'] = True
                message = ('登录成功，可以关闭此页面。' if credentials.plan_enabled
                           else '已登录，但订阅调用权限未授权；当前不会发起模型请求。')
                self.page(200, '<p>' + message + '</p>')
            except (AuthError, httpx.HTTPError) as exc:
                result.update(received=True, error=exc)
                self.page(400, '<p>授权处理未完成，请查看本地命令行诊断。</p>')

    with HTTPServer(('127.0.0.1', 0), Callback) as server:
        server.timeout = 0.5
        attempt = auth.begin(f'http://127.0.0.1:{server.server_port}/auth/callback')
        page_url = f'http://127.0.0.1:{server.server_port}/'
        print('请在浏览器完成本项目的 ChatGPT 授权：' + page_url, flush=True)
        if open_browser:
            webbrowser.open(page_url)
        deadline = time.monotonic() + timeout
        while not result['received'] and time.monotonic() < deadline:
            server.handle_request()
    if result['error']:
        raise result['error']
    if not result['received']:
        raise AuthError('等待浏览器授权超时；可以重新运行登录命令', code='login_timeout')


def status(store: CredentialStore) -> dict:
    credentials = store.load()
    return {'account': store.account, 'registered': bool(credentials.registration.client_id),
            'signed_in': bool(credentials.access_token), 'plan_enabled': credentials.plan_enabled,
            'needs_refresh': credentials.expires_at <= time.time() + 60,
            'credentials_file': str(store.path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['login', 'status', 'models', 'logout'])
    parser.add_argument('--account', default=os.getenv('CHATGPT_ACCOUNT', 'default'),
                        help='每个标签对应一个独立账号/工作区注册，切换账号请使用新标签')
    parser.add_argument('--auth-dir', type=Path,
                        default=Path(os.getenv('CHATGPT_AUTH_DIR', str(ROOT / '.data/chatgpt'))))
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--timeout', type=int, default=600)
    args = parser.parse_args()
    store = CredentialStore(args.auth_dir, args.account)
    try:
        with httpx.Client(timeout=25, follow_redirects=False) as http:
            auth = ChatGPTAuth(store, http)
            if args.command == 'login':
                login(auth, open_browser=not args.no_browser, timeout=args.timeout)
            elif args.command == 'models':
                print(json.dumps(auth.models(), ensure_ascii=False, indent=2))
                return 0
            elif args.command == 'logout':
                auth.logout()
        print(json.dumps(status(store), ensure_ascii=False, indent=2))
        return 0
    except AuthError as exc:
        print(json.dumps({'error': str(exc), 'code': exc.code,
                          'http_status': exc.status, 'request_id': exc.request_id}, ensure_ascii=False))
        return 1
    except httpx.HTTPError as exc:
        print(json.dumps({'error': 'OpenAI 网络请求未完成', 'type': type(exc).__name__}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
