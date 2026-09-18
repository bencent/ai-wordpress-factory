"""Submission boundary: only user requirements, never runtime policy or credentials."""
from dataclasses import dataclass
from typing import Any, Protocol
from domain.contracts import Task


class ValidationError(ValueError):
    code = "INVALID_SUBMISSION"

    def __init__(self, field):
        self.field = field
        super().__init__(f"Invalid submission field: {field}")


class IdempotencyConflict(ValueError):
    code = "IDEMPOTENCY_CONFLICT"

    def __init__(self):
        super().__init__("Submission key already belongs to a different request")


class ProfileError(ValueError):
    code = "INVALID_PROFILE"


class TaskNotFound(LookupError):
    code = "TASK_NOT_FOUND"

    def __init__(self):
        super().__init__("Task not found")


@dataclass(frozen=True, kw_only=True)
class SubmissionProfile:
    """Trusted, credential-free projection supplied by the composition layer.

    This is not a Factory Config; resolvers must not return raw configuration.
    """
    site_id: str
    brand_profile_id: str
    client_profile_id: str | None
    snapshot: dict[str, Any]
    approval_mode: str = "REQUIRE_HUMAN_REVIEW"


class ProfileResolver(Protocol):
    def __call__(self, site_id: str, brand_profile_id: str) -> SubmissionProfile: ...


@dataclass(frozen=True)
class SubmissionResult:
    task: Task
    created: bool


def validate_submission(body):
    required = {'site_id', 'content_type', 'topic', 'brief', 'brand_profile_id'}
    optional = {'target_audience', 'page_purpose', 'category_ids', 'tag_ids', 'selected_slug'}
    if type(body) is not dict:
        raise ValidationError('body')
    if set(body) - required - optional:
        # Never include untrusted field names/values in a public error.
        raise ValidationError('unknown_field')
    for field in sorted(required):
        if field not in body:
            raise ValidationError(field)
    data = dict(body)
    for field in ('site_id', 'brand_profile_id'):
        if type(data[field]) is not str or not 1 <= len(data[field]) <= 200 or not data[field].strip():
            raise ValidationError(field)
    for field, minimum, maximum in (('topic', 3, 150), ('brief', 20, 5000)):
        value = data[field]
        if type(value) is not str or not minimum <= len(value) <= maximum or not value.strip():
            raise ValidationError(field)
    if type(data['content_type']) is not str or data['content_type'] not in ('POST', 'PAGE'):
        raise ValidationError('content_type')
    for field in ('target_audience', 'page_purpose', 'selected_slug'):
        data.setdefault(field, None)
        if data[field] is not None and (type(data[field]) is not str or not data[field].strip()):
            raise ValidationError(field)
    if data['content_type'] == 'POST':
        if data['target_audience'] is None:
            raise ValidationError('target_audience')
        if data['page_purpose'] is not None:
            raise ValidationError('page_purpose')
    else:
        if data['page_purpose'] not in ('ABOUT', 'SERVICE', 'EVENT', 'OTHER'):
            raise ValidationError('page_purpose')
        if data['target_audience'] is not None:
            raise ValidationError('target_audience')
    for field in ('category_ids', 'tag_ids'):
        data.setdefault(field, [])
        ids = data[field]
        if type(ids) is not list or any(type(i) is not int or i <= 0 for i in ids) or len(set(ids)) != len(ids):
            raise ValidationError(field)
        if data['content_type'] == 'PAGE' and ids:
            raise ValidationError(field)
        data[field] = list(ids)
    # Preserve text and list order exactly. Only omitted optional fields get defaults.
    return data
