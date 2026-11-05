"""任务主流程：接纳 → 历史 → 本地 Harness → 记录 → 收尾。"""
from __future__ import annotations

import asyncio
import logging
import traceback
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path

from open_protocol import AgentInput, AgentParams, ChatCommand
from agent_runtime.research import ResearchError
from agent_runtime.runtime import CancellationCleanupTimeout

from ..errors import InvalidCommand, StorageError
from ..models.task import TaskRecord, TERMINAL

log = logging.getLogger(__name__)


@dataclass
class RunHandle:
    record: TaskRecord
    work: asyncio.Task | None = None
    phase: str = 'created'


class TaskService:
    def __init__(self, store, records, factories: dict, model: str | None, timeout: float = 300):
        self.store, self.records = store, records
        self.factories, self.model, self.timeout = factories, model, timeout
        self.runs: dict[str, RunHandle] = {}

    async def create_thread(self):
        return {'thread_id': await self.store.create_thread()}

    async def health(self):
        storage = {'postgres': False, 'redis': False}
        for name, adapter in [('postgres', self.store), ('redis', self.records.bus)]:
            try:
                await adapter.ping()
                storage[name] = True
            except StorageError:
                pass
        return {'storage': storage, 'server_epoch': self.records.epoch,
                'ready': all(storage.values()) and bool(self.factories) and not self.records.pending,
                'active_runs': len(self.runs), 'pending_saves': len(self.records.pending),
                'records': dict(self.records.counts)}

    def validate(self, command: ChatCommand):
        if not self.factories:
            raise InvalidCommand('模型未配置，请检查本地模型认证', 503)
        if command.agent not in self.factories:
            raise InvalidCommand('场景未配置；DeepSearch 需要检索凭证')
        if command.model.id and command.model.id != self.model:
            raise InvalidCommand('当前首版只允许使用服务已配置的模型')

    async def submit(self, thread_id: str, command: ChatCommand):
        self.validate(command)
        if self.records.pending:
            raise StorageError('存在尚未保存的终态，请先重试保存')
        admission = await self.store.admit(thread_id, command)
        if admission.created:
            handle = RunHandle(admission.task)
            self.runs[admission.task.id] = handle
            handle.work = asyncio.create_task(self.execute(handle), name=f'run:{admission.task.id}')
        return accepted(admission.task)

    async def execute(self, handle: RunHandle):
        try:
            await self._execute(handle)
        except asyncio.CancelledError:
            await self.finish(handle, 'abandon', '任务已停止')
        except Exception as exc:
            reason = '任务超时' if isinstance(exc, TimeoutError) else f'执行失败：{type(exc).__name__}'
            if isinstance(exc, CancellationCleanupTimeout):
                reason = '取消清理超时，未确认上游停止'
            research_error = exc if isinstance(exc, ResearchError) else exc.__cause__
            if isinstance(research_error, ResearchError):
                # 仅此类型由本项目固定文案构造；不展示供应商响应或任意异常正文。
                reason = f'检索失败：{research_error}'
            frames = [f'{Path(frame.filename).name}:{frame.lineno}:{frame.name}'
                      for frame in traceback.extract_tb(exc.__traceback__)]
            log.warning('event=task_failed task_id=%s error_type=%s frames=%s',
                        handle.record.id, type(exc).__name__, frames)
            if isinstance(research_error, ResearchError):
                log.warning('event=research_rejected task_id=%s reason=%s', handle.record.id, research_error)
            await self.finish(handle, 'failed', reason)
        finally:
            if handle.record.status in TERMINAL:
                self.runs.pop(handle.record.id, None)

    async def _execute(self, handle: RunHandle):
        task = await self.store.set_running(handle.record.id)
        handle.record = task
        if task.status in TERMINAL:
            return
        handle.phase = 'executing'
        await self.records.register(task)
        history = await self.store.history(task.thread_id, task.id)
        request = AgentInput(thread_id=task.thread_id, task_id=task.id,
                             agent=task.command.agent, user_prompt=task.command.question,
                             history_messages=history, params=AgentParams(model=task.command.model))
        terminal = None
        runtime = self.factories[task.command.agent]()
        async with asyncio.timeout(self.timeout), aclosing(runtime.stream(request)) as stream:
            async for update in stream:
                if terminal is not None:
                    raise ValueError('终态之后仍收到执行更新')
                if update.event_v2.task_status in TERMINAL:
                    terminal = update
                    continue
                handle.record = await self.records.record(handle.record, update)
        if terminal is None:
            raise RuntimeError('Agent 流结束但没有完成信号')
        status = terminal.event_v2.task_status
        reason = None
        if status != 'complete' and terminal.event_v2.value:
            reason = terminal.event_v2.value.meta_data.get('content', 'Agent 执行失败')
        await self.finish(handle, status, reason, terminal)

    async def finish(self, handle: RunHandle, status: str, reason: str | None = None, update=None):
        handle.phase = 'saving'
        for attempt in range(3):
            try:
                handle.record = await self.records.finish(handle.record, status, reason, update)
                handle.phase = 'done'
                return
            except StorageError:
                log.warning('event=terminal_save_failed task_id=%s attempt=%s', handle.record.id, attempt + 1)
                if attempt < 2:
                    await asyncio.sleep(0.5 * 2 ** attempt)
        handle.phase = 'save_failed'
        await self.records.recording_error(handle.record)

    async def cancel(self, task_id: str):
        handle = self.runs.get(task_id)
        if handle is None:
            persisted = await self.store.get(task_id)
            if persisted.status in TERMINAL:
                return {'task_id': task_id, 'cancelled': False, 'status': persisted.status}
            raise ValueError('当前进程没有此任务的执行句柄')
        if handle.phase in {'saving', 'save_failed'}:
            return {'task_id': task_id, 'cancelled': False, 'status': handle.record.status,
                    'message': '执行已经结束，正在处理记录保存'}
        handle.work.cancel()
        await asyncio.gather(handle.work, return_exceptions=True)
        if handle.record.status not in TERMINAL:
            await self.finish(handle, 'abandon', '任务已停止')
        if handle.record.status in TERMINAL:
            self.runs.pop(task_id, None)
        return {'task_id': task_id, 'cancelled': handle.record.status == 'abandon',
                'status': handle.record.status}

    async def retry_save(self, task_id: str):
        task = await self.records.retry_save(task_id)
        if task.status in TERMINAL:
            self.runs.pop(task_id, None)
        return accepted(task)

    async def get(self, task_id: str):
        task = await self.store.get(task_id)
        frame = await self.records.snapshot(task.thread_id)
        record = next(value for value in frame['tasks'] if value['task_id'] == task.id)
        messages = {m['msg_id']: m['value'] for m in frame['messages'] if m['task_id'] == task.id}
        return {**record, 'id': task.id, 'thread_id': task.thread_id,
                'question': task.command.question, 'agent': task.command.agent, 'snapshots': messages}

    async def history(self, thread_id: str):
        return await self.records.snapshot(thread_id)

    async def close(self):
        handles = list(self.runs.values())
        for handle in handles:
            if handle.phase in {'created', 'executing'} and handle.work:
                handle.work.cancel()
        work = [handle.work for handle in handles if handle.work]
        if work:
            await asyncio.gather(*work, return_exceptions=True)
        for handle in handles:
            if handle.record.status not in TERMINAL and handle.phase != 'save_failed':
                await self.finish(handle, 'abandon', '服务关闭，任务已停止')


def accepted(task: TaskRecord) -> dict:
    return {'task_id': task.id, 'thread_id': task.thread_id, 'status': task.status,
            'client_request_id': task.command.client_request_id}
