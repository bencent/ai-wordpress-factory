"""Workspace-scoped application facade and public projections; no network execution."""
import base64,json
from datetime import datetime,timezone
from uuid import UUID
from domain.submission import ValidationError,TaskNotFound
from domain.contracts import Status
from domain.failures import SAFE_RUN_ERROR_CODES
from service.query import ScopedQueryService
from service.submission import ScopedTaskSubmissionService
from service.workspace_bootstrap import default_workspace_context

class RetryConflict(ValueError): pass

def cursor_encode(value):
    return base64.urlsafe_b64encode(json.dumps({'v':1,'position':value},separators=(',',':')).encode()).decode().rstrip('=') if value else None

def cursor_decode(value):
    if value is None: return None
    try:
        if type(value) is not str or len(value)>512: raise ValueError()
        raw=base64.b64decode(value+'='*(-len(value)%4),altchars=b'-_',validate=True)
        data=json.loads(raw)
        if set(data)!={'v','position'} or type(data['v']) is not int or data['v']!=1: raise ValueError()
        pos=data['position']
        if type(pos) is not list or len(pos)!=2 or any(type(x) is not str for x in pos): raise ValueError()
        if datetime.fromisoformat(pos[0]).tzinfo is None: raise ValueError()
        UUID(pos[1])
        return tuple(pos)
    except (ValueError,TypeError,KeyError,UnicodeError):
        raise ValidationError('cursor') from None

def task_view(task,run=None):
    result={key:getattr(task,key) for key in ('task_id','site_id','topic','content_type','status',
        'created_at','updated_at','current_run_id','latest_content_version_id')}
    # Internal request/profile/checkpoint snapshots and model prompts never cross this boundary.
    if run is not None:
        error=run.error or {}
        code=error.get('code')
        result['current_run']={'run_id':run.run_id,'attempt':run.attempt,'status':run.status,
            'created_at':run.created_at,'started_at':run.started_at,'finished_at':run.finished_at,
            'error_code':code if type(code) is str and code in SAFE_RUN_ERROR_CODES else None}
    return result

def event_view(event):
    from service.checkpoints import STAGES,EVENTS
    known={'TASK_CREATED','TASK_RETRY_REQUESTED','CONTENT_VERSION_CREATED','RUN_COMPLETED','TASK_AWAITING_APPROVAL',
        'RUN_FAILED','WORKER_LOST'}|{'FACTORY_'+e.upper() for e in EVENTS}
    kind=event.type if event.type in known else 'TASK_UPDATED'
    metadata={}
    data=event.metadata
    for key in ('stage','agent'):
        val=data.get(key)
        if type(val) is str and val in STAGES: metadata[key]=val
    code=data.get('error_code')
    if type(code) is str and code in SAFE_RUN_ERROR_CODES: metadata['error_code']=code
    if type(event.attempt) is int: metadata['attempt']=event.attempt
    return {'event_id':event.event_id,'task_id':event.task_id,'run_id':event.run_id,
        'sequence_number':event.sequence_number,'type':kind,'status':event.status,'attempt':event.attempt,
        'summary':{'TASK_CREATED':'任務已建立','TASK_RETRY_REQUESTED':'任務已排入重試佇列'}.get(kind,'任務狀態已更新'),
        'metadata':metadata,'created_at':event.created_at}

class TaskHTTPService:
    def __init__(self,store,resolver,*,context_provider=default_workspace_context,clock=None):
        self.store,self.resolver,self.context_provider=store,resolver,context_provider
        self.clock=clock or (lambda:datetime.now(timezone.utc))
    def context(self): return self.context_provider(self.store)
    def create(self,key,body):
        result=ScopedTaskSubmissionService(self.store,self.context(),self.resolver).submit(key,body)
        return task_view(result.task),result.created
    def get(self,task_id):
        query=ScopedQueryService(self.store,self.context())
        task=query.get_task(task_id)
        run=query.get_run(task_id,task.current_run_id) if task.current_run_id else None
        return task_view(task,run)
    def list(self,limit=50,cursor=None):
        page=ScopedQueryService(self.store,self.context()).recent_tasks(limit=limit,cursor=cursor_decode(cursor))
        return {'tasks':[task_view(t) for t in page.tasks],'next_cursor':cursor_encode(page.next_cursor)}
    def events(self,task_id,after_sequence=0):
        if type(after_sequence) is not int or after_sequence<0 or after_sequence>9223372036854775807:
            raise ValidationError('after_sequence')
        with self.store.workspace_reader(self.context().workspace_id) as repo:
            if repo.get_task(task_id) is None: raise TaskNotFound()
            events=repo.public_events(task_id,after_sequence)
        return {'events':[event_view(e) for e in events],
            'last_sequence':events[-1].sequence_number if events else after_sequence}
    def retry(self,task_id):
        with self.store.workspace_transaction(self.context().workspace_id) as repo:
            task=repo.get_task(task_id)
            if task is None: raise TaskNotFound()
            if task.status not in (Status.FAILED,Status.WORKER_LOST): raise RetryConflict()
            updated=repo.retry_task(task_id,task.current_run_id,task.status,self.clock().isoformat())
            if updated is None: raise RetryConflict()
            return task_view(updated)
    def status(self):
        from persistence.connection import PersistenceError
        from domain.submission import ProfileError
        try:
            with self.store.workspace_reader(self.context().workspace_id) as repo:
                versions,heartbeat=repo.health_observation()
            state='UNKNOWN'
            if heartbeat:
                age=(self.clock()-datetime.fromisoformat(heartbeat)).total_seconds()
                state='ONLINE' if 0<=age<60 else 'OFFLINE'
            return {'api':'OK','database':'OK','schema_version':max(versions) if versions else None,
                'worker':{'status':state,'heartbeat_at':heartbeat,'source':'active_run_heartbeat'}}
        except (PersistenceError,ProfileError):
            return {'api':'OK','database':'UNAVAILABLE','worker':{'status':'UNKNOWN','heartbeat_at':None,'source':'active_run_heartbeat'}}
