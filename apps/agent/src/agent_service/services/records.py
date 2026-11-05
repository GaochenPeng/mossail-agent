"""消息记录主流程：内存归并 → 按分支保存 → Redis 发布。"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from uuid import uuid4

from open_protocol import Envelope, MessageItem, MessageUpdate

from ..errors import StorageError
from ..models.task import TaskRecord, TERMINAL
from ..models.message import apply_update

log = logging.getLogger(__name__)


def wire(update: MessageUpdate, task: TaskRecord) -> dict:
    frame = update.model_dump(exclude_none=True, by_alias=True)
    frame['stream'] = {'task_id': task.id, 'seq': task.seq}
    return frame


def sync_frame(thread_id: str, tasks: list[TaskRecord]) -> dict:
    return {
        'type': 'sync', 'thread_id': thread_id,
        'heads': {task.id: task.seq for task in tasks},
        'tasks': [dict(task_id=task.id, status=task.status, error=task.error, seq=task.seq,
                       client_request_id=task.command.client_request_id) for task in tasks],
        'messages': [dict(task_id=task.id, msg_id=mid, value=value)
                     for task in tasks for mid, value in task.snapshots.items()],
    }


@dataclass
class PendingSave:
    task: TaskRecord
    update: MessageUpdate


class RecordService:
    def __init__(self, store, bus, mirror_interval: float = 1.0):
        self.store, self.bus = store, bus
        self.epoch = uuid4().hex
        self.mirror_interval = mirror_interval
        self.live: dict[str, TaskRecord] = {}
        self.pending: dict[str, PendingSave] = {}
        self.locks = defaultdict(asyncio.Lock)
        self.last_mirror: dict[str, float] = {}
        self.counts = defaultdict(int)

    async def register(self, task: TaskRecord):
        async with self.locks[task.thread_id]:
            self.live[task.id] = task
            await self._mirror(task)

    async def record(self, task: TaskRecord, update: MessageUpdate) -> TaskRecord:
        if update.event_v2.task_status in TERMINAL:
            raise ValueError('终态必须经 finish 提交')
        async with self.locks[task.thread_id]:
            previous = self.live.get(task.id)
            if previous is None or previous.status in TERMINAL:
                raise ValueError('任务未注册或已经结束，拒绝迟到更新')
            current = apply_update(previous, update)
            self.live[task.id] = current
            user = update.event_v2.value and update.event_v2.value.sender == 'user'
            structural = len(previous.snapshots) != len(current.snapshots)
            if user:
                current = await self.store.save(current)
                self.live[task.id] = current
                self.counts['pg_snapshots'] += 1
            if user or structural or time.monotonic() - self.last_mirror.get(task.id, 0) >= self.mirror_interval:
                await self._mirror(current)
            await self._publish(current.thread_id, wire(update, current))
            return current

    async def finish(self, task: TaskRecord, status: str, error: str | None = None,
                     update: MessageUpdate | None = None) -> TaskRecord:
        async with self.locks[task.thread_id]:
            current = self.live.get(task.id, task)
            if current.status in TERMINAL:
                return current
            if update is None:
                update = failure_update(current, status, error or '任务已结束')
            candidate = apply_update(current, update)
            candidate = replace(candidate, status=status, error=error,
                                finished_at=datetime.now(timezone.utc).isoformat())
            pending = PendingSave(candidate, update)
            self.pending[task.id] = pending
            return await self._commit(pending)

    async def retry_save(self, task_id: str) -> TaskRecord:
        pending = self.pending.get(task_id)
        if pending is None:
            return await self.store.get(task_id)
        async with self.locks[pending.task.thread_id]:
            pending = self.pending.get(task_id)
            if pending is None:
                return await self.store.get(task_id)
            return await self._commit(pending)

    async def _commit(self, pending: PendingSave) -> TaskRecord:
        # PG 提交之前保留原工作状态与 pending，不发送 complete/abandon。
        saved = await self.store.save(pending.task)
        self.counts['pg_snapshots'] += 1
        self.live[saved.id] = saved
        self.pending.pop(saved.id, None)
        try:
            await self.bus.discard(saved)
        except StorageError:
            self.counts['mirror_errors'] += 1
            log.warning('event=mirror_delete_failed task_id=%s', saved.id)
        if saved.seq == pending.task.seq and saved.status == pending.task.status:
            frame = wire(pending.update, saved)
        else:
            frame = await self._snapshot(saved.thread_id)
        await self._publish(saved.thread_id, frame)
        if saved.status in TERMINAL:
            self.live.pop(saved.id, None)
            self.last_mirror.pop(saved.id, None)
        log.info('event=task_persisted task_id=%s status=%s seq=%s', saved.id, saved.status, saved.seq)
        return saved

    async def snapshot(self, thread_id: str) -> dict:
        async with self.locks[thread_id]:
            return await self._snapshot(thread_id)

    async def _snapshot(self, thread_id: str) -> dict:
        if not await self.store.thread_exists(thread_id):
            raise KeyError('会话不存在')
        tasks = await self.store.list_tasks(thread_id)
        try:
            mirror = await self.bus.read(thread_id)
        except StorageError:
            mirror = None
            self.counts['mirror_errors'] += 1
        for index, task in enumerate(tasks):
            if task.status in TERMINAL:
                continue
            choices = [self.live.get(task.id), mirror]
            for candidate in choices:
                if candidate and candidate.id == task.id and candidate.generation == task.generation and candidate.seq >= task.seq:
                    task = candidate
            tasks[index] = task
        frame = sync_frame(thread_id, tasks)
        # 进程重启可能只找回较早的Redis检查点；新实例的权威同步需可识别。
        frame['server_epoch'] = self.epoch
        for task in frame['tasks']:
            task['save_pending'] = task['task_id'] in self.pending
        return frame

    async def recover(self):
        # claim_owner 已排除同 schema 的第二个 Server；这里只收尾旧进程残留。
        for task in await self.store.unfinished():
            try:
                mirror = await self.bus.read(task.thread_id)
            except StorageError:
                mirror = None
            if mirror and mirror.id == task.id and mirror.generation == task.generation and mirror.seq > task.seq:
                task = mirror
            self.live[task.id] = task
            await self.finish(task, 'failed', 'INTERRUPTED：服务重启，执行已中断；请重新提交')

    async def recording_error(self, task: TaskRecord):
        await self._publish(task.thread_id, {
            'type': 'recording_error', 'thread_id': task.thread_id, 'task_id': task.id,
            'message': '执行已结束，但终态保存失败；请重试保存，不要重复执行任务',
        })

    async def _mirror(self, task: TaskRecord):
        try:
            if await self.bus.activate(task) and await self.bus.mirror(task):
                self.last_mirror[task.id] = time.monotonic()
                self.counts['redis_mirrors'] += 1
        except StorageError:
            self.counts['mirror_errors'] += 1
            log.warning('event=mirror_write_failed task_id=%s seq=%s', task.id, task.seq)

    async def _publish(self, thread_id: str, frame: dict):
        try:
            await self.bus.publish(thread_id, frame)
            self.counts['published'] += 1
        except StorageError:
            self.counts['publish_errors'] += 1
            log.warning('event=publish_failed thread_id=%s', thread_id)


def failure_update(task: TaskRecord, status: str, reason: str) -> MessageUpdate:
    return MessageUpdate(event_v2=Envelope(
        thread_id=task.thread_id, task_id=task.id, msg_id=f'{task.id}_outcome',
        op='add', task_status=status,
        value=MessageItem(index=1_000_000, type='markdown', status='error',
                          meta_data={'content': reason}),
    ))
