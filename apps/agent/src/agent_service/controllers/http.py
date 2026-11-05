"""HTTP Controller：校验传输契约，调用用例，不直接操作存储。"""
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse
from open_protocol import ChatCommand

from ..errors import InvalidCommand, StorageError

router = APIRouter()
WEB_ROOT = Path(__file__).resolve().parents[5] / 'apps/web'


@router.get('/')
async def index():
    return FileResponse(WEB_ROOT / 'index.html')


@router.get('/health')
async def health(request: Request):
    app = request.app
    registry = app.state.agents
    agent = {'model_configured': bool(registry.factories), 'agents': list(registry.factories),
             'model': registry.model, 'research_configured': registry.research_configured,
             'execution': registry.execution}
    return {'service': 'mossail', 'mode': 'local-single-process', 'agent': agent,
            **await app.state.service.health()}


@router.post('/threads', status_code=201)
async def create_thread(request: Request):
    return await request.app.state.service.create_thread()


@router.post('/threads/{thread_id}/tasks', status_code=202)
async def submit(thread_id: str, command: ChatCommand, request: Request):
    return await request.app.state.service.submit(thread_id, command)


@router.get('/tasks/{task_id}')
async def get_task(task_id: str, request: Request):
    return await request.app.state.service.get(task_id)


@router.get('/threads/{thread_id}/history')
async def history(thread_id: str, request: Request):
    return await request.app.state.service.history(thread_id)


@router.post('/tasks/{task_id}/cancel')
async def cancel(task_id: str, request: Request):
    return await request.app.state.service.cancel(task_id)


@router.post('/tasks/{task_id}/retry-save')
async def retry_save(task_id: str, request: Request):
    return await request.app.state.service.retry_save(task_id)


async def missing(_: Request, __: KeyError):
    return JSONResponse(status_code=404, content={'detail': '对象不存在'})


async def invalid(_: Request, exc: InvalidCommand):
    return JSONResponse(status_code=exc.status_code, content={'detail': str(exc)})


async def conflict(_: Request, exc: ValueError):
    return JSONResponse(status_code=409, content={'detail': str(exc)})


async def unavailable(_: Request, exc: StorageError):
    return JSONResponse(status_code=503, content={'detail': str(exc)})


def configure_error_handlers(app):
    for error, handler in [(KeyError, missing), (InvalidCommand, invalid),
                           (ValueError, conflict), (StorageError, unavailable)]:
        app.add_exception_handler(error, handler)
