"""SDK 事件转换为现有 event_v2；不读取数据库或客户端连接。"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Callable

from agents import Agent, ModelSettings, Runner, RunConfig
from open_protocol import AgentInput, Envelope, MessageItem, MessageUpdate, PatchOperation

CANCEL_CLEANUP_TIMEOUT = 5.0


class CancellationCleanupTimeout(RuntimeError):
    """已请求取消，但尚未确认 SDK 后台运行结束。"""


async def cancel_stream(run, events):
    if not run.is_complete:
        run.cancel()

    async def settle():
        try:
            # 使用原 iterator：外层暂停在 yield 时，SDK 仍需消费取消信号并清理。
            async for _ in events:
                pass
        finally:
            try:
                await events.aclose()
            finally:
                work = getattr(run, 'run_loop_task', None)
                if work is not None:
                    await asyncio.gather(work, return_exceptions=True)

    cleanup = asyncio.create_task(settle(), name='sdk-cancel-cleanup')
    deadline = asyncio.get_running_loop().time() + CANCEL_CLEANUP_TIMEOUT
    interrupted = None
    while not cleanup.done():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            cleanup.cancel()
            cleanup.add_done_callback(lambda task: None if task.cancelled() else task.exception())
            raise CancellationCleanupTimeout('取消清理超时，未确认上游停止')
        try:
            await asyncio.wait({cleanup}, timeout=remaining)
        except asyncio.CancelledError as exc:
            # 重复 stop 不能提前跳过清理，也不能重置等待上限。
            interrupted = exc
    await cleanup
    if interrupted is not None:
        raise interrupted


@dataclass
class FinalAnswer:
    content: str
    sources: list[dict] = field(default_factory=list)


@dataclass
class AgentSpec:
    name: str
    instructions: str
    tools: list[Any]
    finalize: Callable[[str], FinalAnswer] = FinalAnswer


class Formatter:
    def __init__(self, request: AgentInput):
        self.request = request
        self.index = 0

    def add(self, msg_id: str, kind: str, data: dict, *, sender='assistant', status='loading'):
        self.index += 1
        return self.frame(msg_id, 'add', value=MessageItem(
            index=self.index, sender=sender, type=kind, status=status, meta_data=data))

    def patch(self, msg_id: str, **fields):
        return self.frame(msg_id, 'patch', patches=[PatchOperation(op='replace', path=path, value=value)
                         for path, value in fields.items()])

    def frame(self, msg_id: str, op: str, **kwargs):
        return MessageUpdate(event_v2=Envelope(thread_id=self.request.thread_id,
            task_id=self.request.task_id, msg_id=msg_id, op=op, **kwargs))


class AgentRuntime:
    def __init__(self, model: Any, spec: AgentSpec, execution: dict | None = None,
                 *, model_settings: ModelSettings | None = None):
        self.model, self.spec = model, spec
        self.execution = execution or {}
        self.model_settings = model_settings or ModelSettings()

    async def stream(self, request: AgentInput) -> AsyncIterator[MessageUpdate]:
        fmt = Formatter(request)
        yield fmt.add(f'{request.task_id}_user', 'markdown', {'content': request.user_prompt,
                      'execution': {**self.execution, 'agent': self.spec.name}},
                      sender='user', status='success')
        answer_id = f'{request.task_id}_answer'
        yield fmt.add(answer_id, 'markdown', {'content': '', 'source': []})
        agent = Agent(name=self.spec.name, instructions=self.spec.instructions,
                      model=self.model, tools=self.spec.tools, model_settings=self.model_settings)
        inputs = [item.model_dump() for item in request.history_messages]
        inputs.append({'role': 'user', 'content': request.user_prompt})
        run = Runner.run_streamed(agent, input=inputs, max_turns=8,
                                  run_config=RunConfig(tracing_disabled=True))
        events = run.stream_events()
        content, last_push = '', 0.0
        try:
            async for event in events:
                if event.type == 'raw_response_event' and event.data.type == 'response.output_text.delta':
                    content += event.data.delta
                    now = time.monotonic()
                    if now - last_push >= 0.1:
                        yield fmt.patch(answer_id, **{'/meta_data/content': content})
                        last_push = now
                elif event.type == 'run_item_stream_event':
                    item = event.item
                    if event.name == 'tool_called':
                        raw = item.raw_item
                        yield fmt.add(raw.call_id, 'tool_call', {'name': raw.name, 'arguments': raw.arguments, 'output': ''})
                    elif event.name == 'tool_output':
                        raw = item.raw_item
                        call_id = raw.get('call_id') if isinstance(raw, dict) else raw.call_id
                        output = item.output if isinstance(item.output, str) else json.dumps(item.output, ensure_ascii=False)
                        yield fmt.patch(call_id, **{'/meta_data/output': output, '/status': 'success'})
            final = self.spec.finalize(str(run.final_output or content))
            yield fmt.patch(answer_id, **{'/meta_data/content': final.content,
                '/meta_data/source': final.sources, '/status': 'success'})
            yield fmt.frame(answer_id, 'patch', task_status='complete', patches=[])
        finally:
            work = getattr(run, 'run_loop_task', None)
            if not run.is_complete or (work is not None and not work.done()):
                await cancel_stream(run, events)
