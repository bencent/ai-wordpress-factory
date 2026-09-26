"""Persist real workflow observations and validated content; never publish."""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4
from domain.contracts import ContentVersion
from domain.execution import LeaseLost
from service.checkpoints import checkpoint


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


class PersistingObserver:
    def __init__(self, store, lease, cancelled, images):
        self.store, self.lease, self.cancelled, self.images = store, lease, cancelled, images
        self.error = None

    def snapshot(self, value):
        return checkpoint(value,task_id=self.lease.task_id,workspace_id=self.lease.workspace_id,
                          run_id=self.lease.run_id,images=self.images)

    def on_event(self, event):
        try:
            if self.cancelled.is_set():
                raise LeaseLost()
            with self.store.transaction() as repo:
                accepted = repo.record_workflow_event(self.lease, event, self.snapshot(event.snapshot), now())
            if not accepted:
                raise LeaseLost()
        except Exception as exc:
            self.error = exc
            raise


def build_version(task, legacy, image_data):
    gates = {
        'quality': legacy.quality_result,
        'security': legacy.frontend_security_result,
        'validation': legacy.frontend_validation_result,
        'production_quality': legacy.frontend_production_quality_result,
        'rendered_technical': legacy.rendered_technical_result,
    }
    if any(not isinstance(v, dict) or v.get('passed') is not True for v in gates.values()):
        raise ValueError('Required validation did not pass')
    conversion = legacy.frontend_conversion_result or {}
    content = conversion.get('blocks')
    if conversion.get('success') is not True or not isinstance(content, str) or not content.strip():
        raise ValueError('Converted content is missing')
    visual = legacy.visual_quality_result or {}
    if visual.get('action') not in ('PASS', 'WARN', 'HUMAN_REVIEW', 'pass', 'warn', 'human_review'):
        raise ValueError('Visual review is incomplete')
    gates['visual_quality'] = visual
    created = now()
    return ContentVersion(content_version_id=str(uuid4()), task_id=task.task_id,
        run_id=task.current_run_id, version_number=1, content_type=task.content_type,
        title=legacy.title, content=content, validation_result=gates,
        image_data=image_data, seo_metadata={'title': legacy.seo_title,
        'description': legacy.seo_description, 'keywords': legacy.seo_keywords},
        optimization_report={'quality_evaluator': legacy.quality_result},
        taxonomy={'category_ids': task.request_snapshot.get('category_ids', []),
                  'tag_ids': task.request_snapshot.get('tag_ids', [])},
        suggested_slug=task.request_snapshot.get('selected_slug'),
        created_at=created, updated_at=created)
