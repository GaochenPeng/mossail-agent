"""真实本机 PG/Redis 检查；只创建和清理本测试的随机 schema/namespace。"""
import asyncio
import copy
import json
import os
import shlex
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from open_protocol import ChatCommand

from agent_service.errors import StorageError
from agent_service.models.task import TaskRecord
from agent_service.repositories.postgres import PgStore
from agent_service.repositories.redis import RedisBus, QUEUE_LIMIT


def _environment():
    values = {}
    private = Path(__file__).resolve().parents[2] / '.data/agent-workspace/environment/.env.private'
    if private.exists():
        for line in private.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            name, value = line.split('=', 1)
            values[name] = shlex.split(value)[0]
    values.update({name: value for name, value in os.environ.items() if name.startswith('MOSSAIL_')})
    return values


@pytest.fixture
def environment():
    values = _environment()
    if not values.get('MOSSAIL_DATABASE_URL') or not values.get('MOSSAIL_REDIS_URL'):
        pytest.skip('未配置本机 Mossail PG/Redis，跳过真实中间件检查')
    return values


@pytest_asyncio.fixture
async def pg(environment):
    schema = f'mossail_test_{uuid4().hex}'
    store = PgStore(environment['MOSSAIL_DATABASE_URL'], schema=schema)
    await store.open()
    try:
        yield store
    finally:
        try:
            await store.close()
        finally:
            # 关闭失败也要清理由当前 fixture 创建的随机测试 schema。
            cleanup = await asyncpg.connect(environment['MOSSAIL_DATABASE_URL'], timeout=8, command_timeout=8)
            try:
                await cleanup.execute(f'DROP SCHEMA "{schema}" CASCADE')
            finally:
                await cleanup.close()


@pytest_asyncio.fixture
async def bus(environment):
    namespace = f'mossail:test:{uuid4().hex}'
    bus = RedisBus(environment['MOSSAIL_REDIS_URL'], namespace=namespace)
    await bus.open()
    try:
        yield bus
    finally:
        # 不使用 FLUSH，也不扫描或清理开发 namespace。
        keys = [key async for key in bus.client.scan_iter(match=f'{namespace}:*')]
        if keys:
            await bus.client.delete(*keys)
        await bus.close()


def _task(thread='thread', generation=1):
    return TaskRecord(id=str(uuid4()), thread_id=thread, generation=generation,
                      command=ChatCommand(question='问题'))


def _answer(content):
    return {'index': 1, 'sender': 'assistant', 'type': 'markdown', 'status': 'success',
            'meta_data': {'content': content}}


@pytest.mark.asyncio
async def test_pg_concurrent_admission_deduplicates_and_locks_thread(pg):
    thread = await pg.create_thread()
    command = ChatCommand(question='并发请求', client_request_id='same-request')
    admissions = await asyncio.gather(*(pg.admit(thread, command) for _ in range(8)))
    assert len({item.task.id for item in admissions}) == 1
    assert sum(item.created for item in admissions) == 1
    first = admissions[0].task
    assert first.generation == 1
    with pytest.raises(ValueError, match='已有运行'):
        await pg.admit(thread, ChatCommand(question='另一个请求', client_request_id='another'))
    first.status = 'complete'
    await pg.save(first)
    next_task = await pg.admit(thread, ChatCommand(question='下一轮', client_request_id='next'))
    assert next_task.created and next_task.task.generation == 2
    duplicate = await pg.admit(thread, command)
    assert not duplicate.created and duplicate.task.id == first.id
    with pytest.raises(KeyError):
        await pg.admit('missing', command)


@pytest.mark.asyncio
async def test_pg_full_projection_seq_fence_and_terminal_immutability(pg):
    thread = await pg.create_thread()
    task = (await pg.admit(thread, ChatCommand(question='保存完整结果'))).task
    task = await pg.set_running(task.id)
    task.seq = 3
    task.snapshots = {'answer': _answer('完整中文答案')}
    saved = await pg.save(task)
    assert saved.snapshots == task.snapshots and saved.status == 'running'
    async with pg.pool.acquire() as db:
        raw = await db.fetchval(f'SELECT result_json FROM "{pg.schema}".tasks WHERE id=$1', task.id)
    projection = json.loads(raw)
    assert projection == {'protocol_version': 2, 'events_v2': [
        {'op': 'add', 'thread_id': thread, 'task_id': task.id, 'msg_id': 'answer',
         'task_status': 'running', 'value': _answer('完整中文答案')}]}
    stale = copy.deepcopy(task)
    stale.seq, stale.status, stale.snapshots = 2, 'failed', {'answer': _answer('旧结果')}
    assert (await pg.save(stale)).snapshots == task.snapshots
    task.status = 'complete'
    committed = await pg.save(task)
    assert committed.finished_at and committed.status == 'complete'
    replacement = copy.deepcopy(task)
    replacement.status, replacement.seq = 'abandon', 4
    replacement.snapshots = {'answer': _answer('迟到写入')}
    assert (await pg.save(replacement)).to_dict() == committed.to_dict()
    assert (await pg.set_running(task.id)).to_dict() == committed.to_dict()
    assert not await pg.unfinished()
    history = await pg.history(thread, before_task='next-task')
    assert [(message.role, message.content) for message in history] == [('user', '保存完整结果'), ('assistant', '完整中文答案')]


@pytest.mark.asyncio
async def test_pg_save_failure_does_not_commit_partial_record(pg):
    thread = await pg.create_thread()
    task = (await pg.admit(thread, ChatCommand(question='事务回滚'))).task
    task.seq = 5
    task.snapshots = {'broken': {'unserializable': object()}}
    with pytest.raises(StorageError, match='PG 读写失败'):
        await pg.save(task)
    saved = await pg.get(task.id)
    assert saved.seq == 0 and saved.snapshots == {} and saved.status == 'submitted'


@pytest.mark.asyncio
async def test_pg_single_server_owner_is_released_on_close(pg, environment):
    other = PgStore(environment['MOSSAIL_DATABASE_URL'], schema=pg.schema)
    await other.open()
    try:
        await pg.claim_owner()
        await pg.claim_owner()
        with pytest.raises(StorageError, match='已有运行中的 Agent Server'):
            await other.claim_owner()
        await pg.close()
        await other.claim_owner()
    finally:
        await other.close()


@pytest.mark.asyncio
async def test_pg_lost_owner_connection_rejects_further_access(pg):
    await pg.claim_owner()
    pg.owner.terminate()
    with pytest.raises(StorageError, match='所有权连接已断开'):
        await pg.create_thread()
    with pytest.raises(StorageError, match='所有权连接已断开'):
        await pg.claim_owner()


@pytest.mark.asyncio
async def test_pg_history_has_only_recent_six_completed_pairs(pg):
    thread = await pg.create_thread()
    for index in range(9):
        task = (await pg.admit(thread, ChatCommand(question=f'问题{index}'))).task
        task.status = 'failed' if index == 7 else 'complete'
        task.snapshots = {'answer': _answer(f'答案{index}')}
        await pg.save(task)
    current = (await pg.admit(thread, ChatCommand(question='当前问题'))).task
    history = await pg.history(thread, before_task=current.id)
    assert len(history) == 12
    assert [message.content for message in history] == [
        text for index in [2, 3, 4, 5, 6, 8] for text in [f'问题{index}', f'答案{index}']]


@pytest.mark.asyncio
async def test_redis_owner_generation_seq_ttl_and_conditional_delete(bus):
    old = _task()
    assert not await bus.mirror(old)
    assert await bus.activate(old)
    old.seq, old.snapshots = 3, {'answer': _answer('运行快照')}
    assert await bus.mirror(old)
    assert (await bus.read(old.thread_id)).to_dict() == old.to_dict()
    ttl = await bus.client.ttl(bus._key('live_ev2', old.thread_id))
    assert 1790 < ttl <= 1800
    stale = copy.deepcopy(old)
    stale.seq = 2
    assert not await bus.mirror(stale)
    new = _task(generation=2)
    assert await bus.activate(new)
    assert await bus.read(new.thread_id) is None
    assert not await bus.mirror(old)
    assert not await bus.activate(old)
    assert not await bus.activate(_task(generation=2))
    assert await bus.mirror(new)
    assert not await bus.discard(old)
    assert (await bus.read(new.thread_id)).id == new.id
    assert await bus.discard(new)
    assert await bus.read(new.thread_id) is None
    assert await bus.client.exists(bus._key('owner', new.thread_id))


@pytest.mark.asyncio
async def test_redis_subscription_ack_precedes_publish_and_cleanup_is_local(bus):
    first = await bus.subscribe('same-thread')
    second = await bus.subscribe('same-thread')
    other = await bus.subscribe('other-thread')
    frame = {'stream': {'task_id': 'task', 'seq': 1}, 'text': '中文增量'}
    await bus.publish('same-thread', frame)
    assert await asyncio.wait_for(first.queue.get(), 2) == frame
    assert await asyncio.wait_for(second.queue.get(), 2) == frame
    assert other.queue.empty()
    await bus.unsubscribe(first)
    assert first.listener.done() and first not in bus.subscriptions
    await bus.publish('same-thread', {'next': True})
    assert await asyncio.wait_for(second.queue.get(), 2) == {'next': True}
    assert first.queue.get_nowait() is None


@pytest.mark.asyncio
async def test_redis_slow_subscriber_overflow_disconnects_without_blocking_publish(bus):
    subscription = await bus.subscribe('slow-thread')
    pipeline = bus.client.pipeline(transaction=False)
    for seq in range(QUEUE_LIMIT + 4):
        pipeline.publish(bus._key('events', 'slow-thread'), json.dumps({'seq': seq}))
    await pipeline.execute()
    async with asyncio.timeout(3):
        while not subscription.closed:
            await asyncio.sleep(.01)
    assert subscription.queue.qsize() == 1 and subscription.queue.get_nowait() is None
    await bus.unsubscribe(subscription)
    assert subscription.listener.done() and subscription not in bus.subscriptions
    assert await bus.ping()


@pytest.mark.asyncio
async def test_redis_malformed_frame_requires_resync(bus):
    subscription = await bus.subscribe('malformed')
    await bus.client.publish(bus._key('events', 'malformed'), '[]')
    assert await asyncio.wait_for(subscription.queue.get(), 2) is None
    await bus.unsubscribe(subscription)


@pytest.mark.asyncio
async def test_connection_errors_do_not_expose_connection_strings():
    secret = 'do-not-print-this-secret'
    pg = PgStore(f'postgresql://user:{secret}@127.0.0.1:1/missing')
    redis = RedisBus(f'redis://:{secret}@127.0.0.1:1/0')
    for adapter in (pg, redis):
        with pytest.raises(StorageError) as captured:
            await adapter.open()
        assert secret not in str(captured.value)
        assert captured.value.__suppress_context__
