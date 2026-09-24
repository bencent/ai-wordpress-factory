"""Atomic submission and permanent idempotency; no external work in DB transactions."""
from datetime import datetime, timezone
from uuid import uuid4

from domain.providers import Capability
from domain.workspace import WorkspaceContext
from domain.contracts import Task, TaskRun, TaskEvent, ContentType, Status
from domain.submission import (validate_submission, ValidationError, IdempotencyConflict,
                               SubmissionResult, SubmissionProfile, ProfileError, ProfileResolver)
from persistence.codec import encode_snapshot, decode_snapshot
from persistence.repository import Store


class ScopedTaskSubmissionService:
    def __init__(self, store: Store, context: WorkspaceContext, profile_resolver: ProfileResolver):
        if not isinstance(context, WorkspaceContext):
            raise TypeError("Trusted WorkspaceContext required")
        self.context = context
        self.store = store
        self.profile_resolver = profile_resolver

    @staticmethod
    def _replay(task, request):
        if task.request_snapshot != request:
            raise IdempotencyConflict()
        return SubmissionResult(task, created=False)

    def submit(self, submission_key, body):
        if type(submission_key) is not str or not 1 <= len(submission_key) <= 200 or not submission_key.strip():
            raise ValidationError('submission_key')
        request = validate_submission(body)
        # Replays survive profile changes/removal and do not create a new attempt.
        with self.store.workspace_reader(self.context.workspace_id) as repo:
            existing = repo.find_by_submission_key(submission_key)
            if existing is not None:
                return self._replay(existing, request)
        # Resolve outside the lock. Only a credential-free profile projection is accepted.
        try:
            profile = self.profile_resolver(self.context, request['site_id'], request['brand_profile_id'])
        except Exception:
            raise ProfileError() from None
        if (not isinstance(profile, SubmissionProfile)
                or profile.workspace_id != self.context.workspace_id
                or type(profile.provider_connection_id) is not str or not profile.provider_connection_id
                or profile.site_id != request['site_id']
                or profile.brand_profile_id != request['brand_profile_id']
                or profile.approval_mode != 'REQUIRE_HUMAN_REVIEW'
                or type(profile.snapshot) is not dict
                or (profile.client_profile_id is not None and (type(profile.client_profile_id) is not str or not profile.client_profile_id))):
            raise ProfileError('Profile or approval policy is not allowed')
        for key,id_field,expected in (
                ('client_profile','client_id',profile.client_profile_id),
                ('brand_profile','brand_id',profile.brand_profile_id),
                ('site','site_id',profile.site_id)):
            part = profile.snapshot.get(key)
            if part is not None:
                if (type(part) is not dict or (id_field in part and part[id_field] != expected)
                        or ('workspace_id' in part and part['workspace_id'] != self.context.workspace_id)):
                    raise ProfileError()
        brand_snapshot = decode_snapshot(encode_snapshot(profile.snapshot))
        with self.store.workspace_transaction(self.context.workspace_id) as repo:
            # Recheck after acquiring the write lock: another submitter may have won.
            existing = repo.find_by_submission_key(submission_key)
            if existing is not None:
                return self._replay(existing, request)
            if repo.workspace() is None:
                raise ProfileError()
            provider = repo.get_provider_connection(profile.provider_connection_id)
            if provider is None or Capability.TEXT not in provider.capabilities:
                raise ProfileError()
            now = datetime.now(timezone.utc).isoformat(timespec='microseconds')
            task_id, run_id, event_id = (str(uuid4()) for _ in range(3))
            task = Task(task_id=task_id, workspace_id=self.context.workspace_id, submission_key=submission_key,
                        site_id=request['site_id'], content_type=ContentType(request['content_type']),
                        topic=request['topic'], brief=request['brief'],
                        target_audience=request['target_audience'], page_purpose=request['page_purpose'],
                        brand_profile_id=request['brand_profile_id'], client_profile_id=profile.client_profile_id,
                        client_brand_snapshot=brand_snapshot, request_snapshot=request,
                        approval_policy_snapshot={'mode': 'REQUIRE_HUMAN_REVIEW'},
                        current_run_id=run_id, created_at=now, updated_at=now)
            run = TaskRun(run_id=run_id, task_id=task_id, attempt=1, created_at=now, updated_at=now,
                          provider_connection_id=provider.provider_connection_id,
                          provider_type=provider.provider_type, provider_mode=provider.provider_mode.value,
                          model=provider.default_model, provider_configuration_version=provider.configuration_version)
            event = TaskEvent(event_id=event_id, event_key='task-created:'+task_id,
                              task_id=task_id, run_id=run_id, attempt=1, sequence_number=1,
                              type='TASK_CREATED', actor='system', status=Status.QUEUED,
                              summary='任務已建立，等待執行', created_at=now)
            repo.add(task)
            repo.add(run)
            repo.add(event)
        return SubmissionResult(task, created=True)


class TaskSubmissionService:
    """Legacy single-workspace entry; new callers supply context and a scoped resolver."""
    def __init__(self, store, profile_resolver, *, context=None):
        if context is None:
            from .workspace_bootstrap import default_workspace_context, default_profile_resolver
            context = default_workspace_context(store)
            profile_resolver = default_profile_resolver(store,context,profile_resolver)
        self._scoped = ScopedTaskSubmissionService(store,context,profile_resolver)

    def submit(self, submission_key, body):
        return self._scoped.submit(submission_key,body)
