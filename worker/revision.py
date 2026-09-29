"""Revision execution context - immutable composition of revision decision."""
from dataclasses import dataclass
from persistence.connection import PersistenceError
from domain.contracts import RunMode


@dataclass(frozen=True, kw_only=True)
class RevisionContext:
    """Execution-time revision context composed from authoritative persisted sources.

    Authoritative sources (not duplicated here):
    - task_revision_requests → source_content_version_id, reviewer_feedback
    - content_versions → source_content
    - Task (persistence domain) → original requirement (passed separately)

    This is NOT persisted. It is constructed fresh per Worker execution.
    """
    source_content_version_id: str
    source_content: str
    reviewer_feedback: str


def build_revision_context(repo, workspace_id: str, run_id: str) -> RevisionContext:
    """Resolve RevisionContext from the revision decision for a REVISION run.

    Args:
        repo: Workspace-scoped repository
        workspace_id: Active workspace ID
        run_id: REVISION TaskRun ID

    Returns:
        RevisionContext with source content and reviewer feedback

    Raises:
        PersistenceError: If revision decision or source ContentVersion not found/valid
    """
    # Find the authoritative revision run (follow retry lineage if needed)
    authoritative_run_id = _resolve_authoritative_revision_run(repo, workspace_id, run_id)
    
    # Find revision request by resulting_run_id of the authoritative run
    rev_req = repo.find_revision_request_by_run_id(authoritative_run_id)
    if rev_req is None:
        raise PersistenceError(f'Revision decision not found for run {authoritative_run_id}')
    task_id, content_version_id, feedback = rev_req
    # Load source ContentVersion
    cv = repo.get_content_version(content_version_id)
    if cv is None:
        raise PersistenceError(f'Source ContentVersion {content_version_id} not found')
    if cv.task_id != task_id:
        raise PersistenceError(f'ContentVersion {content_version_id} does not belong to task {task_id}')
    if not cv.content.strip():
        raise PersistenceError(f'Source ContentVersion {content_version_id} has empty content')
    return RevisionContext(
        source_content_version_id=content_version_id,
        source_content=cv.content,
        reviewer_feedback=feedback,
    )


def _resolve_authoritative_revision_run(repo, workspace_id: str, run_id: str) -> str:
    """Follow retry lineage to find the original human revision run."""
    # Get the original run to capture its task_id for cross-task validation
    original_run = repo.get_run_by_id(run_id)
    if original_run is None:
        raise PersistenceError(f'Run {run_id} not found')
    if original_run.run_mode != RunMode.REVISION:
        raise PersistenceError(f'Run {run_id} is not a REVISION run')
    original_task_id = original_run.task_id
    
    current_run_id = run_id
    while True:
        run = repo.get_run_by_id(current_run_id)
        if run is None:
            raise PersistenceError(f'Run {current_run_id} not found')
        if run.run_mode != RunMode.REVISION:
            raise PersistenceError(f'Run {current_run_id} is not a REVISION run')
        # Cross-task validation: all runs in the lineage must belong to the same task
        if run.task_id != original_task_id:
            raise PersistenceError(f'Cross-task revision lineage detected: run {current_run_id} belongs to task {run.task_id}, expected {original_task_id}')
        # If this run has no source_revision_run_id, it's the original human revision
        if run.source_revision_run_id is None:
            return current_run_id
        # Otherwise, follow the lineage to the originating revision run
        current_run_id = run.source_revision_run_id