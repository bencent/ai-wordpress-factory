"""Atomic submission and permanent idempotency; no external work in DB transactions."""
from datetime import datetime, timezone
from uuid import uuid4

from domain.contracts import Task, TaskRun, TaskEvent, ContentType, Status
from domain.submission import (validate_submission, ValidationError, IdempotencyConflict,
                               SubmissionResult, SubmissionProfile, ProfileError, ProfileResolver)
from persistence.codec import encode_snapshot, decode_snapshot
from persistence.repository import Store


class TaskSubmissionService:
    def __init__(self, store: Store, profile_resolver: ProfileResolver):
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
        with self.store.reader() as repo:
            existing = repo.find_by_submission_key(submission_key)
            if existing is not None:
                return self._replay(existing, request)
        # Resolve outside the lock. Only a credential-free profile projection is accepted.
        profile = self.profile_resolver(request['site_id'], request['brand_profile_id'])
        if (not isinstance(profile, SubmissionProfile)
                or profile.site_id != request['site_id']
                or profile.brand_profile_id != request['brand_profile_id']
                or profile.approval_mode != 'REQUIRE_HUMAN_REVIEW'
                or type(profile.snapshot) is not dict
                or (profile.client_profile_id is not None and type(profile.client_profile_id) is not str)):
            raise ProfileError('Profile or approval policy is not allowed')
        brand_snapshot = decode_snapshot(encode_snapshot(profile.snapshot))
        with self.store.transaction() as repo:
            # Recheck after acquiring the write lock: another submitter may have won.
            existing = repo.find_by_submission_key(submission_key)
            if existing is not None:
                return self._replay(existing, request)
            now = datetime.now(timezone.utc).isoformat(timespec='microseconds')
            task_id, run_id, event_id = (str(uuid4()) for _ in range(3))
            task = Task(task_id=task_id, submission_key=submission_key,
                        site_id=request['site_id'], content_type=ContentType(request['content_type']),
                        topic=request['topic'], brief=request['brief'],
                        target_audience=request['target_audience'], page_purpose=request['page_purpose'],
                        brand_profile_id=request['brand_profile_id'], client_profile_id=profile.client_profile_id,
                        client_brand_snapshot=brand_snapshot, request_snapshot=request,
                        approval_policy_snapshot={'mode': 'REQUIRE_HUMAN_REVIEW'},
                        current_run_id=run_id, created_at=now, updated_at=now)
            run = TaskRun(run_id=run_id, task_id=task_id, attempt=1, created_at=now, updated_at=now)
            event = TaskEvent(event_id=event_id, event_key='task-created:'+task_id,
                              task_id=task_id, run_id=run_id, attempt=1, sequence_number=1,
                              type='TASK_CREATED', actor='system', status=Status.QUEUED,
                              summary='任務已建立，等待執行', created_at=now)
            repo.add(task)
            repo.add(run)
            repo.add(event)
        return SubmissionResult(task, created=True)
