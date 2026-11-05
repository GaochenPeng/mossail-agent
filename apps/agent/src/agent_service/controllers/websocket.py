"""WebSocket Controller：维护连接订阅，执行任务的生命周期属于 Service。"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from open_protocol import ChatCommand
from pydantic import ValidationError

from ..errors import StorageError

router = APIRouter()


@dataclass
class Connection:
    socket: WebSocket
    service: object
    bus: object
    thread_id: str | None = None
    subscription: object = None
    sender: asyncio.Task | None = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, frame: dict):
        async with asyncio.timeout(10), self.send_lock:
            await self.socket.send_json(frame)

    async def observe(self, thread_id: str, force: bool = False):
        if self.thread_id == thread_id and not force:
            return
        await self.release_subscription()
        subscription = await self.bus.subscribe(thread_id)
        try:
            initial = await self.service.history(thread_id)
            await self.send(initial)
        except BaseException:
            await self.bus.unsubscribe(subscription)
            raise
        self.thread_id, self.subscription = thread_id, subscription
        self.sender = asyncio.create_task(self.send_updates(subscription), name=f'ws:{thread_id}')

    async def send_updates(self, subscription):
        try:
            while True:
                try:
                    frame = await asyncio.wait_for(subscription.queue.get(), timeout=10)
                except TimeoutError:
                    state = await self.service.history(subscription.thread_id)
                    frame = {'type': 'heads', 'thread_id': subscription.thread_id,
                             'server_epoch': state['server_epoch'],
                             'heads': state['heads'], 'tasks': state['tasks']}
                if frame is None:
                    await self.socket.close(code=1013, reason='请重连恢复完整快照')
                    return
                await self.send(frame)
        except (WebSocketDisconnect, RuntimeError, OSError, StorageError, TimeoutError):
            try:
                async with asyncio.timeout(3):
                    await self.socket.close(code=1013)
            except (RuntimeError, OSError, TimeoutError):
                pass

    async def receive(self, raw: dict):
        kind = raw.get('type', 'chat')
        if kind == 'ping':
            await self.send({'type': 'pong'})
        elif kind in {'reconnect', 'sync_request'}:
            await self.observe(raw['thread_id'], force=True)
        elif kind == 'chat':
            await self.observe(raw['thread_id'])
            body = {key: value for key, value in raw.items()
                    if key not in {'type', 'thread_id', 'protocol_version'}}
            result = await self.service.submit(raw['thread_id'], ChatCommand.model_validate(body))
            await self.send({'type': 'task_accepted', **result})
        elif kind == 'stop':
            result = await self.service.cancel(raw['task_id'])
            await self.send({'type': 'cancel_result', **result})
        else:
            raise ValueError('不支持的消息类型')

    async def release_subscription(self):
        if self.sender:
            self.sender.cancel()
            await asyncio.gather(self.sender, return_exceptions=True)
            self.sender = None
        if self.subscription:
            await self.bus.unsubscribe(self.subscription)
            self.subscription = None
        self.thread_id = None


@router.websocket('/ws/chat/')
async def websocket(socket: WebSocket):
    await socket.accept()
    connection = Connection(socket, socket.app.state.service, socket.app.state.bus)
    try:
        while True:
            try:
                raw = await socket.receive_json()
            except json.JSONDecodeError:
                await connection.send({'type': 'error', 'message': '消息不是合法 JSON'})
                continue
            try:
                if not isinstance(raw, dict):
                    raise ValueError('消息必须为对象')
                await connection.receive(raw)
            except (KeyError, ValueError, StorageError) as exc:
                message = '请求字段不合法' if isinstance(exc, (ValidationError, KeyError)) else str(exc)
                request = raw if isinstance(raw, dict) else {}
                await connection.send({'type': 'error', 'message': message,
                                       'thread_id': request.get('thread_id'),
                                       'client_request_id': request.get('client_request_id')})
    except (WebSocketDisconnect, TimeoutError):
        pass
    finally:
        await connection.release_subscription()
