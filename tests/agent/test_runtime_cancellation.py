"""只验证本地 SDK 边界与清理时序，不创建模型请求。"""
import asyncio
from types import SimpleNamespace

import pytest
from agents.models.interface import Model
from openai.types.responses import ResponseTextDeltaEvent
from open_protocol import AgentInput
from agent_runtime import runtime as runtime_module
from agent_runtime.runtime import AgentRuntime, AgentSpec, CancellationCleanupTimeout


class LocalStreamingRun:
    def __init__(self, emit_delta=True):
        self.emit_delta = emit_delta
        self.is_complete = False
        self.final_output = ''
        self.cancel_calls = self.iterator_count = 0
        self.worker_started, self.waiting_events = asyncio.Event(), asyncio.Event()
        self.cleanup_started, self.cleanup_release, self.cleaned = (asyncio.Event() for _ in range(3))
        self.cancel_signal, self.iterator_closed = asyncio.Event(), asyncio.Event()
        self.run_loop_task = asyncio.create_task(self.work())

    async def work(self):
        self.worker_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cleanup_started.set()
            await self.cleanup_release.wait()
            self.cleaned.set()

    def cancel(self):
        self.cancel_calls += 1
        self.is_complete = True
        self.run_loop_task.cancel()
        self.cancel_signal.set()

    async def stream_events(self):
        self.iterator_count += 1
        await self.worker_started.wait()
        try:
            if self.emit_delta:
                yield SimpleNamespace(type='raw_response_event', data=SimpleNamespace(
                    type='response.output_text.delta', delta='本地文本'))
            self.waiting_events.set()
            await self.cancel_signal.wait()
        except asyncio.CancelledError:
            # 对齐已安装 SDK：取消等待事件的消费者会发 cancel 并关闭 iterator。
            self.cancel()
            raise
        finally:
            self.iterator_closed.set()


async def stream_for(monkeypatch, run):
    monkeypatch.setattr(runtime_module.Runner, 'run_streamed', lambda *args, **kwargs: run)
    stream = AgentRuntime('local-placeholder', AgentSpec('local', '', [])).stream(
        AgentInput(thread_id='local-thread', task_id='local-task', user_prompt='local-only'))
    await anext(stream)  # 用户消息。
    await anext(stream)  # 答案基线；此时未调用 SDK。
    return stream


@pytest.mark.asyncio
async def test_close_while_yielded_waits_for_same_sdk_iterator_and_worker(monkeypatch):
    run = LocalStreamingRun()
    stream = await stream_for(monkeypatch, run)
    assert (await anext(stream)).event_v2.op == 'patch'
    closing = asyncio.create_task(stream.aclose())
    await asyncio.wait_for(run.cleanup_started.wait(), 1)
    assert not closing.done() and not run.cleaned.is_set()
    run.cleanup_release.set()
    await asyncio.wait_for(closing, 1)
    assert run.iterator_count == 1 and run.iterator_closed.is_set()
    assert run.run_loop_task.done() and run.cleaned.is_set()


@pytest.mark.asyncio
async def test_cancel_while_awaiting_sdk_events_still_waits_for_background_cleanup(monkeypatch):
    run = LocalStreamingRun(emit_delta=False)
    stream = await stream_for(monkeypatch, run)
    reading = asyncio.create_task(anext(stream))
    await asyncio.wait_for(run.waiting_events.wait(), 1)
    reading.cancel()
    await asyncio.wait_for(run.cleanup_started.wait(), 1)
    assert not reading.done()
    run.cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(reading, 1)
    assert run.iterator_count == 1 and run.cleaned.is_set() and run.run_loop_task.done()


@pytest.mark.asyncio
async def test_repeated_stop_does_not_skip_background_cleanup(monkeypatch):
    run = LocalStreamingRun()
    stream = await stream_for(monkeypatch, run)
    await anext(stream)
    closing = asyncio.create_task(stream.aclose())
    await asyncio.wait_for(run.cleanup_started.wait(), 1)
    closing.cancel()
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done() and not run.cleaned.is_set()
    run.cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(closing, 1)
    assert run.cleaned.is_set() and run.run_loop_task.done()


@pytest.mark.asyncio
async def test_cleanup_timeout_is_explicit_failure_instead_of_confirmed_cancel(monkeypatch):
    monkeypatch.setattr(runtime_module, 'CANCEL_CLEANUP_TIMEOUT', .02)
    run = LocalStreamingRun()
    stream = await stream_for(monkeypatch, run)
    await anext(stream)
    try:
        with pytest.raises(CancellationCleanupTimeout, match='未确认上游停止'):
            await asyncio.wait_for(stream.aclose(), .5)
        assert not run.cleaned.is_set()
    finally:
        run.cleanup_release.set()
        await asyncio.wait_for(asyncio.gather(run.run_loop_task, return_exceptions=True), 1)
        await asyncio.sleep(0)


class BlockingModel(Model):
    """实际 SDK 运行的本地 Model；事件闸门代替网络响应。"""
    def __init__(self, emit_delta):
        self.emit_delta = emit_delta
        self.started, self.cleanup_started, self.cleanup_release, self.cleaned = (asyncio.Event() for _ in range(4))

    async def get_response(self, *args, **kwargs):
        raise AssertionError('测试只允许本地流式 Model')

    async def stream_response(self, *args, **kwargs):
        self.started.set()
        try:
            if self.emit_delta:
                yield ResponseTextDeltaEvent(type='response.output_text.delta', item_id='local-item',
                    output_index=0, content_index=0, delta='local', sequence_number=1, logprobs=[])
            await asyncio.Event().wait()
        finally:
            self.cleanup_started.set()
            await self.cleanup_release.wait()
            self.cleaned.set()


@pytest.mark.asyncio
@pytest.mark.parametrize('emit_delta', [False, True])
async def test_installed_sdk_cancellation_waits_for_local_model_cleanup(emit_delta):
    model = BlockingModel(emit_delta)
    stream = AgentRuntime(model, AgentSpec('local', '', [])).stream(
        AgentInput(thread_id='local-thread', task_id='local-task', user_prompt='local-only'))
    await anext(stream)
    await anext(stream)
    try:
        if emit_delta:
            await asyncio.wait_for(anext(stream), 1)
            operation = asyncio.create_task(stream.aclose())
        else:
            operation = asyncio.create_task(anext(stream))
            await asyncio.wait_for(model.started.wait(), 1)
            operation.cancel()
        await asyncio.wait_for(model.cleanup_started.wait(), 1)
        assert not operation.done() and not model.cleaned.is_set()
        model.cleanup_release.set()
        if emit_delta:
            await asyncio.wait_for(operation, 1)
        else:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(operation, 1)
        assert model.cleaned.is_set()
    finally:
        model.cleanup_release.set()
        await stream.aclose()
