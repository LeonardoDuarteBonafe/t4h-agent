from contextlib import asynccontextmanager
from pathlib import Path
import asyncio
import json

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings, ROOT
from .runtime import Runtime

settings = Settings()
runtime = Runtime(settings)


@asynccontextmanager
async def lifespan(app):
    if settings.auto_start:
        try:
            await runtime.start()
        except Exception as exc:
            runtime.error = str(exc)
    yield
    await runtime.stop()


app = FastAPI(title='Triagem N1', lifespan=lifespan)
web = Path(__file__).parent / 'web'
app.mount('/static', StaticFiles(directory=web), name='static')


@app.middleware('http')
async def same_origin(request: Request, call_next):
    origin = request.headers.get('origin')
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and origin and origin not in (
        f'http://localhost:{settings.port}', f'http://127.0.0.1:{settings.port}'):
        return JSONResponse({'detail': 'Origem não autorizada para operar o painel local.'}, status_code=403)
    return await call_next(request)


@app.exception_handler(ValueError)
async def value_error(request, exc):
    return JSONResponse({'detail': str(exc)}, status_code=409)


@app.get('/')
async def index():
    return FileResponse(web / 'index.html')


@app.get('/health')
async def health():
    return {'status': 'ok', 'model': 'gpt-4o-mini', 'configured': bool(settings.api_key),
            'running': runtime.running, 'world_connected': runtime.world_connected,
            'run_id': settings.run_id, 'identity_mode': 'identificação declarada no protótipo local'}


@app.get('/api/state')
async def state():
    return runtime.snapshot()


@app.get('/api/cases/{case_id}')
async def case_detail(case_id: str):
    case = runtime.store.get_case(case_id)
    if not case:
        raise HTTPException(404, 'Caso não encontrado.')
    return {'case': case, 'traces': runtime.store.traces(case_id),
            'decisions': runtime.store.decisions(case_id),
            'operations': runtime.store.list_operations(case_id)}


class Intake(BaseModel):
    de: str = Field(min_length=3, max_length=200)
    nome: str = Field(default='', max_length=200)
    assunto: str = Field(min_length=2, max_length=300)
    corpo: str = Field(min_length=3, max_length=5000)


@app.post('/api/intake')
async def intake(data: Intake):
    try:
        result = await runtime.world.post_event(data.model_dump())
    except Exception as exc:
        raise HTTPException(503, str(exc)) from None
    return result


@app.post('/api/runtime/start')
async def start():
    return await runtime.start()


@app.post('/api/runtime/stop')
async def stop():
    return await runtime.stop()


class Decision(BaseModel):
    actor: str = Field(min_length=2, max_length=100)
    decision: str
    version: str
    note: str = Field(default='', max_length=1500)


@app.post('/api/cases/{case_id}/decision')
async def decision(case_id: str, data: Decision):
    return await runtime.decide(case_id, data.version, data.actor, data.decision, data.note)


@app.post('/api/cases/{case_id}/retry')
async def retry(case_id: str):
    case = runtime.store.get_case(case_id)
    if not case:
        raise HTTPException(404, 'Caso não encontrado.')
    if case['status'] != 'erro':
        raise HTTPException(409, 'Somente falha de análise pode ser retomada automaticamente. Operação ambígua requer conferência.')
    if not runtime.running:
        raise HTTPException(409, 'Inicie a triagem antes de retomar.')
    runtime.enqueue(case_id)
    return {'status': 'agendado'}


@app.get('/api/evidence/{filename}')
async def evidence(filename: str):
    folder = (settings.data_dir / 'evidencias').resolve()
    target = (folder / filename).resolve()
    if target.parent != folder or not target.is_file():
        raise HTTPException(404, 'Evidência não encontrada.')
    return FileResponse(target)


@app.get('/api/export')
async def export():
    return JSONResponse({'run_id': settings.run_id, 'state': runtime.snapshot(),
                         'operations': runtime.store.list_operations()},
                        headers={'Content-Disposition': 'attachment; filename=rodada-triagem.json'})
