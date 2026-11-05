"""本机单进程 Mossail Server；只管理本脚本启动的进程。"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = ROOT / '.data/runtime'
STATE_FILE = RUNTIME_DIR / 'mossail-agent.json'
LOG_FILE = RUNTIME_DIR / 'mossail-agent.log'
MODULE = 'agent_service.main:app'
STORAGE_ENV_FILE = ROOT / '.data/agent-workspace/environment/.env.private'


def _read_env(path: Path) -> dict[str, str]:
    values = {}
    if not path.exists():
        return values
    for number, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:].lstrip()
        if '=' not in line:
            raise RuntimeError(f'{path.name} 第 {number} 行缺少等号')
        key, value = line.split('=', 1)
        try:
            parts = shlex.split(value, comments=True)
        except ValueError:
            raise RuntimeError(f'{path.name} 第 {number} 行引号不完整') from None
        values[key.strip()] = parts[0] if parts else ''
    return values


def configuration():
    """保留原模型/研究配置，另外装配本任务专属 PG/Redis 变量。"""
    env = os.environ.copy()
    env.update(_read_env(ROOT / '.env.local'))
    # 私有中间件文件只可提供 Mossail 配置，不能覆盖模型路由或认证。
    env.update({key: value for key, value in _read_env(STORAGE_ENV_FILE).items()
                if key.startswith('MOSSAIL_')})
    return env


@contextmanager
def process_lock():
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with (RUNTIME_DIR / 'mossail-agent.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _state():
    try:
        state = json.loads(STATE_FILE.read_text())
        return state if isinstance(state, dict) else None
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _owned(state) -> bool:
    if not state or state.get('root') != str(ROOT):
        return False
    pid = state.get('pid')
    if not isinstance(pid, int) or pid <= 1:
        return False
    # PID 可能被系统复用；命令和进程启动时间都匹配才允许管理。
    result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'lstart=', '-o', 'command='],
                            text=True, capture_output=True, check=False)
    return result.returncode == 0 and result.stdout.strip() == state.get('identity')


def _identity(pid: int) -> str:
    result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'lstart=', '-o', 'command='],
                            text=True, capture_output=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ''


def _clear_state(pid: int):
    state = _state()
    if state and state.get('pid') == pid:
        STATE_FILE.unlink(missing_ok=True)


def stop_managed() -> bool:
    state = _state()
    if not _owned(state):
        STATE_FILE.unlink(missing_ok=True)
        print('本脚本没有正在运行的 Mossail Server。', flush=True)
        return False
    pid = state['pid']
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _clear_state(pid)
        return True
    for _ in range(250):
        if not _owned(state):
            _clear_state(pid)
            print(f'Mossail Server 已停止（PID {pid}）。', flush=True)
            return True
        time.sleep(0.1)
    # 仍只处理通过身份核对的本任务进程；不按端口杀进程。
    if _owned(state):
        os.kill(pid, signal.SIGKILL)
    _clear_state(pid)
    print(f'Mossail Server 已终止（PID {pid}）。', flush=True)
    return True


def _check_port(host: str, port: int):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            raise RuntimeError(f'{host}:{port} 已被占用；请改用 --port，未停止任何其他进程') from None


def _wait_ready(child, url: str):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError(f'Server 启动失败；请查看 {LOG_FILE}')
        try:
            with urlopen(url + '/health', timeout=1) as response:
                health = json.load(response)
            if health.get('service') == 'mossail':
                return
        except (URLError, TimeoutError, OSError, ValueError):
            pass
        time.sleep(0.2)
    raise RuntimeError(f'30 秒内未收到 Mossail health；请查看 {LOG_FILE}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skip-sync', action='store_true', help='复用现有 Agent 虚拟环境')
    parser.add_argument('--port', type=int, help='HTTP、前端与 WebSocket 共用端口，默认 8010')
    parser.add_argument('--host', default='127.0.0.1')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--background', action='store_true', help='后台启动，日志写入 .data/runtime/')
    mode.add_argument('--stop', action='store_true', help='只停止本脚本记录且身份匹配的进程')
    mode.add_argument('--status', action='store_true', help='查看本脚本管理的 Server')
    args = parser.parse_args()
    if args.stop or args.status:
        with process_lock():
            if args.stop:
                stop_managed()
            else:
                state = _state()
                if _owned(state):
                    print(f'Mossail Server 运行中：{state["url"]}（PID {state["pid"]}）')
                else:
                    print('本脚本没有正在运行的 Mossail Server。')
        return
    env = configuration()
    port = args.port if args.port is not None else int(env.get('MOSSAIL_PORT', '8010'))
    if not 1 <= port <= 65535:
        parser.error('--port 必须在 1 到 65535 之间')
    if not args.skip_sync:
        subprocess.run(['uv', 'sync', '--project', 'apps/agent', '--frozen'], cwd=ROOT, check=True)
    python = ROOT / 'apps/agent/.venv/bin/python'
    if not python.exists():
        raise RuntimeError('Agent 虚拟环境不存在，请先去掉 --skip-sync 安装依赖')
    command = [str(python), '-m', 'uvicorn', MODULE, '--host', args.host, '--port', str(port),
               '--workers', '1', '--timeout-graceful-shutdown', '20']
    url = f'http://{args.host}:{port}'
    with process_lock():
        old = _state()
        if _owned(old):
            print(f'Mossail Server 已运行：{old["url"]}（PID {old["pid"]}）', flush=True)
            return
        _check_port(args.host, port)
        log = LOG_FILE.open('a') if args.background else None
        try:
            child = subprocess.Popen(command, cwd=ROOT, env=env, start_new_session=args.background,
                                     stdout=log, stderr=subprocess.STDOUT if log else None)
        finally:
            if log:
                log.close()
        try:
            identity = _identity(child.pid)
            if not identity:
                raise RuntimeError('无法记录 Server 进程身份，请在允许进程查询的终端启动')
            STATE_FILE.write_text(json.dumps({'pid': child.pid, 'root': str(ROOT), 'identity': identity,
                'url': url, 'module': MODULE, 'log': str(LOG_FILE) if args.background else None,
                'started_at': datetime.now(timezone.utc).isoformat()}, ensure_ascii=False, indent=2) + '\n')
        except BaseException:
            child.terminate()
            child.wait(timeout=25)
            raise
    if args.background:
        try:
            _wait_ready(child, url)
        except BaseException:
            with process_lock():
                stop_managed()
            raise
        print(f'Mossail 工作台：{url}（单 Server、单 Worker，PID {child.pid}）', flush=True)
        print(f'日志：{LOG_FILE}', flush=True)
        return
    print(f'Mossail 工作台：{url}（单 Server、单 Worker，PID {child.pid}）', flush=True)
    def interrupted(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    try:
        code = child.wait()
        if code:
            raise RuntimeError(f'Server 已退出（状态 {code}），请查看启动错误')
    except KeyboardInterrupt:
        pass
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=25)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        with process_lock():
            _clear_state(child.pid)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
