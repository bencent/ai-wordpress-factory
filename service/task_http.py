"""Workspace-scoped application facade and public projections; no network execution."""
import base64,json,os
from datetime import datetime,timezone
from pathlib import Path
from uuid import UUID
from domain.submission import ValidationError,TaskNotFound,SubmissionProfile,ProfileError,PreviewNotFound,PreviewAssetUnavailable,ApprovalConflict
from domain.contracts import Status
from domain.failures import SAFE_RUN_ERROR_CODES
from domain.preview import PreviewAssetKind
from service.query import ScopedQueryService
from service.submission import ScopedTaskSubmissionService
from service.workspace_bootstrap import default_workspace_context
from service.preview_artifact import deliver_preview_asset

UI_CONTENT_TYPES=['POST','PAGE']
UI_PAGE_PURPOSES=['ABOUT','SERVICE','EVENT','OTHER']
UI_LIMITS={'topic_min':3,'topic_max':150,'brief_min':20,'brief_max':5000}

class RetryConflict(ValueError): pass
class RetryIdempotencyConflict(ValueError): pass
class RevisionConflict(ValueError): pass
class RevisionIdempotencyConflict(ValueError): pass
class PublishConflict(ValueError): pass
class PublishIdempotencyConflict(ValueError): pass
class PublishTargetUnavailable(ValueError): pass

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
        'RUN_FAILED','WORKER_LOST','TASK_APPROVED'}|{'FACTORY_'+e.upper() for e in EVENTS}
    kind=event.type if event.type in known else 'TASK_UPDATED'
    metadata={}
    data=event.metadata
    for key in ('stage','agent','content_version_id'):
        val=data.get(key)
        if type(val) is str: metadata[key]=val
    code=data.get('error_code')
    if type(code) is str and code in SAFE_RUN_ERROR_CODES: metadata['error_code']=code
    if type(event.attempt) is int: metadata['attempt']=event.attempt
    return {'event_id':event.event_id,'task_id':event.task_id,'run_id':event.run_id,
        'sequence_number':event.sequence_number,'type':kind,'status':event.status,'attempt':event.attempt,
        'actor':event.actor,
        'summary':{'TASK_CREATED':'任務已建立','TASK_RETRY_REQUESTED':'任務已排入重試佇列','TASK_APPROVED':'內容已通過審核'}.get(kind,'任務狀態已更新'),
        'metadata':metadata,'created_at':event.created_at}

def preview_view(stored):
    record=stored.record
    assets=[]
    for a in stored.assets:
        assets.append({'kind':a.kind.value,'artifact_key':a.artifact_key,'media_type':a.media_type.value,
            'width':a.width,'height':a.height,'byte_size':a.byte_size,'sha256':a.sha256})
    return {'preview_id':record.preview_id,'task_id':record.task_id,'run_id':record.run_id,
        'content_version_id':record.content_version_id,'created_at':record.created_at,'assets':assets}

def publication_view(request):
    # Remote columns stay null until a future executor records a confirmed outcome.
    # No workspace, key, or run lineage is exposed to the caller.
    return {'publication_id':request.publication_id,'task_id':request.task_id,
        'content_version_id':request.content_version_id,'content_type':request.content_type.value,
        'state':request.state.value,'remote_resource_id':request.remote_resource_id,
        'remote_url':request.remote_url,'created_at':request.created_at,'updated_at':request.updated_at}

class TaskHTTPService:
    def __init__(self,store,resolver,*,context_provider=default_workspace_context,clock=None,preview_base_dir="artifacts/previews"):
        self.store,self.resolver,self.context_provider=store,resolver,context_provider
        self.clock=clock or (lambda:datetime.now(timezone.utc))
        self.preview_base_dir=preview_base_dir

    def bootstrap(self):
        context=self.context_provider(self.store)
        path=os.environ.get('AIWF_PROFILES_FILE')
        if not path: raise ProfileError()
        try:
            records=json.loads(Path(path).read_text(encoding='utf-8'))
        except (OSError,UnicodeError,json.JSONDecodeError,TypeError): raise ProfileError()
        if type(records) is not list: raise ProfileError()
        for row in records:
            if type(row) is not dict or row.get('workspace_id')!=context.workspace_id: continue
            site_id=row.get('site_id');brand_profile_id=row.get('brand_profile_id')
            if type(site_id) is not str or not site_id or type(brand_profile_id) is not str or not brand_profile_id: continue
            try:
                profile=self.resolver(context,site_id,brand_profile_id)
            except (ProfileError,ValueError,TypeError): continue
            if type(profile) is not SubmissionProfile or profile.workspace_id!=context.workspace_id: continue
            if profile.site_id!=site_id or profile.brand_profile_id!=brand_profile_id: continue
            return {
                'defaults':{'site_id':site_id,'brand_profile_id':brand_profile_id},
                'content_types':list(UI_CONTENT_TYPES),
                'page_purposes':list(UI_PAGE_PURPOSES),
                'limits':dict(UI_LIMITS),
            }
        raise ProfileError()
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

    def get_preview(self,task_id):
        query=ScopedQueryService(self.store,self.context())
        task=query.get_task(task_id)
        if task.latest_content_version_id is None:
            raise PreviewNotFound()
        stored=query.get_preview_by_content_version(task.latest_content_version_id)
        return preview_view(stored)

    def get_preview_asset(self,task_id: str, kind: str) -> bytes:
        query=ScopedQueryService(self.store,self.context())
        task=query.get_task(task_id)
        if task.latest_content_version_id is None:
            raise PreviewNotFound()
        stored=query.get_preview_by_content_version(task.latest_content_version_id)
        try:
            asset_kind = PreviewAssetKind(kind)
        except ValueError:
            raise ValidationError('kind')
        return deliver_preview_asset(stored, asset_kind, self.preview_base_dir)
    def retry(self,task_id,idempotency_key):
        if (type(idempotency_key) is not str or not 1 <= len(idempotency_key) <= 200
                or not idempotency_key.strip()):
            raise ValidationError('idempotency_key')
        with self.store.workspace_transaction(self.context().workspace_id) as repo:
            existing=repo.find_retry_request(idempotency_key)
            if existing is not None:
                if existing[0]!=task_id: raise RetryIdempotencyConflict()
                task=repo.get_task(task_id)
                if task is None: raise TaskNotFound()
                return task_view(task)
            task=repo.get_task(task_id)
            if task is None: raise TaskNotFound()
            if task.status not in (Status.FAILED,Status.WORKER_LOST): raise RetryConflict()
            updated=repo.retry_task(task_id,task.current_run_id,task.status,idempotency_key,self.clock().isoformat())
            if updated is None: raise RetryConflict()
            return task_view(updated)

    def request_revision(self, task_id, content_version_id, feedback, idempotency_key):
        if (type(idempotency_key) is not str or not 1 <= len(idempotency_key) <= 200
                or not idempotency_key.strip()):
            raise ValidationError('idempotency_key')
        if type(content_version_id) is not str or not content_version_id.strip():
            raise ValidationError('content_version_id')
        if type(feedback) is not str:
            raise ValidationError('feedback')
        normalized = feedback.strip()
        if not normalized:
            raise ValidationError('feedback')
        if len(normalized) > 10000:
            raise ValidationError('feedback')
        with self.store.workspace_transaction(self.context().workspace_id) as repo:
            existing = repo.find_revision_request(idempotency_key)
            if existing is not None:
                if existing[0] != task_id or existing[1] != content_version_id or existing[3] != normalized:
                    raise RevisionIdempotencyConflict()
                task = repo.get_task(task_id)
                if task is None:
                    raise TaskNotFound()
                return task_view(task)

            task = repo.get_task(task_id)
            if task is None:
                raise TaskNotFound()

            result = repo.request_revision(task_id, content_version_id, normalized, idempotency_key, self.clock().isoformat())
            if result is None:
                raise RevisionConflict()
            task, _ = result
            return task_view(task)
    def approve(self, task_id, content_version_id, idempotency_key):
        if (type(idempotency_key) is not str or not 1 <= len(idempotency_key) <= 200
                or not idempotency_key.strip()):
            raise ValidationError('idempotency_key')
        if type(content_version_id) is not str or not content_version_id.strip():
            raise ValidationError('content_version_id')
        with self.store.workspace_transaction(self.context().workspace_id) as repo:
            # Check for existing idempotency key
            existing = repo.find_approval_request(idempotency_key)
            if existing is not None:
                if existing[0] != task_id or existing[1] != content_version_id:
                    raise ValidationError('idempotency_key')
                # Replay: return current task state
                task = repo.get_task(task_id)
                if task is None:
                    raise TaskNotFound()
                return task_view(task)

            task = repo.get_task(task_id)
            if task is None:
                raise TaskNotFound()

            success, error_code = repo.approve_content_version(task_id, content_version_id, self.clock().isoformat())
            if not success:
                if error_code == 'TASK_NOT_FOUND':
                    raise TaskNotFound()
                elif error_code == 'WRONG_TASK_STATE':
                    raise ApprovalConflict('Task cannot be approved in its current state')
                elif error_code == 'VERSION_MISMATCH':
                    raise ApprovalConflict('Content version does not match the task')
                elif error_code == 'VERSION_NOT_FOUND':
                    raise ApprovalConflict('Content version not found')
                elif error_code == 'PREVIEW_NOT_FOUND':
                    raise ApprovalConflict('Preview not found for this content version')
                else:
                    raise ApprovalConflict('Approval failed')

            # Record idempotency key
            repo.record_approval_request(task_id, idempotency_key, content_version_id, self.clock().isoformat())

            updated = repo.get_task(task_id)
            return task_view(updated)
    def request_publish(self, task_id, content_version_id, idempotency_key):
        """Record an explicit intent to publish the approved ContentVersion.

        This is a durable local record only. It performs no network I/O, does not
        start a TaskRun, and never advances the publication past PENDING. The
        repository remains the sole authority on which version is approved.
        """
        if (type(idempotency_key) is not str or not 1 <= len(idempotency_key) <= 200
                or not idempotency_key.strip()):
            raise ValidationError('idempotency_key')
        if type(content_version_id) is not str:
            raise ValidationError('content_version_id')
        normalized = content_version_id.strip()
        if not normalized:
            raise ValidationError('content_version_id')
        with self.store.workspace_transaction(self.context().workspace_id) as repo:
            existing = repo.find_publication(idempotency_key)
            if existing is not None:
                if existing[0] != task_id or existing[1] != normalized:
                    raise PublishIdempotencyConflict()
                request = repo.get_publication(existing[2])
                if request is None:
                    raise PublishConflict('Publication request is unavailable')
                return publication_view(request)

            request, error_code = repo.request_publication(
                task_id, normalized, idempotency_key, self.clock().isoformat())
            if request is None:
                if error_code == 'TASK_NOT_FOUND':
                    raise TaskNotFound()
                # A missing publishing destination is a workspace configuration
                # problem, not a problem with this task or this content version.
                # Reporting it as "cannot be published" would send an operator
                # looking at the wrong thing entirely.
                if error_code == 'NO_ACTIVE_PUBLISHING_TARGET':
                    raise PublishTargetUnavailable()
                # A version the caller cannot see is reported exactly like a version
                # that simply is not the approved one, so existence never leaks.
                raise PublishConflict('Content version cannot be published')
            return publication_view(request)
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