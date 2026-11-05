"""Redis 运行镜像与 Pub/Sub；owner 代次用于拒绝旧任务迟到写入。"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import redis.asyncio as redis

from ..errors import StorageError
from ..models.task import TaskRecord

TIMEOUT = 8
MIRROR_TTL = 1800
QUEUE_LIMIT = 256

ACTIVATE = '''
local raw = redis.call('GET', KEYS[1])
if raw then
    local owner = cjson.decode(raw)
    if owner.generation > tonumber(ARGV[2]) then return 0 end
    if owner.generation == tonumber(ARGV[2]) and owner.task_id ~= ARGV[1] then return 0 end
end
redis.call('SET', KEYS[1], cjson.encode({task_id=ARGV[1],generation=tonumber(ARGV[2])}))
return 1
'''

MIRROR = '''
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local owner = cjson.decode(raw)
if owner.task_id ~= ARGV[1] or owner.generation ~= tonumber(ARGV[2]) then return 0 end
local current = redis.call('GET', KEYS[2])
if current then
    local previous = cjson.decode(current)
    if previous.id == ARGV[1] and previous.generation == tonumber(ARGV[2]) and previous.seq > tonumber(ARGV[3]) then return 0 end
end
redis.call('SET', KEYS[2], ARGV[4], 'EX', ARGV[5])
return 1
'''

READ = '''
local raw = redis.call('GET', KEYS[1])
local live = redis.call('GET', KEYS[2])
if not raw or not live then return nil end
local owner = cjson.decode(raw)
local task = cjson.decode(live)
if owner.task_id ~= task.id or owner.generation ~= task.generation then return nil end
return live
'''

DISCARD = '''
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local task = cjson.decode(raw)
if task.id ~= ARGV[1] or task.generation ~= tonumber(ARGV[2]) then return 0 end
return redis.call('DEL', KEYS[1])
'''


@dataclass(eq=False)
class RedisSubscription:
    thread_id: str
    pubsub: object
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=QUEUE_LIMIT))
    listener: asyncio.Task | None = None
    closed: bool = False


def _disconnect(subscription: RedisSubscription):
    # 丢弃尚未交付的增量，只保留断线信号；重连必须重新取得 PG/Redis 完整快照。
    while not subscription.queue.empty():
        subscription.queue.get_nowait()
    subscription.queue.put_nowait(None)


async def _close_pubsub(pubsub):
    try:
        async with asyncio.timeout(TIMEOUT):
            await pubsub.aclose()
    except Exception:
        pass


class RedisBus:
    def __init__(self, redis_url: str, namespace: str = 'mossail:dev'):
        if not namespace or any(char in namespace for char in '\r\n'):
            raise ValueError('Redis namespace 不合法')
        self.redis_url = redis_url
        self.namespace = namespace
        self.client = None
        self.subscriptions = set()

    def _key(self, kind: str, thread_id: str) -> str:
        return f'{self.namespace}:{kind}:{thread_id}'

    def _client(self):
        if self.client is None:
            raise StorageError('Redis 尚未连接')
        return self.client

    async def _call(self, method, *args):
        try:
            async with asyncio.timeout(TIMEOUT):
                return await method(*args)
        except Exception:
            raise StorageError('Redis 读写失败') from None

    async def open(self):
        if self.client is not None:
            return
        client = None
        try:
            client = redis.Redis.from_url(self.redis_url, decode_responses=True,
                                         socket_timeout=TIMEOUT, socket_connect_timeout=TIMEOUT,
                                         health_check_interval=30)
            await self._call(client.ping)
        except Exception:
            if client is not None:
                await self._call(client.aclose)
            raise StorageError('Redis 连接失败') from None
        self.client = client

    async def close(self):
        for subscription in list(self.subscriptions):
            await self.unsubscribe(subscription)
        client, self.client = self.client, None
        if client is not None:
            await self._call(client.aclose)

    async def ping(self):
        return await self._call(self._client().ping)

    async def activate(self, task: TaskRecord) -> bool:
        return bool(await self._call(self._client().eval, ACTIVATE, 1,
                                    self._key('owner', task.thread_id), task.id, task.generation))

    async def mirror(self, task: TaskRecord) -> bool:
        return bool(await self._call(self._client().eval, MIRROR, 2,
                                    self._key('owner', task.thread_id), self._key('live_ev2', task.thread_id),
                                    task.id, task.generation, task.seq,
                                    json.dumps(task.to_dict(), ensure_ascii=False), MIRROR_TTL))

    async def read(self, thread_id: str) -> TaskRecord | None:
        raw = await self._call(self._client().eval, READ, 2,
                               self._key('owner', thread_id), self._key('live_ev2', thread_id))
        if raw is None:
            return None
        try:
            task = TaskRecord.from_dict(json.loads(raw))
            if task.thread_id != thread_id:
                raise ValueError('镜像会话不匹配')
            return task
        except Exception:
            raise StorageError('Redis 运行镜像格式无效') from None

    async def discard(self, task: TaskRecord) -> bool:
        return bool(await self._call(self._client().eval, DISCARD, 1,
                                    self._key('live_ev2', task.thread_id), task.id, task.generation))

    async def publish(self, thread_id: str, frame: dict):
        await self._call(self._client().publish, self._key('events', thread_id),
                         json.dumps(frame, ensure_ascii=False))

    async def subscribe(self, thread_id: str) -> RedisSubscription:
        pubsub = self._client().pubsub(ignore_subscribe_messages=False)
        subscription = RedisSubscription(thread_id, pubsub)
        channel = self._key('events', thread_id)
        try:
            async with asyncio.timeout(TIMEOUT):
                await pubsub.subscribe(channel)
                while True:
                    message = await pubsub.get_message(ignore_subscribe_messages=False, timeout=1)
                    if message and message['type'] == 'subscribe' and message['channel'] == channel:
                        break
            self.subscriptions.add(subscription)
            subscription.listener = asyncio.create_task(self._listen(subscription))
            return subscription
        except BaseException as exc:
            await _close_pubsub(pubsub)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise StorageError('Redis 订阅失败') from None

    async def _listen(self, subscription: RedisSubscription):
        try:
            while not subscription.closed:
                message = await subscription.pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
                if message is None or message['type'] != 'message':
                    continue
                frame = json.loads(message['data'])
                if not isinstance(frame, dict):
                    raise ValueError('Pub/Sub 帧不是对象')
                subscription.queue.put_nowait(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            _disconnect(subscription)
        finally:
            if not subscription.closed:
                subscription.closed = True
                await _close_pubsub(subscription.pubsub)

    async def unsubscribe(self, subscription: RedisSubscription):
        already_closed = subscription.closed
        subscription.closed = True
        listener = subscription.listener
        if listener is not None and not listener.done():
            listener.cancel()
        if listener is not None:
            try:
                await listener
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        try:
            async with asyncio.timeout(TIMEOUT):
                if not already_closed:
                    await subscription.pubsub.unsubscribe(self._key('events', subscription.thread_id))
                await subscription.pubsub.aclose()
        except Exception:
            # socket 已失效也必须完成本地清理，订阅生命周期不影响执行任务。
            await _close_pubsub(subscription.pubsub)
        finally:
            self.subscriptions.discard(subscription)
            _disconnect(subscription)
