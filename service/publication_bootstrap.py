"""Production composition for the publication runtime.

This module WIRES existing services. It contains no publish logic, no
reconciliation logic, and no state semantics: every decision about a publication
belongs to ``PublicationExecutor`` / ``PublicationReconciler``, and every write
belongs to the repository. What lives here is only the question "which concrete
objects do those two services get in production".

Composition choices, and why
---------------------------
* **One transport, shared.** ``RequestsHttpTransport`` owns a ``requests.Session``.
  Building one per gateway would create a session -- and a connection pool -- per
  attempt and leak it. One transport is built and closed by the factory, so
  pooling works and ``MAX_RETRIES`` stays pinned at 0.
* **One gateway factory, both services.** Both call
  ``factory(connection, timeout)``, so a single factory serves the create path
  and the read path without either service knowing which it is talking to.
* **No secret is touched here.** The resolver is injected, never invoked. A
  bootstrap that resolved a credential would be a place a secret exists outside
  the abstraction that is careful about it.
* **No workspace iteration.** Phase 8 is single-workspace, and the content
  worker already resolves ``default_workspace()``. Following the same assumption
  keeps the two runtimes consistent and defers multi-tenancy to a decision rather
  than a guess.
"""
import json
import logging
import os
import signal
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

from domain.contracts import ContentType  # noqa: F401  (documents the type flow)
from persistence.connection import ConnectionFactory
from persistence.migration_runner import migrate
from persistence.repository import SQLiteStore
from publishing.environment_secrets import EnvironmentSecretResolver
from publishing.requests_transport import RequestsHttpTransport
from publishing.wordpress import WordPressGateway
from service.publication_executor import PublicationExecutor
from service.publication_reconciler import PublicationReconciler
from service.publish_command import PublishCommandService
from worker.publication_loop import PublicationWorker

log = logging.getLogger(__name__)

# The gateway timeout is per CONNECT and per READ phase, not a total: 3C5C
# measured that in the installed requests. PublicationExecutor therefore refuses
# to construct unless ``2 * gateway_timeout + margin <= stale_seconds``.
#
# 15s with the 60s default stale window gives 2*15+5 = 35s of headroom against 60s.
# 15s rather than the 30s default because the reconciler has NO timing guard of
# its own: a multi-page scan has no aggregate bound, so a smaller per-phase
# timeout is the only lever left on that path.
DEFAULT_PUBLICATION_GATEWAY_TIMEOUT = 15.0


class PublicationBootstrapError(RuntimeError):
    """Publication runtime configuration error."""


def _default_gateway_timeout() -> float:
    raw = os.environ.get('AIWF_PUBLICATION_GATEWAY_TIMEOUT')
    if raw is None:
        return DEFAULT_PUBLICATION_GATEWAY_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        raise PublicationBootstrapError('AIWF_PUBLICATION_GATEWAY_TIMEOUT must be a number') from None
    if value <= 0:
        raise PublicationBootstrapError('AIWF_PUBLICATION_GATEWAY_TIMEOUT must be positive')
    return value


def _default_stale_seconds() -> float:
    raw = os.environ.get('AIWF_PUBLICATION_STALE_SECONDS')
    if raw is None:
        return 60.0
    try:
        value = float(raw)
    except ValueError:
        raise PublicationBootstrapError('AIWF_PUBLICATION_STALE_SECONDS must be a number') from None
    if value <= 0:
        raise PublicationBootstrapError('AIWF_PUBLICATION_STALE_SECONDS must be positive')
    return value


def _default_database_path() -> str:
    return os.environ.get('AIWF_DATABASE', 'data/aiwf.sqlite3')


def build_publication_worker(
    database_path: Optional[str] = None,
    stale_seconds: Optional[float] = None,
    gateway_timeout: Optional[float] = None,
    store: Optional[SQLiteStore] = None,
    owner_id: Optional[str] = None,
) -> PublicationWorker:
    """Construct a fully-wired publication runtime from production dependencies.

    ``store`` may be supplied to reuse an already-open store; otherwise one is
    opened and migrated at ``database_path``. ``owner_id`` is a test seam only:
    production leaves it unset so each runtime mints its own identity, matching
    ``worker.loop.Worker``.
    """
    stale = _default_stale_seconds() if stale_seconds is None else stale_seconds
    timeout = _default_gateway_timeout() if gateway_timeout is None else gateway_timeout

    if store is None:
        factory = ConnectionFactory(database_path or _default_database_path())
        migrate(factory)
        store = SQLiteStore(factory)

    with store.reader() as repo:
        workspace = repo.default_workspace()
    if workspace is None:
        raise PublicationBootstrapError('Default workspace not found')
    workspace_id = workspace.workspace_id

    # One transport, shared by every gateway this runtime builds. Its session is
    # never closed here: the process owns it for its lifetime, which is what
    # makes connection pooling worth having.
    transport = RequestsHttpTransport()

    def gateway_factory(connection, gateway_timeout_seconds):
        return WordPressGateway(connection, transport, timeout=gateway_timeout_seconds)

    # Injected, never invoked here. A bootstrap that resolved a credential would
    # be a second place a secret exists, outside the abstraction built to keep it
    # out of logs, exceptions and rows.
    secret_resolver = EnvironmentSecretResolver()
    clock = lambda: datetime.now(timezone.utc)  # noqa: E731 - matches the other runtimes

    # One process identity, minted once and shared by both services. Sharing it is
    # safe precisely because the two regimes never share a predicate: execution
    # matches the IN_PROGRESS columns, reconciliation matches the INDETERMINATE
    # ones, so an owner id cannot let one satisfy the other's ownership check.
    runtime_owner_id = owner_id if owner_id is not None else str(uuid4())

    executor = PublicationExecutor(
        store,
        owner_id=runtime_owner_id,
        secret_resolver=secret_resolver,
        gateway_factory=gateway_factory,
        command_service=PublishCommandService(store),
        clock=lambda: clock().isoformat(),
        gateway_timeout=timeout,
        stale_seconds=stale,
    )
    reconciler = PublicationReconciler(
        store,
        owner_id=runtime_owner_id,
        secret_resolver=secret_resolver,
        gateway_factory=gateway_factory,
        clock=clock,
        gateway_timeout=timeout,
    )

    return PublicationWorker(
        store,
        executor=executor,
        reconciler=reconciler,
        workspace_id=workspace_id,
        clock=clock,
        stale_seconds=stale,
        owner_id=runtime_owner_id,
    )


def run_publication_worker(
    worker: PublicationWorker,
    poll_seconds: float = 1.0,
    run_once: bool = False,
) -> int:
    """Run the publication runtime with the content Worker's signal semantics.

    Exit codes match ``service.worker_bootstrap.run_worker`` so an operator sees
    one convention: 0 normal, 2 fatal runtime error.
    """
    stop = threading.Event()

    def _install_signal_handlers():
        try:
            signal.signal(signal.SIGINT, lambda *_: stop.set())
            signal.signal(signal.SIGTERM, lambda *_: stop.set())
        except (AttributeError, ValueError):
            pass

    _install_signal_handlers()

    try:
        if run_once:
            worker.run_once()
            return 0
        worker.run(stop, poll_seconds=poll_seconds)
        return 0
    except PublicationBootstrapError:
        raise
    except Exception as error:
        # Type only. A publication failure can carry a transport or credential
        # detail in its message, and this frame is the outermost one.
        log.error('Publication worker loop failed (%s)', type(error).__name__)
        raise PublicationBootstrapError('Publication worker loop failed') from error


__all__ = [
    'PublicationBootstrapError',
    'build_publication_worker',
    'run_publication_worker',
    'DEFAULT_PUBLICATION_GATEWAY_TIMEOUT',
]
