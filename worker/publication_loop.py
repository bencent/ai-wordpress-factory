"""Synchronous publication runtime: one iteration, one process, one thread.

This module is ORCHESTRATION ONLY. It constructs no dependency, opens no
connection, and knows nothing about WordPress, HTTP, or secrets. Everything it
calls is injected, which is why the whole loop is testable with fakes and why
nothing here can perform network I/O even by accident.

Why a separate loop rather than another executor for ``Worker``
----------------------------------------------------------------
``worker.loop.Worker`` owns a **TaskRun** lease and calls
``executor(lease, cancelled)`` -- an interface whose parameter is a TaskRun.
``PublicationExecutor`` and ``PublicationReconciler`` each own a *different*
claim model on *different* columns (``PublicationLease`` and
``ReconciliationLease``), and each is responsible for its own transaction
boundaries and its own network window. Forcing them through the TaskRun callable
would mean a dispatcher that branches on lease type and an outer transaction
spanning a remote call. This module instead calls ``run_once(workspace_id)``
directly, outside any transaction, exactly as each service documents.

Ordering, and why expiry comes first
-------------------------------------
Every iteration, in this order:

    1. expire stale publication EXECUTION ownership
    2. expire stale RECONCILIATION ownership
    3. executor.run_once(workspace_id)
    4. reconciler.run_once(workspace_id)

Expiry leads because a claim is only meaningful once stale claims have been
fenced. Claiming first and expiring after would let a resurrected claim be
handed out before the previous holder was invalidated, and the fencing token
would be advanced underneath a worker that still believed it owned the row. This
mirrors ``Worker.run_once``, which expires and then claims.

Bounded work per iteration
--------------------------
At most one publication and at most one reconciliation per iteration, never a
drain loop. That keeps shutdown latency predictable -- the loop cannot be stuck
inside a queue drain when a stop signal arrives -- and it stops either queue from
starving the other. Polling for new durable work is not a retry: a publication
that fails is terminal or INDETERMINATE, and this loop never re-claims either.
"""
import logging
from datetime import timedelta

log = logging.getLogger(__name__)


class PublicationWorker:
    """Run publication execution and reconciliation as one bounded iteration.

    Two ownership regimes, one process, one thread. The executor's and the
    reconciler's results are summed only for the caller's benefit; this class
    never interprets either outcome and never writes publication state itself.
    """

    def __init__(self, store, executor, reconciler, *, workspace_id, clock,
                 stale_seconds: float, owner_id: str = None):
        """
        Parameters
        ----------
        store:
            The store used for the two expiry sweeps. Expiry must be a write, so
            it runs in its own short transaction.
        executor, reconciler:
            Objects exposing ``run_once(workspace_id) -> int``.
        workspace_id:
            Phase 8 is single-workspace, resolved by the bootstrap from
            ``default_workspace()``. Bound here so ``run()`` needs no argument;
            ``run_once`` still accepts an explicit one, which is the seam a
            future multi-workspace scheduler would use.
        clock:
            Returns a timezone-aware ``datetime``, matching
            ``TaskHTTPService.clock`` and the content Worker's injected clock.
        stale_seconds:
            How long a claim may go unheard before it is fenced. One value is used
            for both regimes, matching the single ``--stale-seconds`` CLI flag.
        owner_id:
            Process identity, recorded for diagnostics only. A fresh uuid4 is
            generated when omitted, matching ``worker.loop.Worker`` so two
            separately constructed runtimes can never share an identity.
        """
        if isinstance(stale_seconds, bool) or not isinstance(stale_seconds, (int, float)) \
                or stale_seconds <= 0:
            raise ValueError('stale_seconds must be a positive number')
        if type(workspace_id) is not str or not workspace_id.strip():
            raise ValueError('workspace_id is required')
        for name, candidate in (('executor', executor), ('reconciler', reconciler)):
            if not callable(getattr(candidate, 'run_once', None)):
                raise ValueError(f'{name} must expose run_once(workspace_id)')
        self._store = store
        self._executor = executor
        self._reconciler = reconciler
        self._workspace_id = workspace_id
        self._clock = clock
        self._stale_seconds = float(stale_seconds)
        if owner_id is None:
            from uuid import uuid4
            owner_id = str(uuid4())
        self.owner_id = owner_id

    # -- one iteration -----------------------------------------------------

    def run_once(self, workspace_id: str = None) -> int:
        """Expire, then give each regime one bounded turn. Returns work consumed.

        The count is ``0``, ``1`` or ``2``: how many units of durable work this
        iteration took. It is a liveness signal for the loop, not a publication
        outcome -- a publication that ended FAILED and one that ended
        SUCCEEDED both count as 1, because both consumed a claim.
        """
        workspace_id = workspace_id or self._workspace_id
        self._expire(workspace_id)
        consumed = 0
        consumed += 1 if self._executor.run_once(workspace_id) else 0
        consumed += 1 if self._reconciler.run_once(workspace_id) else 0
        return consumed

    def _expire(self, workspace_id: str) -> None:
        """Fence stale claims in both regimes, each in its own short transaction.

        Publication expiry and reconciliation expiry are separate transactions on
        purpose: they write different columns and are independently safe, and one
        failing must not undo the other's fencing.

        No executor or reconciler call is wrapped here. Both services own their
        own transaction boundaries and perform network I/O between short
        transactions; holding this transaction open across either would hold a
        SQLite write lock for the length of a remote scan.
        """
        now = self._clock()
        cutoff = (now - timedelta(seconds=self._stale_seconds)).isoformat()
        with self._store.workspace_transaction(workspace_id) as repo:
            expired = repo.expire_stale_publications(cutoff, now.isoformat())
        if expired:
            log.info('publication execution: fenced %s stale claim(s)', expired)
        with self._store.workspace_transaction(workspace_id) as repo:
            expired = repo.expire_stale_reconciliation(cutoff, now.isoformat())
        if expired:
            log.info('reconciliation: fenced %s stale claim(s)', expired)

    # -- loop --------------------------------------------------------------

    def run(self, stop, *, poll_seconds: float = 1.0) -> None:
        """Iterate until ``stop`` is set.

        Idle uses ``stop.wait(poll_seconds)`` rather than ``time.sleep``, matching
        ``worker.loop.Worker.run`` so a signal interrupts the wait immediately
        instead of after the full interval. A non-empty iteration continues
        immediately: work exists, so there is nothing to wait for.
        """
        if isinstance(poll_seconds, bool) or not isinstance(poll_seconds, (int, float)) \
                or poll_seconds <= 0:
            raise ValueError('poll_seconds must be positive')
        while not stop.is_set():
            if not self.run_once():
                stop.wait(poll_seconds)
