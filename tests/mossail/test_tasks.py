"""任务生命周期真实联调：PG/Redis 独立命名空间，Harness 使用本地替身。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import shlex
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from open_protocol import ChatCommand, Envelope, MessageItem, MessageUpdate, PatchOperation

from agent_service.models.task import TERMINAL
from agent_service.repositories.postgres import PgStore
from agent_service.services.records import RecordService
from agent_service.models.message import apply_update
from agent_service.repositories.redis import RedisBus
from agent_service.errors import InvalidCommand
from agent_service.services.tasks import RunHandle, TaskService


class LocalRuntime:
    """按事件闸门控制开始/结束；不构造模型客户端或请求外部网络。"""

    def __init__(self, mode='normal'):
        self.mode = mode
        self.started, self.release, self.closed = (asyncio.Event() for _ in range(3))
        self.inputs = []

    async def stream(self, request):
        self.inputs.append(request)
        try:
            yield update(request, 'user', value=MessageItem(index=0, sender='user',
                type='markdown', status='success', meta_data={'content': request.user_prompt}))
            yield update(request, 'answer', value=MessageItem(index=1, type='markdown',
                status='loading', meta_data={'content': ''}))
            self.started.set()
            if self.mode == 'slow':
                await self.release.wait()
            if self.mode == 'eof':
                return
            yield update(request, 'answer', op='patch', task_status='complete', patches=[
                PatchOperation(op='replace', path='/meta_data/content', value='42'),
                PatchOperation(op='replace', path='/status', value='success')])
        finally:
            self.closed.set()


def update(request, msg_id, *, op='add', task_status='running', **kwargs):
    task_id = getattr(request, 'task_id', None) or request.id
    return MessageUpdate(event_v2=Envelope(thread_id=request.thread_id, task_id=task_id,
        msg_id=msg_id, op=op, task_status=task_status, **kwargs))


def environment():
    values = {}
    private = Path(__file__).resolve().parents[2] / '.data/agent-workspace/environment/.env.private'
    if private.exists():
        for line in private.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            name, raw = line.split('=', 1)
            parts = shlex.split(raw)
            values[name] = parts[0] if parts else ''
    values.update({key: value for key, value in os.environ.items() if key.startswith('MOSSAIL_')})
    if not values.get('MOSSAIL_DATABASE_URL') or not values.get('MOSSAIL_REDIS_URL'):
        pytest.skip('未配置本机 Mossail PG/Redis，跳过真实生命周期联调')
    return values


@dataclass
class Components:
    store: PgStore
    bus: RedisBus
    records: RecordService
    services: list

    def service(self, runtime, *, timeout=2):
        service = TaskService(self.store, self.records, {'assistant': lambda: runtime},
                              model='local-test-model', timeout=timeout)
        self.services.append(service)
        return service


@pytest_asyncio.fixture
async def components():
    env = environment()
    schema = f'mossail_lifecycle_{uuid4().hex}'
    namespace = f'mossail:lifecycle:{uuid4().hex}'
    store = PgStore(env['MOSSAIL_DATABASE_URL'], schema=schema)
    bus = RedisBus(env['MOSSAIL_REDIS_URL'], namespace=namespace)
    await store.open()
    services = []
    try:
        await bus.open()
        yield Components(store, bus, RecordService(store, bus, mirror_interval=0), services)
    finally:
        try:
            for service in services:
                await service.close()
        finally:
            try:
                if bus.client is not None:
                    # 只清理本次 UUID namespace，不影响开发任务和订阅。
                    keys = [key async for key in bus.client.scan_iter(match=f'{namespace}:*')]
                    if keys:
                        await bus.client.delete(*keys)
                await bus.close()
            finally:
                try:
                    await store.close()
                finally:
                    cleanup = await asyncpg.connect(env['MOSSAIL_DATABASE_URL'], timeout=8, command_timeout=8)
                    try:
                        await cleanup.execute(f'DROP SCHEMA "{schema}" CASCADE')
                    finally:
                        await cleanup.close()


async def wait_terminal(service, task_id):
    async with asyncio.timeout(4):
        while True:
            saved = await service.store.get(task_id)
            if saved.status in TERMINAL:
                handle = service.runs.get(task_id)
                if handle and handle.work:
                    await asyncio.shield(handle.work)
                return saved
            await asyncio.sleep(.01)


@pytest.mark.asyncio
async def test_admission_deduplicates_and_completed_turn_feeds_next_context(components):
    runtime = LocalRuntime()
    service = components.service(runtime)
    thread = (await service.create_thread())['thread_id']
    command = ChatCommand(question='第一轮', client_request_id='first-request')
    first, duplicate = await asyncio.gather(service.submit(thread, command), service.submit(thread, command))
    assert first['task_id'] == duplicate['task_id']
    result = await wait_terminal(service, first['task_id'])
    assert result.status == 'complete' and result.snapshots['answer']['meta_data']['content'] == '42'
    assert result.snapshots['user']['sender'] == 'user'
    assert result.seq == 3 and await components.bus.read(thread) is None
    assert len(runtime.inputs) == 1
    second = await service.submit(thread, ChatCommand(question='第二轮', client_request_id='second-request'))
    second_result = await wait_terminal(service, second['task_id'])
    assert second_result.generation == 2
    assert [(m.role, m.content) for m in runtime.inputs[1].history_messages] == [
        ('user', '第一轮'), ('assistant', '42')]
    assert (await service.submit(thread, command))['task_id'] == first['task_id']
    assert len(await components.store.list_tasks(thread)) == 2


@pytest.mark.asyncio
async def test_active_thread_rejects_second_task_and_slow_cancel_is_persisted(components):
    runtime = LocalRuntime('slow')
    service = components.service(runtime)
    thread = (await service.create_thread())['thread_id']
    accepted = await service.submit(thread, ChatCommand(question='等待停止'))
    await asyncio.wait_for(runtime.started.wait(), 2)
    with pytest.raises(ValueError, match='已有运行'):
        await service.submit(thread, ChatCommand(question='并发第二轮'))
    cancelled = await service.cancel(accepted['task_id'])
    saved = await components.store.get(accepted['task_id'])
    assert cancelled['cancelled'] and saved.status == 'abandon'
    assert '停止' in saved.error and runtime.closed.is_set()
    assert accepted['task_id'] not in service.runs
    assert await components.bus.read(thread) is None
    assert not (await service.cancel(saved.id))['cancelled']


@pytest.mark.asyncio
async def test_cancel_before_execute_coroutine_starts_still_saves_terminal(components):
    runtime = LocalRuntime('slow')
    service = components.service(runtime)
    thread = (await service.create_thread())['thread_id']
    task = (await components.store.admit(thread, ChatCommand(question='尚未开始'))).task
    handle = RunHandle(task)
    service.runs[task.id] = handle
    handle.work = asyncio.create_task(service.execute(handle))
    # 同一 event-loop tick 中先取消，确保 execute 尚未触及运行态/镜像。
    handle.work.cancel()
    result = await service.cancel(task.id)
    saved = await components.store.get(task.id)
    assert result['cancelled'] and saved.status == 'abandon'
    assert not runtime.started.is_set() and not runtime.inputs
    assert task.id not in service.runs and saved.finished_at


@pytest.mark.asyncio
async def test_eof_without_terminal_fails_and_closes_runtime(components):
    runtime = LocalRuntime('eof')
    service = components.service(runtime)
    thread = (await service.create_thread())['thread_id']
    accepted = await service.submit(thread, ChatCommand(question='截断的流'))
    saved = await wait_terminal(service, accepted['task_id'])
    assert saved.status == 'failed' and 'RuntimeError' in saved.error
    assert saved.snapshots['answer']['status'] == 'loading'
    assert saved.snapshots[f'{saved.id}_outcome']['status'] == 'error'
    assert runtime.closed.is_set()


@pytest.mark.asyncio
async def test_timeout_fails_without_leaking_runtime_or_active_task(components):
    runtime = LocalRuntime('slow')
    service = components.service(runtime, timeout=.1)
    thread = (await service.create_thread())['thread_id']
    accepted = await service.submit(thread, ChatCommand(question='超时任务'))
    saved = await wait_terminal(service, accepted['task_id'])
    assert saved.status == 'failed' and saved.error == '任务超时'
    assert runtime.closed.is_set() and saved.id not in service.runs
    assert await components.bus.read(thread) is None


@pytest.mark.asyncio
async def test_subscription_disconnect_does_not_cancel_independent_execution(components):
    runtime = LocalRuntime('slow')
    service = components.service(runtime)
    thread = (await service.create_thread())['thread_id']
    subscription = await components.bus.subscribe(thread)
    accepted = await service.submit(thread, ChatCommand(question='页面断开后继续'))
    await asyncio.wait_for(runtime.started.wait(), 2)
    frame = await asyncio.wait_for(subscription.queue.get(), 2)
    assert frame['stream']['task_id'] == accepted['task_id']
    await components.bus.unsubscribe(subscription)
    assert subscription.listener.done() and subscription not in components.bus.subscriptions
    assert not service.runs[accepted['task_id']].work.done()
    runtime.release.set()
    saved = await wait_terminal(service, accepted['task_id'])
    assert saved.status == 'complete'
    snapshot = await RecordService(components.store, components.bus).snapshot(thread)
    answer = next(item for item in snapshot['messages'] if item['msg_id'] == 'answer')
    assert answer['value']['meta_data']['content'] == '42'
    assert snapshot['heads'][saved.id] == saved.seq


@pytest.mark.asyncio
async def test_restart_recovers_newer_mirror_and_marks_interrupted_without_rerun(components):
    thread = await components.store.create_thread()
    task = (await components.store.admit(thread, ChatCommand(question='中断前问题'))).task
    task = await components.store.set_running(task.id)
    task = apply_update(task, update(task, 'answer', value=MessageItem(index=1, type='markdown',
        status='loading', meta_data={'content': '最近运行片段'})))
    assert await components.bus.activate(task) and await components.bus.mirror(task)
    assert (await components.store.get(task.id)).seq == 0
    # 新对象代表新进程；不保留旧 RecordService 的内存或 Runtime。
    recovered = RecordService(components.store, components.bus)
    assert recovered.epoch != components.records.epoch
    await recovered.recover()
    saved = await components.store.get(task.id)
    assert saved.status == 'failed' and 'INTERRUPTED' in saved.error
    assert saved.snapshots['answer']['meta_data']['content'] == '最近运行片段'
    assert saved.seq == 2 and await components.bus.read(thread) is None
    sync = await recovered.snapshot(thread)
    assert sync['server_epoch'] == recovered.epoch
    assert sync['heads'][task.id] == saved.seq
    await recovered.recover()
    assert (await components.store.get(task.id)).to_dict() == saved.to_dict()
    replacement = await components.store.admit(thread, ChatCommand(question='重提'))
    assert replacement.created and replacement.task.generation == 2


@pytest.mark.asyncio
async def test_invalid_scenario_or_model_is_rejected_before_task_admission(components):
    service = components.service(LocalRuntime())
    thread = (await service.create_thread())['thread_id']
    with pytest.raises(InvalidCommand, match='场景未配置'):
        await service.submit(thread, ChatCommand(question='未知场景', agent='missing'))
    with pytest.raises(InvalidCommand, match='只允许'):
        await service.submit(thread, ChatCommand(question='更换模型', model={'id': 'another'}))
    assert not await components.store.list_tasks(thread)
