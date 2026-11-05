"""记录流程用故障替身验证；不调用模型，不把替身结果当成中间件实连。"""
import asyncio
import copy

import jsonpatch
import pytest
from open_protocol import ChatCommand, Envelope, MessageItem, MessageUpdate, PatchOperation
from agent_runtime.research import ResearchError

from agent_service.errors import StorageError
from agent_service.models.task import TaskAdmission, TaskRecord, TERMINAL
from agent_service.services.records import RecordService
from agent_service.models.message import apply_update
from agent_service.services.tasks import TaskService


def _task():
    return TaskRecord(id='task', thread_id='thread', generation=1,
                      command=ChatCommand(question='输入问题', client_request_id='request'))


def _add(task, msg_id='answer', sender='assistant', content='', status='loading'):
    return MessageUpdate(event_v2=Envelope(
        thread_id=task.thread_id, task_id=task.id, msg_id=msg_id, op='add',
        value=MessageItem(index=0 if sender == 'user' else 1, sender=sender,
                          type='markdown', status=status, meta_data={'content': content})))


def _patch(task, content='完整答案', terminal=False):
    return MessageUpdate(event_v2=Envelope(
        thread_id=task.thread_id, task_id=task.id, msg_id='answer', op='patch',
        task_status='complete' if terminal else 'running', patches=[
            PatchOperation(op='replace', path='/meta_data/content', value=content),
            PatchOperation(op='replace', path='/status', value='success' if terminal else 'loading'),
        ]))


class MemoryStore:
    def __init__(self, task):
        self.tasks = {task.id: copy.deepcopy(task)}
        self.save_calls = []
        self.fail_terminal = False

    async def admit(self, thread_id, command):
        task = next(iter(self.tasks.values()))
        return TaskAdmission(copy.deepcopy(task), True)

    async def get(self, task_id):
        return copy.deepcopy(self.tasks[task_id])

    async def save(self, task):
        self.save_calls.append(copy.deepcopy(task))
        if self.fail_terminal and task.status in TERMINAL:
            raise StorageError('注入终态保存失败')
        previous = self.tasks[task.id]
        if previous.status in TERMINAL or previous.seq > task.seq:
            return copy.deepcopy(previous)
        self.tasks[task.id] = copy.deepcopy(task)
        return copy.deepcopy(task)

    async def set_running(self, task_id):
        self.tasks[task_id].status = 'running'
        return await self.get(task_id)

    async def list_tasks(self, thread_id):
        return [copy.deepcopy(task) for task in self.tasks.values() if task.thread_id == thread_id]

    async def thread_exists(self, thread_id):
        return any(task.thread_id == thread_id for task in self.tasks.values())

    async def history(self, thread_id, before_task):
        return []


class MemoryBus:
    def __init__(self):
        self.frames = []
        self.mirrors = []
        self.live = None
        self.fail = False

    def _check(self):
        if self.fail:
            raise StorageError('注入 Redis 不可用')

    async def activate(self, task):
        self._check()
        return True

    async def mirror(self, task):
        self._check()
        self.mirrors.append(copy.deepcopy(task))
        self.live = copy.deepcopy(task)
        return True

    async def discard(self, task):
        self._check()
        self.live = None
        return True

    async def read(self, thread_id):
        self._check()
        return copy.deepcopy(self.live)

    async def publish(self, thread_id, frame):
        self._check()
        self.frames.append(copy.deepcopy(frame))


def test_apply_update_failure_preserves_previous_baseline_and_address():
    task = apply_update(_task(), _add(_task(), content='有效基线'))
    original = copy.deepcopy(task.to_dict())
    update = MessageUpdate(event_v2=Envelope(
        thread_id=task.thread_id, task_id=task.id, msg_id='answer', op='patch', patches=[
            PatchOperation(op='replace', path='/meta_data/content', value='部分写入'),
            PatchOperation(op='remove', path='/不存在的字段'),
        ]))
    with pytest.raises(jsonpatch.JsonPatchConflict):
        apply_update(task, update)
    assert task.to_dict() == original
    invalid = _patch(task)
    invalid.event_v2.task_id = 'different-task'
    with pytest.raises(ValueError, match='消息地址'):
        apply_update(task, invalid)
    assert task.to_dict() == original
    update = _patch(task)
    update.event_v2.patches[-1].value = 'invalid-status'
    with pytest.raises(ValueError):
        apply_update(task, update)
    assert task.to_dict() == original


@pytest.mark.asyncio
async def test_user_snapshot_is_durable_but_regular_patches_do_not_write_pg():
    task = _task()
    store, bus = MemoryStore(task), MemoryBus()
    records = RecordService(store, bus, mirror_interval=3600)
    await records.register(task)
    task = await records.record(task, _add(task, 'question', 'user', '输入问题', 'success'))
    assert len(store.save_calls) == 1
    assert store.tasks[task.id].snapshots['question']['meta_data']['content'] == '输入问题'
    task = await records.record(task, _add(task))
    mirrored = len(bus.mirrors)
    for index in range(20):
        task = await records.record(task, _patch(task, content=f'答案增量{index}'))
    assert len(store.save_calls) == 1 and len(bus.mirrors) == mirrored
    assert task.seq == 22
    assert bus.frames[-1]['stream'] == {'task_id': task.id, 'seq': 22}
    saved = await records.finish(task, 'complete', update=_patch(task, terminal=True))
    assert saved.status == 'complete' and len(store.save_calls) == 2
    assert store.tasks[task.id].snapshots['answer']['meta_data']['content'] == '完整答案'
    assert bus.live is None


@pytest.mark.asyncio
async def test_terminal_save_failure_retains_pending_and_does_not_publish_complete():
    task = _task()
    store, bus = MemoryStore(task), MemoryBus()
    records = RecordService(store, bus)
    await records.register(task)
    task = await records.record(task, _add(task))
    previous = copy.deepcopy(records.live[task.id].to_dict())
    published = len(bus.frames)
    store.fail_terminal = True
    with pytest.raises(StorageError):
        await records.finish(task, 'complete', update=_patch(task, terminal=True))
    assert task.id in records.pending
    assert records.pending[task.id].task.status == 'complete'
    assert records.live[task.id].to_dict() == previous
    assert len(bus.frames) == published
    assert store.tasks[task.id].status == 'submitted'
    assert (await records.snapshot(task.thread_id))['tasks'][0]['save_pending'] is True
    store.fail_terminal = False
    saved = await records.retry_save(task.id)
    assert saved.status == 'complete' and task.id not in records.pending
    assert bus.frames[-1]['event_v2']['task_status'] == 'complete'
    assert (await records.snapshot(task.thread_id))['tasks'][0]['save_pending'] is False


@pytest.mark.asyncio
async def test_retry_save_does_not_start_another_runtime():
    task = _task()
    store, bus = MemoryStore(task), MemoryBus()
    records = RecordService(store, bus)
    calls = []

    class Runtime:
        async def stream(self, request):
            calls.append(request.task_id)
            yield _add(task, 'question', 'user', '输入问题', 'success')
            yield _add(task)
            yield _patch(task, terminal=True)

    service = TaskService(store, records, {'assistant': Runtime}, model=None)
    store.fail_terminal = True
    await service.submit(task.thread_id, task.command)
    handle = service.runs[task.id]
    await asyncio.wait_for(handle.work, timeout=3)
    assert handle.phase == 'save_failed' and calls == [task.id]
    assert task.id in records.pending and store.tasks[task.id].status == 'running'
    assert bus.frames[-1]['type'] == 'recording_error'
    assert not any(frame.get('event_v2', {}).get('task_status') == 'complete' for frame in bus.frames)
    store.fail_terminal = False
    result = await service.retry_save(task.id)
    assert result['status'] == 'complete' and calls == [task.id]
    assert task.id not in service.runs and task.id not in records.pending


@pytest.mark.asyncio
async def test_pg_terminal_remains_readable_when_redis_is_unavailable():
    task = _task()
    store, bus = MemoryStore(task), MemoryBus()
    records = RecordService(store, bus)
    await records.register(task)
    task = await records.record(task, _add(task))
    bus.fail = True
    saved = await records.finish(task, 'complete', update=_patch(task, terminal=True))
    assert saved.status == 'complete' and task.id not in records.pending
    service = TaskService(store, records, {}, model=None)
    result = await service.get(task.id)
    assert result['status'] == 'complete'
    assert result['snapshots']['answer']['meta_data']['content'] == '完整答案'
    snapshot = await records.snapshot(task.thread_id)
    assert snapshot['tasks'][0]['status'] == 'complete'
    assert records.counts['publish_errors'] == 1


@pytest.mark.asyncio
async def test_late_running_patch_after_terminal_cannot_publish_or_restore_mirror():
    task = _task()
    store, bus = MemoryStore(task), MemoryBus()
    records = RecordService(store, bus)
    await records.register(task)
    task = await records.record(task, _add(task))
    completed = await records.finish(task, 'complete', update=_patch(task, terminal=True))
    published, mirrored = len(bus.frames), len(bus.mirrors)
    assert task.id not in records.live and task.id not in records.last_mirror
    with pytest.raises(ValueError, match='未注册或已经结束'):
        await records.record(task, _patch(task, content='迟到的 running 更新'))
    assert len(bus.frames) == published and len(bus.mirrors) == mirrored
    assert bus.live is None and store.tasks[task.id].to_dict() == completed.to_dict()


@pytest.mark.asyncio
async def test_cancel_before_runtime_starts_still_records_abandon():
    task = _task()
    store, bus = MemoryStore(task), MemoryBus()
    records = RecordService(store, bus)
    calls = []

    class Runtime:
        async def stream(self, request):
            calls.append(request.task_id)
            yield _add(task)

    service = TaskService(store, records, {'assistant': Runtime}, model=None)
    await service.submit(task.thread_id, task.command)
    result = await service.cancel(task.id)
    assert result['cancelled'] and result['status'] == 'abandon'
    assert store.tasks[task.id].status == 'abandon'
    assert calls == [] and task.id not in service.runs


@pytest.mark.asyncio
async def test_research_rejection_keeps_safe_reason_and_failure_record():
    task = _task()
    store, bus = MemoryStore(task), MemoryBus()
    records = RecordService(store, bus)

    class Runtime:
        async def stream(self, request):
            yield _add(task)
            # SDK会把工具内的业务异常包装；对外仍应识别原固定文案。
            raise RuntimeError('tool wrapper') from ResearchError('回答缺少来源标记')

    service = TaskService(store, records, {'assistant': Runtime}, model=None)
    await service.submit(task.thread_id, task.command)
    await service.runs[task.id].work
    saved = store.tasks[task.id]
    assert saved.status == 'failed' and saved.error == '检索失败：回答缺少来源标记'
    assert saved.snapshots[f'{task.id}_outcome']['meta_data']['content'] == saved.error
    assert task.id not in service.runs
