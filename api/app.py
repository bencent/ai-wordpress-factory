"""HTTP transport only. Execution and persistence stay in application services."""
import logging
from pathlib import Path
from uuid import uuid4
from fastapi import FastAPI,Request,Query
from fastapi.responses import FileResponse,JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from fastapi.concurrency import run_in_threadpool
from domain.submission import ValidationError,IdempotencyConflict,ProfileError,TaskNotFound
from service.task_http import RetryConflict
from persistence.connection import PersistenceError
from service.http_bootstrap import build_http_service

BASE_DIR=Path(__file__).parents[1]
STATIC_DIR=BASE_DIR/'static'
INDEX_PATH=STATIC_DIR/'index.html'
SECURITY_HEADERS={
    b'content-security-policy':b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
    b'x-content-type-options':b'nosniff',
    b'referrer-policy':b'strict-origin-when-cross-origin',
    b'x-frame-options':b'DENY',
}

def error(request,status,code,message):
    return JSONResponse({'error':{'code':code,'message':message,'request_id':request.state.request_id}},status_code=status)

class RequestBoundary:
    def __init__(self,app): self.app=app
    async def __call__(self,scope,receive,send):
        if scope['type']!='http': return await self.app(scope,receive,send)
        scope.setdefault('state',{})['request_id']=str(uuid4())
        chunks=[];size=0
        while True:
            message=await receive()
            if message['type']=='http.disconnect': return
            size+=len(message.get('body',b''))
            if size>32768:
                response=error(Request(scope),413,'REQUEST_TOO_LARGE','Request body is too large.')
                return await response(scope,receive,send)
            chunks.append(message.get('body',b''))
            if not message.get('more_body',False): break
        sent=False
        async def buffered():
            nonlocal sent
            if sent: return await receive()
            sent=True
            return {'type':'http.request','body':b''.join(chunks),'more_body':False}
        started=False
        async def tracked_send(message):
            nonlocal started
            if message['type']=='http.response.start': started=True
            await send(message)
        try:
            await self.app(scope,buffered,tracked_send)
        except Exception:
            logging.getLogger('aiwf.api').error('Unexpected API failure request_id=%s',scope['state']['request_id'])
            if not started:
                response=error(Request(scope),500,'INTERNAL_ERROR','An unexpected error occurred.')
                await response(scope,receive,send)

class SecurityHeadersMiddleware:
    def __init__(self,app): self.app=app
    async def __call__(self,scope,receive,send):
        if scope['type']!='http': return await self.app(scope,receive,send)
        async def tracked_send(message):
            if message['type']=='http.response.start':
                headers=[(key,value) for key,value in message.get('headers',[]) if key.lower() not in SECURITY_HEADERS]
                message['headers']=headers+[(key,value) for key,value in SECURITY_HEADERS.items()]
            await send(message)
        await self.app(scope,receive,tracked_send)


def create_app(service=None):
    app=FastAPI(debug=False,docs_url=None,redoc_url=None,openapi_url=None)
    app.add_middleware(RequestBoundary)
    app.add_middleware(SecurityHeadersMiddleware)
    application=service if service is not None else build_http_service()
    mappings={ValidationError:(400,'VALIDATION_ERROR','Invalid request.'),
        IdempotencyConflict:(409,'IDEMPOTENCY_CONFLICT','Submission key conflicts with an existing request.'),
        ProfileError:(400,'VALIDATION_ERROR','Requested resources are unavailable.'),
        TaskNotFound:(404,'TASK_NOT_FOUND','Task not found.'),
        RetryConflict:(409,'RETRY_CONFLICT','Task cannot be retried in its current state.'),
        PersistenceError:(503,'SERVICE_UNAVAILABLE','Service is temporarily unavailable.')}
    async def domain_error(request,exc):
        mapping=next(v for k,v in mappings.items() if isinstance(exc,k))
        return error(request,*mapping)
    for cls in mappings: app.add_exception_handler(cls,domain_error)
    @app.exception_handler(RequestValidationError)
    async def invalid(request,exc): return error(request,400,'VALIDATION_ERROR','Invalid request.')
    @app.exception_handler(HTTPException)
    async def transport(request,exc): return error(request,exc.status_code,'HTTP_ERROR','Request could not be handled.')
    @app.exception_handler(Exception)
    async def unexpected(request,exc):
        logging.getLogger('aiwf.api').error('Unexpected API failure request_id=%s',request.state.request_id)
        return error(request,500,'INTERNAL_ERROR','An unexpected error occurred.')
    def boundary(request,allowed=()):
        if set(request.query_params)-set(allowed): raise ValidationError('query')
        if any(len(request.query_params.getlist(k))!=1 for k in request.query_params): raise ValidationError('query')
        if any('workspace' in k.lower() for k in request.headers): raise ValidationError('workspace')
    async def body(request):
        if request.headers.get('content-type','').split(';')[0].lower()!='application/json': raise ValidationError('content_type')
        try: return await request.json()
        except (ValueError,UnicodeError): raise ValidationError('body') from None
    app.mount("/static", StaticFiles(directory=STATIC_DIR,check_dir=False), name="static")
    @app.get("/")
    async def index(request:Request):
        if not INDEX_PATH.is_file(): return error(request,503,'SERVICE_UNAVAILABLE','Static UI is unavailable.')
        return FileResponse(INDEX_PATH,media_type='text/html')
    @app.post('/api/v1/tasks')
    async def create(request:Request):
        boundary(request)
        keys=request.headers.getlist('idempotency-key')
        if len(keys)!=1: raise ValidationError('idempotency_key')
        value=await body(request)
        result,created=await run_in_threadpool(application.create,keys[0],value)
        from fastapi.encoders import jsonable_encoder
        return JSONResponse(jsonable_encoder(result),status_code=201 if created else 200)
    @app.get('/api/v1/tasks')
    async def listing(request:Request,limit:int=Query(50,ge=1,le=100),cursor:str|None=None):
        boundary(request,('limit','cursor'))
        return await run_in_threadpool(application.list,limit,cursor)
    @app.get('/api/v1/tasks/{task_id}')
    async def get(request:Request,task_id:str):
        boundary(request)
        return await run_in_threadpool(application.get,task_id)
    @app.get('/api/v1/tasks/{task_id}/events')
    async def events(request:Request,task_id:str,after_sequence:int=Query(0,ge=0,le=9223372036854775807)):
        boundary(request,('after_sequence',))
        return await run_in_threadpool(application.events,task_id,after_sequence)
    @app.post('/api/v1/tasks/{task_id}/retry')
    async def retry(request:Request,task_id:str):
        boundary(request)
        raw=await request.body()
        if raw and await body(request)!={}: raise ValidationError('body')
        return await run_in_threadpool(application.retry,task_id)
    @app.get('/api/v1/system/status')
    async def status(request:Request):
        boundary(request)
        return await run_in_threadpool(application.status)

    @app.get('/api/v1/ui/bootstrap')
    async def bootstrap(request:Request):
        boundary(request)
        try:
            result = await run_in_threadpool(application.bootstrap)
            from fastapi.encoders import jsonable_encoder
            return JSONResponse(jsonable_encoder(result))
        except ProfileError:
            return error(request,503,'SERVICE_UNAVAILABLE','Requested resources are unavailable.')
    return app

app=create_app()
