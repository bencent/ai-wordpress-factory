"""Allowlisted execution failures; exception messages never select a code."""
from domain.ai_runtime import ErrorCode


class UnsafeApprovalPolicyError(ValueError):
    code = 'UNSAFE_APPROVAL_POLICY'
    safe_summary = 'Phase 8.1 requires human review and does not permit automatic publishing.'

    def __init__(self):
        super().__init__(self.safe_summary)


SAFE_RUN_ERROR_CODES = frozenset({c.value for c in ErrorCode} | {
    'UNSAFE_APPROVAL_POLICY','AI_INVOCATION_PERSISTENCE_FAILED','EXECUTOR_FAILED',
    'EXECUTOR_INCOMPLETE','EXECUTOR_CANCELLED','FACTORY_VALIDATION_FAILED'})


def require_human_policy(policy):
    from contracts import ApprovalPolicy, ApprovalPolicyMode
    if isinstance(policy,ApprovalPolicy): policy=policy.mode
    if type(policy) is dict: policy=policy.get('mode')
    if isinstance(policy,ApprovalPolicyMode): policy=policy.value
    if policy not in ('REQUIRE_HUMAN_REVIEW','require_human_review'):
        raise UnsafeApprovalPolicyError()


def check_policy_sources(value):
    """Inspect explicit policy fields only, without rendering config/profile data."""
    from dataclasses import is_dataclass
    seen=set()
    def visit(item):
        if id(item) in seen: return
        if type(item) is dict or is_dataclass(item):
            seen.add(id(item))
            data=item if type(item) is dict else vars(item)
            for key,child in data.items():
                if key in ('approval_policy','approval_policy_snapshot','approval_mode') and child is not None:
                    require_human_policy(child)
                elif key=='mode' and child in ('AUTO_PUBLISH','auto_publish'):
                    raise UnsafeApprovalPolicyError()
                elif type(child) in (dict,list) or is_dataclass(child): visit(child)
        elif type(item) is list:
            seen.add(id(item))
            for child in item: visit(child)
    visit(value)
