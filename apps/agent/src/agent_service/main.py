"""Mossail Agent Server：用普通对象组合在线执行、记录和传输。"""
from __future__ import annotations

import logging
import os
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .controllers import http, websocket
from .agents.registry import model_resources
from .repositories.postgres import PgStore
from .repositories.redis import RedisBus
from .services.records import RecordService
from .services.tasks import TaskService

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s %(message)s')


def required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f'缺少配置 {name}；请使用 scripts/dev.py 启动，不回退 SQLite')
    return value


@asynccontextmanager
async def lifespan(app):
    store = PgStore(required('MOSSAIL_DATABASE_URL'), os.getenv('MOSSAIL_DATABASE_SCHEMA', 'mossail'))
    bus = RedisBus(required('MOSSAIL_REDIS_URL'), os.getenv('MOSSAIL_REDIS_NAMESPACE', 'mossail:dev'))
    async with model_resources() as agents, AsyncExitStack() as resources:
        await store.open()
        resources.push_async_callback(store.close)
        await store.claim_owner()
        await bus.open()
        resources.push_async_callback(bus.close)
        records = RecordService(store, bus)
        await records.recover()
        service = TaskService(store, records, agents.factories, agents.model,
                              timeout=float(os.getenv('MOSSAIL_TASK_TIMEOUT', '300')))
        resources.push_async_callback(service.close)
        app.state.store, app.state.bus = store, bus
        app.state.records, app.state.service = records, service
        app.state.agents = agents
        yield


app = FastAPI(title='Mossail Agent', lifespan=lifespan)
app.mount('/assets', StaticFiles(directory=http.WEB_ROOT), name='assets')
app.include_router(http.router)
app.include_router(websocket.router)
http.configure_error_handlers(app)
