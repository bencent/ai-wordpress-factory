# Phase 7C-4 Completion Report
## ClientProfile / Approval Policy Workflow Governance

**Date**: 2026-09-11  
**Status**: COMPLETE  
**Branch**: phase-7c-4-approval-policy

---

## Summary

Phase 7C-4 implements the ClientProfile / Approval Policy workflow governance system. This adds human-in-the-loop approval gates to the frontend pipeline while maintaining backward compatibility and safe defaults.

---

## Contracts Added/Modified

### `contracts.py`
| Addition | Type | Description |
|----------|------|-------------|
| `ApprovalPolicyMode` | Enum | `AUTO_PUBLISH`, `REQUIRE_HUMAN_REVIEW` |
| `ApprovalPolicy` | Dataclass | Embedded in Task; `mode`, optional `client_profile_id`, `created_at` |
| `ClientProfile` | Dataclass | Identity + approval_policy reference; NO raw credentials (Case B config) |
| Serialization | Methods | `to_dict`/`from_dict` for all three types |

---

## State Changes

### `state.py`
| Addition | Type | Description |
|----------|------|-------------|
| `TaskStatus.AWAITING_APPROVAL` | Enum | Workflow pauses awaiting human decision |
| `TaskStatus.REJECTED_NEEDS_REVISION` | Enum | Human rejected; task needs revision (no retry increment) |
| `Task.approval_policy` | Field | Embedded `ApprovalPolicy` (optional) |
| `Task.client_profile` | Field | Embedded `ClientProfile` (optional) |
| `Task.approval_status` | Field | `pending`/`approved`/`rejected` |
| `Task.approval_requested_at` | Field | ISO timestamp |
| `Task.approval_decided_at` | Field | ISO timestamp |
| `Task.approval_feedback` | Field | Human feedback text |
| `Task.approval_decision` | Field | Boolean decision |
| `Task.to_dict/from_dict` | Methods | Full round-trip serialization for all new fields |

---

## Core Logic (`main.py`)

### `_resolve_approval_policy(task)`
Priority resolution (highest first):
1. `task.approval_policy` (explicit task-level override)
2. `task.client_profile.approval_policy` (client default)
3. **Default**: `ApprovalPolicyMode.REQUIRE_HUMAN_REVIEW` (safe explicit default)

### `submit_approval_decision(task_id, approved, feedback)`
- Loads workflow state, finds task
- Validates task is in `AWAITING_APPROVAL`
- Records decision + feedback + timestamps
- Sets status: `APPROVED` → `IN_PROGRESS` (resume) / `REJECTED` → `REJECTED_NEEDS_REVISION`
- Persists state
- Returns `(success, message)`

### `_continue_post_approval(task_id, approved, feedback)`
- Called after approval decision
- If `approved`:
  1. Runs ImageAgent (generates images)
  2. Runs Publisher (publishes to WordPress)
  3. Sets status → `COMPLETED`
  4. Runs LearningAgent (logs success)
- If `rejected`:
  - Sets status → `REJECTED_NEEDS_REVISION` (NOT FrontendFailureFeedback, no retry increment)

### `run_workflow()` Policy Branch
After all 4 frontend gates PASS (Security → Converter → Validator → ProductionQuality):
```python
policy = _resolve_approval_policy(task)
if policy.mode == ApprovalPolicyMode.AUTO_PUBLISH:
    return _continue_post_approval(task_id, True, "auto-approved")
else:  # REQUIRE_HUMAN_REVIEW
    task.status = TaskStatus.AWAITING_APPROVAL
    task.approval_status = "pending"
    task.approval_requested_at = now()
    save_state()
    return False  # pause workflow
```

### `load_state()` Fix
Updated to update `workflow_state` in place (preserves module references used by tests):
```python
data = json.load(f)
workflow_state.__dict__.update(data.__dict__)
# + rehydrate tasks dict with Task.from_dict
```

---

## Test Coverage (`tests/test_frontend_pipeline_integration.py`)

| Test | Scenario |
|------|----------|
| `test_auto_publish_flow` | AUTO_PUBLISH continues through ImageAgent → Publisher → COMPLETED → LEARNING |
| `test_require_human_review_pauses_at_awaiting_approval` | REQUIRE_HUMAN_REVIEW pauses at AWAITING_APPROVAL, publisher not called |
| `test_interactive_eof_requires_human_review` | Legacy interactive mode simulation → AWAITING_APPROVAL |
| `test_non_interactive_requires_human_review` | Non-interactive simulation → AWAITING_APPROVAL |
| `test_approval_resume_approved` | submit_approval_decision(approved=True) resumes pipeline |
| `test_approval_resume_rejected` | submit_approval_decision(approved=False) → REJECTED_NEEDS_REVISION |
| `test_approval_rejection_no_frontend_failure_feedback` | Rejection does NOT create FrontendFailureFeedback, retry_count unchanged |
| `test_approval_persistence_roundtrip` | Full Task → JSON → Task round-trip preserves all approval fields |

**All 8 new tests + 6 existing tests = 14/14 PASS**

---

## Integration Points Verified

| Test File | Status | Notes |
|-----------|--------|-------|
| `test_frontend_production_gate.py` | 36/36 PASS | Production quality gate tests unchanged |
| `test_validation_retry_pass.py` | 1/1 PASS | Updated to use AUTO_PUBLISH policy for full pipeline |
| `test_frontend_contracts.py` | 10/10 PASS | Contract serialization verified |
| `test_frontend_pipeline_integration.py` | 14/14 PASS | All approval flows covered |

---

## Architecture Compliance (Phase 7C-4A Boundary)

✅ **No coupling between ClientProfile and ProductionQualityGate**  
- ClientProfile only contains identity + approval_policy reference
- ProductionQualityGate receives HTML/CSS/JS only, knows nothing about clients
- Approval policy resolution happens in `main.py` workflow layer

✅ **Case B Config (Direct Composition)**  
- Config has `wordpress_url`, `username`, `app_password` directly
- ClientProfile contains NO raw credentials
- No site registry, no indirection

✅ **MANUAL_REVIEW Legacy Checkpoint Removed**  
- Old `input()` blocking call eliminated
- Replaced by explicit `AWAITING_APPROVAL` pause + `submit_approval_decision()`
- Non-interactive mode no longer fails closed unexpectedly

✅ **Rejection ≠ Retry**  
- Human rejection → `REJECTED_NEEDS_REVISION` (terminal for current attempt)
- Does NOT increment `retry_count`
- Does NOT create `FrontendFailureFeedback`
- Frontend agent can be re-triggered separately for new attempt

---

## Files Modified

| File | Changes |
|------|---------|
| `contracts.py` | +ApprovalPolicyMode, ApprovalPolicy, ClientProfile with serialization |
| `state.py` | +2 TaskStatus values, 8 Task approval fields, to_dict/from_dict updates |
| `main.py` | +_resolve_approval_policy, +submit_approval_decision, +_continue_post_approval, run_workflow policy branch, load_state fix |
| `tests/test_frontend_pipeline_integration.py` | +8 new tests, fixed 2 existing tests (added missing mocks) |
| `tests/test_validation_retry_pass.py` | Updated to use AUTO_PUBLISH policy |

---

## Rollback Safety

- All changes are additive (new enums, optional fields, new methods)
- Default policy is REQUIRE_HUMAN_REVIEW (safe, explicit)
- Existing workflows without approval fields continue to work (fields default to None)
- JSON persistence is backward compatible (unknown fields ignored on load)

---

## Next Steps (Phase 7C-5+)

- CLI/UI for `submit_approval_decision` (admin dashboard, webhook endpoint)
- ApprovalPolicy registry (if multiple clients need shared policies)
- Audit logging for approval decisions
- Notification integration (email, Slack) on AWAITING_APPROVAL