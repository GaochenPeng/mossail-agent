"""默认只检查 health；同时指定 --live --crash 才执行真实任务与本任务进程崩溃恢复验收。"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import httpx

import dev
from verify_mossail import Acceptance, Evidence
from agent_service.repositories.postgres import _record
from agent_service.repositories.redis import RedisBus

ROOT = Path(__file__).resolve().parents[1]
QUESTION = ('请检索 Python 官方文档中 asyncio.Task.cancel() 与 asyncio.timeout() 的行为，'
            '分别给出相关说明、链接及取消与超时的边界；需要检索并阅读官方文档后回答。')


def model_responses(raw: bytes) -> int:
    lines = raw.decode('utf-8', errors='replace').splitlines()
    return sum('HTTP Request: POST ' in line and ('/responses' in line or '/chat/completions' in line)
               for line in lines)


def owned_process(base, expected_pid):
    state = dev._state()
    assert dev._owned(state), '状态文件与进程启动时间/完整命令不匹配，拒绝停止进程'
    assert state['url'] == base and state['module'] == dev.MODULE
    assert state['root'] == str(ROOT)
    assert state['pid'] == expected_pid, 'PID 已变化，拒绝停止进程'
    assert 'agent_service.main:app' in state['identity'] and '--workers 1' in state['identity']
    assert '--port 8010' in state['identity'] and base == 'http://127.0.0.1:8010'
    return state


async def pg_get(db, schema, task_id):
    row = await db.fetchrow(f'SELECT * FROM "{schema}".tasks WHERE id=$1', task_id)
    assert row is not None, 'PG 中未找到验收任务'
    return _record(row)


async def restart(evidence):
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(ROOT / 'scripts/dev.py'), '--skip-sync', '--port', '8010', '--background',
        cwd=ROOT, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await asyncio.wait_for(process.communicate(), timeout=45)
    evidence.write('restart.json', {'returncode': process.returncode, 'output': output.decode(errors='replace')})
    assert process.returncode == 0, '本任务 Server 重新启动失败，请查看 restart.json'
    return dev._state()


async def run_crash(args, client, initial_health):
    directory = args.output / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:8])
    evidence = Evidence(directory)
    summary = {'started_at': datetime.now(timezone.utc).isoformat(), 'health_before': initial_health,
               'passed': False, 'killed': False, 'restarted': False, 'question': QUESTION}
    db = bus = None
    try:
        assert initial_health['service'] == 'mossail' and initial_health['ready']
        expected_runs = 1 if args.task_id else 0
        assert initial_health['active_runs'] == expected_runs and initial_health['pending_saves'] == 0, '运行任务数或待保存状态不符合本次验收前置条件'
        state = owned_process(args.base_url, args.expect_pid)
        summary['process_before'] = state
        summary['model_counter_source'] = '本任务httpx已收到响应的POST /responses或/chat/completions日志行；不等同远端计费计数'
        summary['model_response_log_count_at_start'] = model_responses(dev.LOG_FILE.read_bytes())
        env = dev.configuration()
        schema = env.get('MOSSAIL_DATABASE_SCHEMA', 'mossail')
        assert re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,62}', schema)
        db = await asyncpg.connect(env['MOSSAIL_DATABASE_URL'], timeout=8, command_timeout=8)
        bus = RedisBus(env['MOSSAIL_REDIS_URL'], env.get('MOSSAIL_REDIS_NAMESPACE', 'mossail:dev'))
        await bus.open()
        active = await db.fetch(f"SELECT id FROM \"{schema}\".tasks WHERE status IN ('submitted','running')")
        expected_active = [args.task_id] if args.task_id else []
        assert [row['id'] for row in active] == expected_active, 'PG 中存在本次验收之外的未完成任务'
        probe = Acceptance(client, evidence, args.base_url)
        if args.task_id:
            observed = await pg_get(db, schema, args.task_id)
            assert observed.status == 'running' and observed.command.agent == 'deep_search', '指定任务不是正在运行的DeepSearch'
            task_id, thread = observed.id, observed.thread_id
            request_id = observed.command.client_request_id
            summary['question'] = observed.command.question
            summary['task_source'] = 'existing_browser_task_no_new_submission'
        else:
            thread = (await probe.request('POST', '/threads', expected=201))['thread_id']
            request_id = 'acceptance-crash-' + uuid4().hex
            accepted = await probe.request('POST', f'/threads/{thread}/tasks', {
                'question': QUESTION, 'agent': 'deep_search', 'client_request_id': request_id}, expected=202)
            task_id = accepted['task_id']
            summary['task_source'] = 'script_created_one_task'
        summary.update(thread_id=thread, task_id=task_id, client_request_id=request_id)
        before_ids = [row['id'] for row in await db.fetch(f'SELECT id FROM "{schema}".tasks WHERE thread_id=$1 ORDER BY generation', thread)]
        summary['before_thread_task_ids'] = before_ids
        deadline = time.monotonic() + 60
        tool_seen_at = None
        while True:
            mirror = await bus.read(thread)
            pg_task = await pg_get(db, schema, task_id)
            assert pg_task.status not in {'complete', 'failed', 'abandon'}, '任务已终态，不再执行崩溃操作'
            if mirror and mirror.id == task_id and mirror.status == 'running' and pg_task.status == 'running':
                tools = [value for value in mirror.snapshots.values() if value.get('type') == 'tool_call']
                if tools:
                    tool_seen_at = tool_seen_at or time.monotonic()
                    # 优先取得非空工具输出；若普通 patch 尚未到镜像节流点，保留已有调用参数即可。
                    if any(tool.get('meta_data', {}).get('output') for tool in tools) or time.monotonic() - tool_seen_at >= .5:
                        break
            assert time.monotonic() < deadline, '60秒内未得到包含真实工具调用的运行镜像，不执行崩溃操作'
            await asyncio.sleep(.03)
        evidence.write('pg-before.json', pg_task.to_dict())
        evidence.write('redis-before.json', mirror.to_dict())
        summary['before'] = {'pg_seq': pg_task.seq, 'redis_seq': mirror.seq,
                             'pg_snapshots': list(pg_task.snapshots), 'redis_snapshots': list(mirror.snapshots)}
        # 获取进程锁后再次核对 PID/启动时间/命令；绝不按端口或宽泛进程名 kill。
        with dev.process_lock():
            state = owned_process(args.base_url, args.expect_pid)
            active_ids = await db.fetch(f"SELECT id FROM \"{schema}\".tasks WHERE status IN ('submitted','running')")
            assert [row['id'] for row in active_ids] == [task_id], '出现其他活动任务，拒绝停止 Server'
            pg_task = await pg_get(db, schema, task_id)
            assert pg_task.status == 'running', 'PG 任务已不处于 running，拒绝停止 Server'
            mirror = await bus.read(thread)
            assert mirror and mirror.id == task_id and mirror.status == 'running'
            assert any(value.get('type') == 'tool_call' for value in mirror.snapshots.values()), '运行镜像缺少真实工具调用，拒绝停止 Server'
            evidence.write('pg-immediately-before-kill.json', pg_task.to_dict())
            evidence.write('redis-immediately-before-kill.json', mirror.to_dict())
            summary['kill_mirror_seq'] = mirror.seq
            os.kill(state['pid'], signal.SIGKILL)
            summary['killed'] = True
        log_offset = dev.LOG_FILE.stat().st_size
        summary['log_offset_after_kill'] = log_offset
        summary['model_response_log_count_at_kill'] = model_responses(dev.LOG_FILE.read_bytes())
        for _ in range(100):
            if not dev._owned(state):
                break
            await asyncio.sleep(.03)
        assert not dev._owned(state), '原 Server 进程尚未消失'
        restarted = await restart(evidence)
        summary['restarted'] = True
        summary['process_after'] = restarted
        assert restarted['pid'] != state['pid'] and dev._owned(restarted)
        restored = await pg_get(db, schema, task_id)
        evidence.write('pg-after.json', restored.to_dict())
        assert restored.status == 'failed' and 'INTERRUPTED' in (restored.error or '')
        assert restored.finished_at and restored.seq > mirror.seq
        assert set(mirror.snapshots).issubset(restored.snapshots), '恢复后丢失已有消息身份'
        for msg_id, previous in mirror.snapshots.items():
            current = restored.snapshots[msg_id]
            assert (current['type'], current['sender'], current['index']) == (previous['type'], previous['sender'], previous['index'])
            before_content = previous.get('meta_data', {}).get('content')
            after_content = current.get('meta_data', {}).get('content')
            if isinstance(before_content, str):
                assert isinstance(after_content, str) and after_content.startswith(before_content)
        assert await bus.read(thread) is None, '终态恢复后运行镜像未删除'
        result = await probe.request('GET', f'/tasks/{task_id}')
        assert result['status'] == restored.status and result['seq'] == restored.seq
        assert result['snapshots'] == restored.snapshots
        async with probe.connect() as socket:
            sync = await probe.sync(socket, thread)
            assert [item['task_id'] for item in sync['tasks']] == before_ids
            assert next(item for item in sync['tasks'] if item['task_id'] == task_id)['status'] == 'failed'
            assert sync['heads'][task_id] == restored.seq
            assert {item['msg_id']: item['value'] for item in sync['messages'] if item['task_id'] == task_id} == restored.snapshots
            evidence.write('restored-sync.json', sync)
        # 只读复查稳定性；不重新提交 task，不调用重试执行入口。
        await asyncio.sleep(2)
        again = await probe.request('GET', f'/tasks/{task_id}')
        assert again == result
        history = await probe.request('GET', f'/threads/{thread}/history')
        assert [item['task_id'] for item in history['tasks']] == before_ids
        summary['model_response_log_lines_after_restart'] = model_responses(dev.LOG_FILE.read_bytes()[log_offset:])
        assert summary['model_response_log_lines_after_restart'] == 0, '重启后观察到新模型响应请求日志'
        summary['after'] = {'status': restored.status, 'error': restored.error, 'seq': restored.seq,
                            'snapshot_ids': list(restored.snapshots), 'thread_task_count': len(before_ids)}
        summary['health_after'] = await probe.request('GET', '/health')
        assert summary['health_after']['ready'] and summary['health_after']['active_runs'] == 0
        assert summary['health_after']['agent']['execution']['source_sha256'] == initial_health['agent']['execution']['source_sha256']
        summary['passed'] = True
    except Exception as exc:
        # 连接异常可能含连接细节；仅保存类型及我们自己的断言说明。
        summary['failure'] = {'type': type(exc).__name__, 'message': str(exc) if isinstance(exc, AssertionError) else '验收失败，未输出连接细节'}
    finally:
        if summary['killed'] and not summary['restarted']:
            try:
                summary['process_after'] = await restart(evidence)
                summary['restarted'] = True
            except Exception as exc:
                summary['restore_failure_type'] = type(exc).__name__
        if bus is not None:
            await bus.close()
        if db is not None:
            await db.close()
        summary['finished_at'] = datetime.now(timezone.utc).isoformat()
        evidence.write('summary.json', summary)
        print(json.dumps({'artifact_directory': str(directory.resolve()), 'passed': summary['passed'],
                          'task_id': summary.get('task_id'), 'killed': summary['killed'],
                          'restarted': summary['restarted'], 'failure': summary.get('failure')}, ensure_ascii=False, indent=2))
    return summary['passed']


async def main(args):
    async with httpx.AsyncClient(base_url=args.base_url, timeout=20, trust_env=False) as client:
        health = (await client.get('/health')).json()
        if not args.live:
            print(json.dumps(health, ensure_ascii=False, indent=2))
            return True
        return await run_crash(args, client, health)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8010')
    parser.add_argument('--live', action='store_true', help='明确允许一条真实DeepSearch任务')
    parser.add_argument('--crash', action='store_true', help='明确允许终止身份核实后的本任务8010 Server并重新启动')
    parser.add_argument('--expect-pid', type=int)
    parser.add_argument('--task-id', help='只观察并中断这个已由浏览器启动的DeepSearch任务，不创建新会话或任务')
    parser.add_argument('--output', type=Path, default=ROOT / '.data/agent-workspace/implementation/acceptance/failure')
    arguments = parser.parse_args()
    arguments.base_url = arguments.base_url.rstrip('/')
    if arguments.live != arguments.crash:
        parser.error('真实异常验收需要同时指定 --live --crash；默认只读 health')
    if arguments.live and not arguments.expect_pid:
        parser.error('真实异常验收必须指定 --expect-pid，防止终止已变化的进程')
    if arguments.task_id and not arguments.live:
        parser.error('--task-id 只能配合 --live --crash 使用；默认只读health')
    raise SystemExit(0 if asyncio.run(main(arguments)) else 1)
