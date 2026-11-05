"""PG 任务记录：接纳时锁会话，保存时锁任务，完整快照一次提交。"""
from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

import asyncpg
from open_protocol import ChatCommand, ChatMessage

from ..errors import StorageError
from ..models.task import TaskAdmission, TaskRecord, TERMINAL

TIMEOUT = 8
STATUSES = {'submitted', 'running', *TERMINAL}


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _iso(value):
    return value.isoformat() if value is not None else None


def _is_open(connection):
    try:
        return connection is not None and not connection.is_closed()
    except asyncpg.InterfaceError:
        # asyncpg 在连接断开时会把池代理分离，is_closed 本身也可能抛错。
        return False


def _result(task: TaskRecord) -> dict:
    return {'protocol_version': 2, 'events_v2': [
        {'op': 'add', 'thread_id': task.thread_id, 'task_id': task.id,
         'msg_id': msg_id, 'task_status': task.status, 'value': value}
        for msg_id, value in task.snapshots.items()
    ]}


def _record(row) -> TaskRecord:
    snapshots = {}
    for event in _json(row['result_json']).get('events_v2', []):
        # 投影必须属于当前任务，不能把其他任务的同名 msg_id 混入快照。
        if (event['thread_id'], event['task_id']) != (row['thread_id'], row['id']):
            raise StorageError('PG 任务快照身份不匹配')
        snapshots[event['msg_id']] = event['value']
    return TaskRecord(
        id=row['id'], thread_id=row['thread_id'],
        command=ChatCommand.model_validate(_json(row['command_json'])),
        status=row['status'], error=row['error'], generation=row['generation'],
        seq=row['seq'], snapshots=snapshots, created_at=_iso(row['created_at']),
        finished_at=_iso(row['finished_at']),
    )


class PgStore:
    def __init__(self, database_url: str, schema: str = 'mossail'):
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,62}', schema):
            raise ValueError('PG schema 名称不合法')
        self.database_url = database_url
        self.schema = schema
        self.pool = None
        self.owner = None
        self._owner_required = False
        self._owner_key = f'mossail:server:{schema}'
        self._threads = f'"{schema}".threads'
        self._tasks = f'"{schema}".tasks'

    async def open(self):
        if self.pool is not None:
            return
        self._owner_required = False
        try:
            async with asyncio.timeout(TIMEOUT):
                self.pool = await asyncpg.create_pool(
                    self.database_url, min_size=1, max_size=6,
                    timeout=TIMEOUT, command_timeout=TIMEOUT,
                )
                async with self.pool.acquire() as db, db.transaction():
                    await db.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
                    await db.execute(f'''
                        CREATE TABLE IF NOT EXISTS {self._threads} (
                            id TEXT PRIMARY KEY,
                            generation BIGINT NOT NULL DEFAULT 0,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                        );
                        CREATE TABLE IF NOT EXISTS {self._tasks} (
                            id TEXT PRIMARY KEY,
                            thread_id TEXT NOT NULL REFERENCES {self._threads}(id),
                            client_request_id TEXT,
                            generation BIGINT NOT NULL,
                            command_json JSONB NOT NULL,
                            result_json JSONB NOT NULL DEFAULT '{{"protocol_version":2,"events_v2":[]}}',
                            status TEXT NOT NULL CHECK (status IN ('submitted','running','complete','failed','abandon')),
                            error TEXT,
                            seq BIGINT NOT NULL DEFAULT 0 CHECK (seq >= 0),
                            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                            finished_at TIMESTAMPTZ,
                            UNIQUE(thread_id, generation),
                            UNIQUE(thread_id, client_request_id)
                        );
                        CREATE UNIQUE INDEX IF NOT EXISTS one_active_task
                            ON {self._tasks}(thread_id) WHERE status IN ('submitted','running');
                        CREATE INDEX IF NOT EXISTS tasks_thread_generation
                            ON {self._tasks}(thread_id,generation);
                    ''')
        except Exception:
            if self.pool is not None:
                self.pool.terminate()
                self.pool = None
            raise StorageError('PG 连接或初始化失败') from None

    @asynccontextmanager
    async def _connection(self):
        if self.pool is None:
            raise StorageError('PG 尚未连接')
        if self._owner_required and not _is_open(self.owner):
            raise StorageError('PG 执行所有权连接已断开，请重启 Agent Server')
        try:
            async with asyncio.timeout(TIMEOUT):
                async with self.pool.acquire(timeout=TIMEOUT) as db:
                    yield db
        except (KeyError, ValueError, StorageError):
            raise
        except Exception:
            raise StorageError('PG 读写失败') from None

    async def close(self):
        pool, self.pool = self.pool, None
        if pool is None:
            return
        try:
            async with asyncio.timeout(TIMEOUT):
                if self.owner is not None:
                    owner, self.owner = self.owner, None
                    if _is_open(owner):
                        await owner.execute('SELECT pg_advisory_unlock(hashtextextended($1,0))', self._owner_key)
                    await pool.release(owner, timeout=TIMEOUT)
                await pool.close()
        except Exception:
            pool.terminate()
            raise StorageError('PG 关闭失败') from None

    async def claim_owner(self):
        if self.owner is not None:
            if not _is_open(self.owner):
                raise StorageError('PG 执行所有权连接已断开，请重启 Agent Server')
            return
        if self.pool is None:
            raise StorageError('PG 尚未连接')
        db = None
        try:
            async with asyncio.timeout(TIMEOUT):
                db = await self.pool.acquire(timeout=TIMEOUT)
                acquired = await db.fetchval(
                    'SELECT pg_try_advisory_lock(hashtextextended($1,0))', self._owner_key,
                )
                if not acquired:
                    await self.pool.release(db, timeout=TIMEOUT)
                    db = None
                    raise StorageError('该 PG schema 已有运行中的 Agent Server')
                self.owner = db
                self._owner_required = True
        except StorageError:
            raise
        except Exception:
            if db is not None:
                await self.pool.release(db, timeout=TIMEOUT)
            raise StorageError('PG 执行所有权获取失败') from None

    async def ping(self) -> bool:
        async with self._connection() as db:
            return await db.fetchval('SELECT 1') == 1

    async def create_thread(self) -> str:
        thread_id = str(uuid4())
        async with self._connection() as db:
            await db.execute(f'INSERT INTO {self._threads}(id) VALUES($1)', thread_id)
        return thread_id

    async def thread_exists(self, thread_id: str) -> bool:
        async with self._connection() as db:
            return await db.fetchval(f'SELECT EXISTS(SELECT 1 FROM {self._threads} WHERE id=$1)', thread_id)

    async def admit(self, thread_id: str, command: ChatCommand) -> TaskAdmission:
        request_id = getattr(command, 'client_request_id', None)
        async with self._connection() as db, db.transaction():
            generation = await db.fetchval(f'SELECT generation FROM {self._threads} WHERE id=$1 FOR UPDATE', thread_id)
            if generation is None:
                raise KeyError('会话不存在')
            if request_id is not None:
                row = await db.fetchrow(f'SELECT * FROM {self._tasks} WHERE thread_id=$1 AND client_request_id=$2', thread_id, request_id)
                if row is not None:
                    return TaskAdmission(_record(row), created=False)
            active = await db.fetchval(f"SELECT EXISTS(SELECT 1 FROM {self._tasks} WHERE thread_id=$1 AND status IN ('submitted','running'))", thread_id)
            if active:
                raise ValueError('该会话已有运行中的任务')
            generation += 1
            await db.execute(f'UPDATE {self._threads} SET generation=$2 WHERE id=$1', thread_id, generation)
            row = await db.fetchrow(f'''
                INSERT INTO {self._tasks}(id,thread_id,client_request_id,generation,command_json,status)
                VALUES($1,$2,$3,$4,$5::jsonb,'submitted') RETURNING *
            ''', str(uuid4()), thread_id, request_id, generation, command.model_dump_json())
            return TaskAdmission(_record(row), created=True)

    async def get(self, task_id: str) -> TaskRecord:
        async with self._connection() as db:
            row = await db.fetchrow(f'SELECT * FROM {self._tasks} WHERE id=$1', task_id)
            if row is None:
                raise KeyError('任务不存在')
            return _record(row)

    async def list_tasks(self, thread_id: str) -> list[TaskRecord]:
        async with self._connection() as db:
            rows = await db.fetch(f'SELECT * FROM {self._tasks} WHERE thread_id=$1 ORDER BY generation', thread_id)
            return [_record(row) for row in rows]

    async def set_running(self, task_id: str) -> TaskRecord:
        async with self._connection() as db, db.transaction():
            row = await db.fetchrow(f"UPDATE {self._tasks} SET status='running' WHERE id=$1 AND status IN ('submitted','running') RETURNING *", task_id)
            if row is None:
                row = await db.fetchrow(f'SELECT * FROM {self._tasks} WHERE id=$1', task_id)
            if row is None:
                raise KeyError('任务不存在')
            return _record(row)

    async def save(self, task: TaskRecord) -> TaskRecord:
        if task.status not in STATUSES or task.seq < 0:
            raise ValueError('任务状态或 seq 不合法')
        async with self._connection() as db, db.transaction():
            row = await db.fetchrow(f'SELECT * FROM {self._tasks} WHERE id=$1 FOR UPDATE', task.id)
            if row is None:
                raise KeyError('任务不存在')
            existing = _record(row)
            if (task.thread_id, task.generation) != (existing.thread_id, existing.generation):
                raise ValueError('任务身份或 generation 不匹配')
            if existing.status in TERMINAL or task.seq < existing.seq:
                return existing
            status = 'running' if existing.status == 'running' and task.status == 'submitted' else task.status
            finished = None
            if status in TERMINAL:
                finished = datetime.fromisoformat(task.finished_at) if task.finished_at else datetime.now(timezone.utc)
                if finished.tzinfo is None:
                    finished = finished.replace(tzinfo=timezone.utc)
            projection = _result(task)
            for event in projection['events_v2']:
                event['task_status'] = status
            saved = await db.fetchrow(f'''
                UPDATE {self._tasks} SET result_json=$2::jsonb,seq=$3,status=$4,error=$5,finished_at=$6
                WHERE id=$1 RETURNING *
            ''', task.id, json.dumps(projection, ensure_ascii=False), task.seq, status, task.error, finished)
            return _record(saved)

    async def unfinished(self) -> list[TaskRecord]:
        async with self._connection() as db:
            rows = await db.fetch(f"SELECT * FROM {self._tasks} WHERE status IN ('submitted','running') ORDER BY created_at,id")
            return [_record(row) for row in rows]

    async def history(self, thread_id: str, before_task: str) -> list[ChatMessage]:
        messages = []
        for task in await self.list_tasks(thread_id):
            if task.id == before_task:
                break
            if task.status != 'complete':
                continue
            values = sorted(task.snapshots.values(), key=lambda value: value.get('index', 0))
            answers = [value['meta_data'].get('content', '') for value in values
                       if value.get('type') == 'markdown' and value.get('sender') == 'assistant'
                       and value.get('status') == 'success']
            if answers:
                messages.extend([ChatMessage(role='user', content=task.command.question),
                                 ChatMessage(role='assistant', content='\n'.join(answers))])
        return messages[-12:]
