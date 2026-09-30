"""Explicit publish intent bound to approved history. Performs no external call."""
from uuid import uuid4

from domain.contracts import ContentVersion, Status
from domain.publication import (ApprovedVersion, PublicationLease, PublicationRequest,
                                PublicationState, ReconciliationLease,
                                SAFE_PUBLICATION_ERROR_CODES,
                                SAFE_RECONCILIATION_ERROR_CODES)
from domain.publishing_target import PublishingProviderType
from persistence.codec import decode_snapshot
from persistence.connection import PersistenceError

APPROVAL_EVENT = 'TASK_APPROVED'
EXECUTOR_LOST = 'EXECUTOR_LOST'
# A stale lease that expired before the durable may-send boundary: no remote
# create was attempted, so the outcome is known and the row is FAILED.
EXECUTOR_LOST_BEFORE_SEND = 'EXECUTOR_LOST_BEFORE_SEND'
PUBLICATION_ALREADY_EXISTS = 'PUBLICATION_ALREADY_EXISTS'

_CLAIM_PREDICATE = ("publication_id=? AND workspace_id=? AND owner_id=? "
                    "AND fencing_token=? AND state='IN_PROGRESS'")
_CLAIM_ARGUMENTS = 'publication_id,workspace_id,owner_id,fencing_token'
_CLAIM_WRITES = ('UPDATE task_publication_requests SET heartbeat_at=?,updated_at=? '
                 f'WHERE {_CLAIM_PREDICATE}')

# The reconciliation ownership predicate is built by
# _reconciliation_predicate() rather than being a module constant, because it has
# to be table-qualified for the reads that join workspaces.
#
# It is deliberately NOT derived from _CLAIM_PREDICATE: the two leases govern
# different activities and a shared predicate would let one satisfy the other.
#
# state='INDETERMINATE' is load-bearing rather than incidental. It is what stops
# a reconciliation write from landing on a row that has since been resolved by
# something else -- an operator, or another tool -- between the claim and the
# result.

# Safe rejections from request_reconciliation. They name a local precondition, not
# a remote outcome, and none of them is ever persisted on the publication.
RECONCILIATION_NOT_FOUND = 'RECONCILIATION_NOT_FOUND'
RECONCILIATION_NOT_INDETERMINATE = 'RECONCILIATION_NOT_INDETERMINATE'
RECONCILIATION_NO_MAY_SEND = 'RECONCILIATION_NO_MAY_SEND'
RECONCILIATION_NO_TARGET_SNAPSHOT = 'RECONCILIATION_NO_TARGET_SNAPSHOT'
RECONCILIATION_WORKSPACE_ARCHIVED = 'RECONCILIATION_WORKSPACE_ARCHIVED'


class PublicationRepositoryMixin:
    def approved_version(self, workspace_id, task_id):
        """Resolve the exact approved ContentVersion from immutable approval history.

        Task.latest_content_version_id is never consulted. Returns None when the
        task exists but was never approved. Raises PersistenceError when recorded
        approval history is ambiguous, unbound, or inconsistent with the version or
        run it names, so a damaged history can never widen publication authority.
        """
        task = self._conn.execute(
            "SELECT t.* FROM tasks t JOIN workspaces w ON w.workspace_id=t.workspace_id "
            "WHERE t.workspace_id=? AND t.task_id=? AND w.status='ACTIVE'",
            (workspace_id, task_id)).fetchone()
        if task is None:
            return None
        events = self._conn.execute(
            "SELECT * FROM task_events WHERE task_id=? AND type=? ORDER BY sequence_number",
            (task_id, APPROVAL_EVENT)).fetchall()
        if not events:
            return None
        if len(events) > 1:
            raise PersistenceError('Ambiguous approval history')
        event = events[0]
        if event['run_id'] is None or type(event['attempt']) is not int:
            raise PersistenceError('Approval event is not bound to a run')
        version_id = decode_snapshot(event['metadata']).get('content_version_id')
        if type(version_id) is not str or not version_id.strip():
            raise PersistenceError('Approval event names no content version')
        run = self._conn.execute(
            "SELECT r.* FROM task_runs r JOIN tasks t ON t.task_id=r.task_id "
            "JOIN workspaces w ON w.workspace_id=t.workspace_id "
            "WHERE t.workspace_id=? AND r.task_id=? AND r.run_id=? AND w.status='ACTIVE'",
            (workspace_id, task_id, event['run_id'])).fetchone()
        if run is None or run['attempt'] != event['attempt']:
            raise PersistenceError('Approval event does not match a persisted run')
        row = self._conn.execute(
            "SELECT cv.* FROM content_versions cv JOIN tasks t ON t.task_id=cv.task_id "
            "JOIN workspaces w ON w.workspace_id=t.workspace_id "
            "WHERE cv.content_version_id=? AND t.workspace_id=? AND cv.task_id=? AND w.status='ACTIVE'",
            (version_id, workspace_id, task_id)).fetchone()
        if row is None:
            raise PersistenceError('Approved content version is missing')
        version = self._decode(ContentVersion, row)
        # The approved version must be the one the approving run actually produced.
        if version.run_id != event['run_id'] or version.content_type != task['content_type']:
            raise PersistenceError('Approval event does not match the approved content version')
        return ApprovedVersion(task_id=task_id, content_version_id=version.content_version_id,
                               content_type=version.content_type, run_id=run['run_id'],
                               run_attempt=run['attempt'])

    def request_publication(self, workspace_id, task_id, content_version_id, idempotency_key, now):
        """Record one durable intent to publish the exact approved ContentVersion.

        Returns (PublicationRequest, None) on success, or (None, error_code). Raises
        ValueError('IDEMPOTENCY_CONFLICT') when the key already names a different
        task or content version, and PersistenceError when approval history is
        damaged. The caller supplies content_version_id so a stale or guessed
        version is rejected rather than silently redirected.

        The workspace's ACTIVE publishing target is resolved HERE, at request time,
        and its identity plus configuration_version are snapshotted onto the row
        atomically with the request. This is the point of the snapshot: an executor
        must never re-resolve "whatever target is active now", because a
        publication recorded for site A would then be silently sent to site B if an
        administrator repointed the workspace in between.

        Only target identity is recorded. base_url, username, credential_reference
        and the secret itself are never copied onto the publication: the snapshot
        names the target, and the credential is resolved from the target at
        execution time.
        """
        self._write()
        existing = self._find_publication(workspace_id, idempotency_key)
        if existing is not None:
            # A replay returns the ORIGINAL row untouched. Re-resolving the active
            # target here would rewrite a durable intent because workspace
            # configuration changed after the fact, which is precisely the drift
            # the snapshot exists to prevent.
            if existing.task_id != task_id or existing.content_version_id != content_version_id:
                raise ValueError('IDEMPOTENCY_CONFLICT')
            return (existing, None)
        task = self._conn.execute(
            "SELECT status FROM tasks WHERE workspace_id=? AND task_id=?",
            (workspace_id, task_id)).fetchone()
        if task is None:
            return None, 'TASK_NOT_FOUND'
        if task['status'] != Status.APPROVED.value:
            return None, 'WRONG_TASK_STATE'
        approved = self.approved_version(workspace_id, task_id)
        if approved is None:
            return None, 'NOT_APPROVED'
        named = self._conn.execute(
            "SELECT cv.content_version_id FROM content_versions cv "
            "JOIN tasks t ON t.task_id=cv.task_id "
            "WHERE cv.content_version_id=? AND t.workspace_id=? AND cv.task_id=?",
            (content_version_id, workspace_id, task_id)).fetchone()
        if named is None:
            return None, 'VERSION_NOT_FOUND'
        if approved.content_version_id != content_version_id:
            return None, 'VERSION_MISMATCH'
        # Request-time target snapshot, read inside this same write transaction so
        # the target cannot change between resolution and insert. The composite
        # foreign key independently proves the target belongs to this workspace.
        #
        # The filter is on provider_type (WORDPRESS), NOT on the content type of
        # the article. POST/PAGE selects which REST collection the create uses;
        # it does not select the destination. One WordPress target serves both.
        target = self._conn.execute(
            "SELECT t.target_id,t.provider_type,t.configuration_version FROM publishing_targets t "
            "JOIN workspaces w ON w.workspace_id=t.workspace_id "
            "WHERE t.workspace_id=? AND t.provider_type=? AND t.status='ACTIVE' "
            "AND w.status='ACTIVE'",
            (workspace_id, PublishingProviderType.WORDPRESS.value)).fetchone()
        if target is None:
            # No destination is a configuration problem, not a remote failure. It
            # is reported before any row exists, so nothing is recorded that could
            # later be claimed and executed against an unknown target.
            return None, 'NO_ACTIVE_PUBLISHING_TARGET'
        # Publication lineage: one durable publication per approved content version
        # and target, regardless of which Idempotency-Key was used. A different key
        # means "this is a different request", not "publish this content again";
        # republish is a separate, future product feature. Returning a conflict
        # rather than replaying the first row matters: replaying would answer with
        # a publication the caller never asked for, under a key they believe is new.
        #
        # The database enforces the same invariant via UNIQUE(workspace_id,
        # content_version_id, target_id). This check exists to return a precise
        # domain code; the constraint is what makes it correct under concurrency.
        lineage = self._conn.execute(
            "SELECT publication_id FROM task_publication_requests "
            "WHERE workspace_id=? AND content_version_id=? AND target_id=?",
            (workspace_id, approved.content_version_id, target['target_id'])).fetchone()
        if lineage is not None:
            # Deliberately does NOT surface the existing row's idempotency key: it
            # is another caller's request identity and is not theirs to learn.
            raise ValueError(PUBLICATION_ALREADY_EXISTS)
        request = PublicationRequest(publication_id=str(uuid4()), workspace_id=workspace_id,
                                     task_id=task_id, content_version_id=approved.content_version_id,
                                     approved_run_id=approved.run_id, content_type=approved.content_type,
                                     idempotency_key=idempotency_key, state=PublicationState.PENDING,
                                     created_at=now, updated_at=now,
                                     target_id=target['target_id'],
                                     target_configuration_version=target['configuration_version'])
        self.add(request)
        return self.get(PublicationRequest, request.publication_id), None

    def get_publication(self, workspace_id, publication_id):
        """Read one publication request scoped to a workspace."""
        row = self._conn.execute(
            "SELECT p.* FROM task_publication_requests p "
            "JOIN workspaces w ON w.workspace_id=p.workspace_id "
            "WHERE p.workspace_id=? AND p.publication_id=? AND w.status='ACTIVE'",
            (workspace_id, publication_id)).fetchone()
        return self._decode(PublicationRequest, row)

    def publications_for_task(self, workspace_id, task_id):
        """List a task's publication requests in creation order."""
        rows = self._conn.execute(
            "SELECT p.* FROM task_publication_requests p "
            "JOIN workspaces w ON w.workspace_id=p.workspace_id "
            "WHERE p.workspace_id=? AND p.task_id=? AND w.status='ACTIVE' "
            "ORDER BY p.created_at, p.publication_id",
            (workspace_id, task_id)).fetchall()
        return [self._decode(PublicationRequest, row) for row in rows]

    def _find_publication(self, workspace_id, idempotency_key):
        row = self._conn.execute(
            "SELECT * FROM task_publication_requests WHERE workspace_id=? AND idempotency_key=?",
            (workspace_id, idempotency_key)).fetchone()
        return self._decode(PublicationRequest, row)

    def find_publication(self, workspace_id, idempotency_key):
        """Resolve an idempotency key to (task_id, content_version_id), or None."""
        request = self._find_publication(workspace_id, idempotency_key)
        if request is None:
            return None
        return (request.task_id, request.content_version_id, request.publication_id, request.state)

    # -- Execution ownership -------------------------------------------------
    # Reuses the TaskRun worker concepts (CAS claim, owner identity, monotonic
    # fencing token, heartbeat, stale expiry) without any TaskRun, RunMode,
    # task status, or task_events coupling. No method here performs external I/O.

    def claim_publication(self, workspace_id, owner_id, now):
        """Take exclusive execution ownership of one PENDING publication.

        Returns a PublicationLease, or None when nothing is claimable. The caller
        receives data only: there is no callback, context manager, or lazy handle,
        so a future network call cannot happen inside this transaction. The lease
        is committed with the caller's transaction before this value is usable.
        """
        self._write()
        row = self._conn.execute(
            "SELECT p.publication_id,p.task_id,p.fencing_token FROM task_publication_requests p "
            "JOIN workspaces w ON w.workspace_id=p.workspace_id "
            "WHERE p.workspace_id=? AND p.state='PENDING' AND w.status='ACTIVE' "
            # A row with no target snapshot predates publishing targets. It is
            # non-executable by contract: the database refuses to move it to
            # IN_PROGRESS without a target, so selecting it here would only turn
            # a clean "nothing to do" into a constraint error.
            "AND p.target_id IS NOT NULL "
            "ORDER BY p.created_at,p.publication_id LIMIT 1", (workspace_id,)).fetchone()
        if row is None:
            return None
        token = (row['fencing_token'] or 0) + 1
        changed = self._conn.execute(
            "UPDATE task_publication_requests SET state='IN_PROGRESS',owner_id=?,fencing_token=?,"
            "claimed_at=?,heartbeat_at=?,updated_at=? "
            "WHERE publication_id=? AND workspace_id=? AND state='PENDING'",
            (owner_id, token, now, now, now, row['publication_id'], workspace_id)).rowcount
        if changed != 1:
            # Another executor won the compare-and-swap inside the same window.
            return None
        return PublicationLease(publication_id=row['publication_id'], task_id=row['task_id'],
                                workspace_id=workspace_id, owner_id=owner_id, fencing_token=token)

    def owned_publication(self, lease):
        """Read the row a lease claims to own. Read-only; safe on a reader."""
        return self._conn.execute(
            "SELECT p.* FROM task_publication_requests p "
            "JOIN workspaces w ON w.workspace_id=p.workspace_id "
            f"WHERE p.publication_id=? AND p.workspace_id=? AND p.task_id=? AND p.owner_id=? "
            "AND p.fencing_token=? AND p.state='IN_PROGRESS' AND w.status='ACTIVE'",
            (lease.publication_id, lease.workspace_id, lease.task_id, lease.owner_id,
             lease.fencing_token)).fetchone()

    def assert_publication_ownership(self, lease):
        """True only while this exact lease still owns an IN_PROGRESS publication."""
        return self.owned_publication(lease) is not None

    def heartbeat_publication(self, lease, now):
        """Refresh the lease. Never changes state; false means the lease is stale."""
        self._write()
        if self.owned_publication(lease) is None:
            return False
        return self._conn.execute(
            _CLAIM_WRITES, (now, now, lease.publication_id, lease.workspace_id,
                            lease.owner_id, lease.fencing_token)).rowcount == 1

    def complete_publication(self, lease, remote_resource_id, remote_url, now):
        """Record a confirmed external success. Requires positive remote evidence."""
        if type(remote_resource_id) is not int or remote_resource_id <= 0:
            raise ValueError('remote_resource_id must be a positive int')
        if remote_url is not None and (type(remote_url) is not str or not remote_url.strip()):
            raise ValueError('remote_url must be a non-empty string when present')
        return self._finish_publication(lease, "SET state='SUCCEEDED',remote_resource_id=?,"
                                        "remote_url=?,error_code=NULL,updated_at=?",
                                        (remote_resource_id, remote_url, now))

    def fail_publication(self, lease, error_code, now):
        """Record a confirmed failure that created no remote resource."""
        self._check_publication_error_code(error_code)
        return self._finish_publication(lease, "SET state='FAILED',error_code=?,updated_at=?",
                                        (error_code, now))

    def mark_publication_indeterminate(self, lease, error_code, now):
        """Record that the external outcome cannot be proven either way."""
        self._check_publication_error_code(error_code)
        return self._finish_publication(lease, "SET state='INDETERMINATE',error_code=?,updated_at=?",
                                        (error_code, now))

    def _finish_publication(self, lease, assignments, values):
        self._write()
        if self.owned_publication(lease) is None:
            return False
        changed = self._conn.execute(
            f"UPDATE task_publication_requests {assignments} WHERE {_CLAIM_PREDICATE}",
            (*values, lease.publication_id, lease.workspace_id, lease.owner_id,
             lease.fencing_token)).rowcount
        if changed != 1:
            return False
        # Confirm the write landed in a decodable state before reporting success.
        self._decode(PublicationRequest, self._conn.execute(
            'SELECT * FROM task_publication_requests WHERE publication_id=?',
            (lease.publication_id,)).fetchone())
        return True

    def mark_publication_may_send(self, lease, now):
        """Record that a remote create MAY now be attempted. Commit, then call the gateway.

        This is the durable boundary that separates a crash which provably never
        attempted a remote create from one that may have. It MUST be committed in
        its own transaction before ``gateway.publish`` is invoked, because a
        create that is sent and then lost is indistinguishable from one that was
        never sent unless the marker was already durable.

        It states nothing about the request itself: it does not mean bytes left
        the process, that WordPress received anything, or that a resource was
        created. It is the weakest durable claim that is still useful.

        Same-lease replay returns **False**. A second call means the caller is
        about to make a second create attempt, and returning True would let a
        duplicated gateway invocation hide behind an idempotent-looking result.
        The timestamp is not rewritten, and the database refuses to clear or
        change it, so the first boundary stands even if this returns False.
        """
        self._write()
        if self.owned_publication(lease) is None:
            return False
        changed = self._conn.execute(
            "UPDATE task_publication_requests SET may_send_at=?,updated_at=? "
            f"WHERE {_CLAIM_PREDICATE} AND may_send_at IS NULL",
            (now, now, lease.publication_id, lease.workspace_id, lease.owner_id,
             lease.fencing_token)).rowcount
        return changed == 1

    def expire_stale_publications(self, cutoff, now):
        """Fence out executors whose lease went silent, split by the may-send boundary.

        may_send_at IS NULL     -> FAILED / EXECUTOR_LOST_BEFORE_SEND. The remote
                                   create was never attempted, so the remote
                                   outcome is known and the row is terminally
                                   failed rather than left ambiguous.
        may_send_at IS NOT NULL -> INDETERMINATE / EXECUTOR_LOST. A create may
                                   have been attempted, so nothing is provable
                                   and reconciliation is required.

        Both paths increment the fencing token, so a resumed executor is
        permanently fenced out and cannot persist an outcome. Neither ever
        returns to PENDING: PENDING implies a fresh external call, and a terminal
        state may already have produced a remote resource. A publication is never
        automatically retried; INDETERMINATE resolution is a later, explicit slice.
        """
        self._write()
        rows = self._conn.execute(
            "SELECT publication_id,owner_id,fencing_token,may_send_at "
            "FROM task_publication_requests "
            "WHERE state='IN_PROGRESS' AND COALESCE(heartbeat_at,claimed_at) <= ?",
            (cutoff,)).fetchall()
        expired = 0
        for row in rows:
            # A pre-send crash is a local failure, not an unknown remote outcome.
            # Using INDETERMINATE there would claim a create may have happened
            # when this executor provably never sent one, and would send it
            # through needless reconciliation.
            state = 'INDETERMINATE' if row['may_send_at'] is not None else 'FAILED'
            code = EXECUTOR_LOST if row['may_send_at'] is not None else EXECUTOR_LOST_BEFORE_SEND
            changed = self._conn.execute(
                "UPDATE task_publication_requests SET state=?,error_code=?,"
                "fencing_token=fencing_token+1,updated_at=? "
                "WHERE publication_id=? AND state='IN_PROGRESS' AND fencing_token=? "
                "AND owner_id IS ? AND may_send_at IS ? "
                "AND COALESCE(heartbeat_at,claimed_at) <= ?",
                (state, code, now, row['publication_id'], row['fencing_token'],
                 row['owner_id'], row['may_send_at'], cutoff)).rowcount
            expired += changed
        return expired

    @staticmethod
    def _check_publication_error_code(error_code):
        """Reject anything that is not a classified code, mirroring fail_run."""
        if error_code not in SAFE_PUBLICATION_ERROR_CODES:
            raise ValueError('Unsupported safe publication error code')

    @staticmethod
    def _check_reconciliation_error_code(error_code):
        """Reject anything outside the reconciliation vocabulary.

        Checked against its own frozenset, not SAFE_PUBLICATION_ERROR_CODES. A
        reconciliation diagnostic is a different kind of fact, and letting the
        two sets overlap would invite a future caller to persist 'TIMEOUT' as a
        reconciliation outcome and 'RECONCILIATION_ZERO_MATCH' as a publication
        failure. Neither is a thing this system can honestly assert.
        """
        if error_code not in SAFE_RECONCILIATION_ERROR_CODES:
            raise ValueError('Unsupported safe reconciliation error code')

    # -- Reconciliation ownership (8.3-3C6C) --------------------------------
    #
    # Read-only with respect to WordPress, and operator-triggered. Nothing below
    # performs or schedules a network call: these methods only move durable
    # ownership and diagnostics. The one state change in the whole set is
    # INDETERMINATE -> SUCCEEDED, and only on a positive proof of presence.

    def request_reconciliation(self, workspace_id, publication_id, now):
        """Explicitly ask for one publication to be investigated.

        Returns None when the request is accepted, or a safe code naming the
        local precondition that rejected it. The four repeated-request cases are
        defined, not incidental:

        * **not pending, not claimed** -- a due request is created. This is the
          only path that sets ``reconciliation_requested_at``.
        * **already pending** -- idempotent success. The existing due flag is
          kept, so its ordering position does not jump forward on a repeat.
        * **currently claimed** -- accepted as a no-op. Ownership is never
          stolen or extended. The publication is already being investigated, so
          the operator's intent is satisfied in progress; a further request is
          needed after the attempt finishes. Recording a due flag here would be
          actively harmful: it would re-arm the row the moment the attempt
          completed, which is the automatic retry loop this design exists to
          avoid.
        * **already SUCCEEDED / FAILED / IN_PROGRESS / PENDING** -- rejected with
          RECONCILIATION_NOT_INDETERMINATE. There is nothing to reconcile.

        Whether the historical target configuration still exists is deliberately
        NOT checked here. Filtering it at request time would make such a
        publication permanently un-requestable, and the reason it cannot be
        investigated would never be recorded. Claiming it and finishing with
        RECONCILIATION_TARGET_UNAVAILABLE makes the condition observable.
        """
        self._write()
        if not self._active_workspace(workspace_id):
            return RECONCILIATION_WORKSPACE_ARCHIVED
        row = self._conn.execute(
            "SELECT state,may_send_at,target_id,target_configuration_version,"
            "reconciliation_requested_at,reconciliation_owner_id "
            "FROM task_publication_requests WHERE publication_id=? AND workspace_id=?",
            (publication_id, workspace_id)).fetchone()
        if row is None:
            return RECONCILIATION_NOT_FOUND
        if row['state'] != 'INDETERMINATE':
            return RECONCILIATION_NOT_INDETERMINATE
        if row['may_send_at'] is None:
            # No create was ever attempted, so there is nothing to find.
            return RECONCILIATION_NO_MAY_SEND
        if row['target_id'] is None or row['target_configuration_version'] is None:
            return RECONCILIATION_NO_TARGET_SNAPSHOT
        if row['reconciliation_owner_id'] is not None:
            return None
        if row['reconciliation_requested_at'] is not None:
            return None
        changed = self._conn.execute(
            "UPDATE task_publication_requests SET reconciliation_requested_at=?,updated_at=? "
            "WHERE publication_id=? AND workspace_id=? AND state='INDETERMINATE' "
            "AND reconciliation_requested_at IS NULL AND reconciliation_owner_id IS NULL",
            (now, now, publication_id, workspace_id)).rowcount
        return None if changed == 1 else RECONCILIATION_NOT_INDETERMINATE

    def claim_publication_for_reconciliation(self, workspace_id, owner_id, now):
        """Take exclusive ownership of one explicitly requested investigation.

        Returns a ReconciliationLease, or None when no requested work exists. The
        caller receives data only, so no network call can happen inside this
        transaction.

        Four preconditions, all enforced by the database as well as here:

        * state='INDETERMINATE' -- the only state with a recoverable question.
        * ``reconciliation_requested_at IS NOT NULL`` -- operator-triggered. A
          sweep that claimed every INDETERMINATE row would retry forever, because
          an attempt that proves nothing returns the row to exactly this state.
        * ``may_send_at IS NOT NULL`` -- a create may have been attempted. Without
          it there is provably nothing to find, and searching for it would only
          produce a fabricated zero-match.
        * target snapshot present -- no destination, no search.

        Workspace must be ACTIVE, so an archived workspace never starts network
        work. The historical target configuration is NOT required to exist: see
        request_reconciliation.

        The claim **consumes** the request (``reconciliation_requested_at`` is
        cleared in the same UPDATE). That single fact is what makes the
        operator-triggered model honest -- an unresolved attempt leaves the row
        INDETERMINATE but no longer due.
        """
        self._write()
        row = self._conn.execute(
            "SELECT p.publication_id,p.reconciliation_fencing_token FROM task_publication_requests p "
            "JOIN workspaces w ON w.workspace_id=p.workspace_id "
            "WHERE p.workspace_id=? AND p.state='INDETERMINATE' "
            "AND p.reconciliation_requested_at IS NOT NULL "
            "AND p.reconciliation_owner_id IS NULL "
            "AND p.may_send_at IS NOT NULL "
            "AND p.target_id IS NOT NULL AND p.target_configuration_version IS NOT NULL "
            "AND w.status='ACTIVE' "
            # Deterministic: the oldest request first, publication_id breaking ties.
            "ORDER BY p.reconciliation_requested_at,p.publication_id LIMIT 1",
            (workspace_id,)).fetchone()
        if row is None:
            return None
        token = (row['reconciliation_fencing_token'] or 0) + 1
        changed = self._conn.execute(
            "UPDATE task_publication_requests SET "
            "reconciliation_owner_id=?,reconciliation_fencing_token=?,"
            "reconciliation_claimed_at=?,reconciliation_heartbeat_at=?,"
            "reconciliation_requested_at=NULL,updated_at=? "
            "WHERE publication_id=? AND workspace_id=? AND state='INDETERMINATE' "
            "AND reconciliation_requested_at IS NOT NULL AND reconciliation_owner_id IS NULL "
            "AND may_send_at IS NOT NULL",
            (owner_id, token, now, now, now, row['publication_id'], workspace_id)).rowcount
        if changed != 1:
            # Another worker won the compare-and-swap inside the same window.
            return None
        return ReconciliationLease(publication_id=row['publication_id'],
                                   workspace_id=workspace_id, owner_id=owner_id,
                                   fencing_token=token)

    def owned_reconciliation(self, lease):
        """Read the row this reconciliation lease claims to own. Read-only."""
        return self._conn.execute(
            "SELECT p.* FROM task_publication_requests p "
            "JOIN workspaces w ON w.workspace_id=p.workspace_id "
            f"WHERE {self._reconciliation_predicate(lease, 'p')} "
            "AND w.status='ACTIVE'",
            (lease.publication_id, lease.workspace_id, lease.owner_id,
             lease.fencing_token)).fetchone()

    def assert_reconciliation_ownership(self, lease):
        """True only while this exact lease still owns the investigation."""
        return self.owned_reconciliation(lease) is not None

    def heartbeat_reconciliation(self, lease, now):
        """Refresh the reconciliation lease. False means ownership was lost.

        Exists because the lookup this lease authorises is a multi-page scan
        whose duration is not bounded: 3C5C established that the HTTP timeout is
        per-phase, so N pages x 2 phases has no fixed ceiling. Without a heartbeat
        a long scan would look abandoned, and without expiry a crashed worker
        would hold the claim forever -- blocking every future operator request,
        since a request may not steal ownership.
        """
        self._write()
        if self.owned_reconciliation(lease) is None:
            return False
        return self._conn.execute(
            "UPDATE task_publication_requests SET reconciliation_heartbeat_at=?,updated_at=? "
            f"WHERE {self._reconciliation_predicate(lease)}",
            (now, now, lease.publication_id, lease.workspace_id, lease.owner_id,
             lease.fencing_token)).rowcount == 1

    def complete_reconciliation_unresolved(self, lease, error_code, now):
        """Finish an attempt that could NOT prove success.

        The publication stays INDETERMINATE. That is not a fallback: it is the
        only honest outcome, because nothing in this architecture can prove a
        remote resource is absent. A zero-match scan, an ambiguous result, a
        cross-type hit, an unreachable site, a 403, and a missing historical
        target all land here, and all of them leave the question open.

        There is deliberately no resolve_reconciliation_failed(). Adding one
        would put a single call between a future worker and a terminal FAILED
        that no evidence in this system can support.

        What this DOES change: ownership is released, a safe diagnostic is
        recorded, and the request stays consumed. The row does not become
        claimable again until an operator explicitly requests it again.
        """
        self._check_reconciliation_error_code(error_code)
        self._write()
        if self.owned_reconciliation(lease) is None:
            return False
        return self._conn.execute(
            "UPDATE task_publication_requests SET "
            "reconciliation_owner_id=NULL,reconciliation_claimed_at=NULL,"
            "reconciliation_heartbeat_at=NULL,reconciliation_error_code=?,"
            "reconciliation_last_attempted_at=?,updated_at=? "
            f"WHERE {self._reconciliation_predicate(lease)}",
            (error_code, now, now, lease.publication_id, lease.workspace_id,
             lease.owner_id, lease.fencing_token)).rowcount == 1

    def resolve_reconciliation_succeeded(self, lease, remote_resource_id, remote_url, now):
        """Resolve INDETERMINATE -> SUCCEEDED on positive proof of presence.

        The ONLY state change reconciliation can make. It requires the caller to
        supply a positive remote id: that id is the evidence, and nothing here
        infers one from a slug, a title, or a permalink. ``remote_url`` is
        optional and is never required, because a lookup may legitimately not
        return a link -- the id is what identifies the resource.

        Ownership is cleared in the same statement. The 0014 CHECK forbids a
        non-INDETERMINATE row from carrying reconciliation ownership, so a caller
        that forgot to release it would have this UPDATE abort rather than leave
        a SUCCEEDED row that a worker still believes it owns.

        Deliberately NOT complete_publication(): that requires a PublicationLease
        and state='IN_PROGRESS', and reusing it would mean pretending a
        reconciliation is a second execution.
        """
        if type(remote_resource_id) is not int or remote_resource_id <= 0:
            raise ValueError('remote_resource_id must be a positive int')
        if remote_url is not None and (type(remote_url) is not str or not remote_url.strip()):
            raise ValueError('remote_url must be a non-empty string when present')
        self._write()
        if self.owned_reconciliation(lease) is None:
            return False
        return self._conn.execute(
            "UPDATE task_publication_requests SET state='SUCCEEDED',"
            "remote_resource_id=?,remote_url=?,error_code=NULL,"
            "reconciliation_owner_id=NULL,reconciliation_claimed_at=NULL,"
            "reconciliation_heartbeat_at=NULL,reconciliation_error_code=NULL,"
            "reconciliation_last_attempted_at=?,updated_at=? "
            f"WHERE {self._reconciliation_predicate(lease)}",
            (remote_resource_id, remote_url, now, now, lease.publication_id,
             lease.workspace_id, lease.owner_id, lease.fencing_token)).rowcount == 1

    def expire_stale_reconciliation(self, cutoff, now):
        """Release reconciliation ownership whose worker went silent.

        The state is NOT changed. An expired attempt is not evidence about the
        remote at all -- the worker may have died before it read anything -- so
        moving the row to FAILED would assert a conclusion nobody observed, and
        moving it to SUCCEEDED would invent one.

        What happens instead: ownership is released, the fencing token is
        incremented so the crashed worker is permanently fenced out, a safe
        EXECUTOR_LOST-style diagnostic is recorded, and the request stays
        consumed. The row remains INDETERMINATE and is NOT requeued; a new
        attempt requires a new explicit operator request.

        Deliberately does not touch may_send_at, state, or the execution
        ownership columns.
        """
        self._write()
        rows = self._conn.execute(
            "SELECT publication_id,reconciliation_owner_id,reconciliation_fencing_token "
            "FROM task_publication_requests "
            "WHERE state='INDETERMINATE' AND reconciliation_owner_id IS NOT NULL "
            "AND COALESCE(reconciliation_heartbeat_at,reconciliation_claimed_at) <= ?",
            (cutoff,)).fetchall()
        expired = 0
        for row in rows:
            changed = self._conn.execute(
                "UPDATE task_publication_requests SET reconciliation_owner_id=NULL,"
                "reconciliation_claimed_at=NULL,reconciliation_heartbeat_at=NULL,"
                "reconciliation_fencing_token=reconciliation_fencing_token+1,"
                "reconciliation_error_code='RECONCILIATION_UNAVAILABLE',"
                "reconciliation_last_attempted_at=?,updated_at=? "
                "WHERE publication_id=? AND state='INDETERMINATE' "
                "AND reconciliation_owner_id=? AND reconciliation_fencing_token=?",
                (now, now, row['publication_id'], row['reconciliation_owner_id'],
                 row['reconciliation_fencing_token'])).rowcount
            expired += changed
        return expired

    @staticmethod
    def _reconciliation_predicate(lease, alias=''):
        """The full ownership predicate, optionally table-qualified.

        The alias exists because ``owned_reconciliation`` joins ``workspaces``,
        where a bare ``workspace_id`` is ambiguous between the two tables -- and
        an ambiguous column is an OperationalError, not a safe default, so the
        qualification must be explicit rather than left to SQLite.
        """
        prefix = f"{alias}." if alias else ""
        return (f"{prefix}publication_id=? AND {prefix}workspace_id=? "
                f"AND {prefix}reconciliation_owner_id=? "
                f"AND {prefix}reconciliation_fencing_token=? "
                f"AND {prefix}state='INDETERMINATE'")
