"""PublicationExecutor: one durable claim, at most one remote create.

This is the first place the publication contracts meet. It owns no authority of
its own -- it holds together components that already exist and refuses to invent
anything when one of them says no.

Transaction discipline
----------------------
Three short transactions surround exactly one network call:

    claim (commit)
      -> local validation, no transaction
      -> may_send_at (commit)
      -> gateway.publish()          <-- no transaction open here
      -> terminal write (commit)

No database transaction is ever open across ``gateway.publish``. The store
refuses writes outside an explicit transaction and the claim returns a lease
before the claim commits, so the ordering below is not a convention that can be
forgotten -- it is the only shape the repository API permits.

Ordering within that shape is not cosmetic. ``mark_publication_may_send`` is the
durable boundary that separates a crash which provably never attempted a create
from one that may have. It is committed *before* the gateway call, never after,
because a create that is sent and then lost is indistinguishable from one that
was never sent unless the marker was already durable.

Local failures are FAILED, not INDETERMINATE
--------------------------------------------
Everything checked before the may-send boundary is provably local: the target
was missing, disabled, drifted, or the credential could not be resolved. No
request was sent, so the remote outcome is known -- nothing was created. Writing
INDETERMINATE there would assert a remote uncertainty that does not exist and
would send the publication through needless reconciliation. Every one of those
failures is therefore persisted under its own specific safe code.

What this module deliberately does not do
-----------------------------------------
It does not retry, does not reconcile, does not project onto Task status, does
not create TaskRun rows, does not expire other publications' leases, and does
not run a polling loop. ``run_once`` processes at most one publication.
"""
from __future__ import annotations

import logging

from domain.contracts import ContentType
from domain.publication import (
    PublicationLease,
    PublishCommand,
    PublishCommandUnavailable,
    PublishOutcome,
    PublishOutcomeKind,
)
from domain.publishing_target import PublishingProviderType, TargetStatus
from publishing.secret_resolver import SecretResolver, SecretUnavailable
from publishing.wordpress import WordPressConnection
from publishing.wordpress import WordPressGateway
from service.execution_timing import (
    DEFAULT_SAFETY_MARGIN_SECONDS,
    UnsafeExecutionTiming,
    validate_execution_timing,
)

log = logging.getLogger(__name__)

# Retired safe codes for locally-provable pre-network failures. Each names the
# condition precisely so an operator is not sent to look at WordPress permissions
# for what is really a local configuration problem.
TARGET_SNAPSHOT_MISSING = 'LEGACY_TARGET_SNAPSHOT_MISSING'
TARGET_NOT_FOUND = 'TARGET_NOT_FOUND'
TARGET_DISABLED = 'TARGET_DISABLED'
TARGET_CONFIGURATION_DRIFT = 'TARGET_CONFIGURATION_DRIFT'
UNSUPPORTED_PROVIDER = 'UNSUPPORTED_PUBLISHING_PROVIDER'
CREDENTIAL_UNAVAILABLE = 'CREDENTIAL_UNAVAILABLE'
CONNECTION_INVALID = 'CONNECTION_INVALID'
COMMAND_INVALID = 'PUBLISH_COMMAND_INVALID'

# Used only when the gateway raises something the contract did not anticipate.
# By then may_send_at is committed, so the remote outcome cannot be proven and
# claiming FAILED would be a lie.
EXECUTOR_FAILED = 'EXECUTOR_FAILED'


class LocalValidationError(Exception):
    """A failure provable before any request was sent.

    Carries only a safe code. The underlying condition, any exception text, and
    any credential detail stay out of the message so this can be logged and
    persisted without leaking anything.
    """

    def __init__(self, safe_code: str):
        super().__init__(safe_code)
        self.safe_code = safe_code


class PublicationExecutor:
    """Processes at most one claimable publication per run_once."""

    def __init__(self, store, *, owner_id: str, secret_resolver: SecretResolver,
                 gateway_factory, command_service, clock, gateway_timeout: float,
                 stale_seconds: float,
                 safety_margin: float = DEFAULT_SAFETY_MARGIN_SECONDS) -> None:
        # Refuse to exist if a lease could normally expire during one gateway
        # call. See service/execution_timing for why the nominal timeout is not
        # the bound.
        self.gateway_timeout_bound = validate_execution_timing(
            gateway_timeout, stale_seconds, safety_margin)
        if type(owner_id) is not str or not owner_id.strip():
            raise ValueError('owner_id is required')
        if not isinstance(secret_resolver, SecretResolver):
            raise ValueError('secret_resolver must satisfy the SecretResolver protocol')
        if not callable(gateway_factory):
            raise ValueError('gateway_factory must be callable')
        self._store = store
        self._owner_id = owner_id
        self._secret_resolver = secret_resolver
        self._gateway_factory = gateway_factory
        self._command_service = command_service
        self._clock = clock
        self._gateway_timeout = gateway_timeout
        self._stale_seconds = stale_seconds

    # -- public API --------------------------------------------------------

    def run_once(self, workspace_id: str) -> int:
        """Process at most one publication. Returns 0 for no work, 1 for one.

        A publication that was claimed counts as processed even when it ends
        SUCCEEDED, FAILED, INDETERMINATE, or when processing stops because the
        lease was lost. Once a claim is committed the publication is consumed
        rather than thrown back, so a caller cannot accidentally spin on it.

        Only a failure before any claim propagates. After a claim the executor
        consumes the publication safely, because raising here could carry
        internal or credential-bearing detail to an outer loop that has no way to
        classify it.
        """
        with self._store.workspace_transaction(workspace_id) as repo:
            lease = repo.claim_publication(self._owner_id, self._now())
        if lease is None:
            return 0
        # The claim is durable now and its transaction is closed. Nothing below
        # runs while a transaction is open until the short one it owns.
        try:
            self._execute(workspace_id, lease)
        except Exception:
            # may_send_at may already be committed, so the remote outcome is not
            # provable. Never FAILED here; never retry.
            self._consume(workspace_id, lease, 'INDETERMINATE', EXECUTOR_FAILED,
                          reason='executor_error')
        return 1

    # -- execution ---------------------------------------------------------

    def _execute(self, workspace_id: str, lease: PublicationLease) -> None:
        try:
            connection = self._validate_locally(workspace_id, lease)
            command = self._build_command(lease)
        except LocalValidationError as failure:
            log.info("publication %s rejected before network: %s",
                     lease.publication_id, failure.safe_code)
            self._consume(workspace_id, lease, 'FAILED', failure.safe_code)
            return

        # The durable may-send boundary, in its own short transaction. This is
        # the last gate before a remote side effect: if it fails, ownership was
        # lost and nothing may be sent.
        with self._store.workspace_transaction(workspace_id) as repo:
            marked = repo.mark_publication_may_send(lease, self._now())
        if not marked:
            log.info("publication %s stopped: lease no longer owns it", lease.publication_id)
            return

        # No transaction is open here. The gateway call is the only network I/O.
        gateway = self._gateway_factory(connection, self._gateway_timeout)
        try:
            outcome = gateway.publish(command)
        except Exception:
            # Unexpected gateway fault after the boundary. The remote outcome is
            # unprovable, so this is INDETERMINATE and never a retry. The
            # exception text is deliberately not logged: it may carry connection
            # detail. The safe code carries the whole meaning.
            log.warning("publication %s gateway raised after may-send boundary",
                        lease.publication_id)
            self._consume(workspace_id, lease, 'INDETERMINATE', EXECUTOR_FAILED,
                          reason='gateway_exception')
            return

        self._persist_outcome(workspace_id, lease, command, outcome)

    def _validate_locally(self, workspace_id: str, lease: PublicationLease):
        """Every check that must pass before a create may be attempted.

        Reads the target by its SNAPSHOTTED id. The currently-active target is
        never consulted: the snapshot is the publication's authority, and
        re-resolving it would let an administrator repoint a workspace and
        silently redirect a publication that was already approved for another
        site.
        """
        with self._store.workspace_reader(workspace_id) as repo:
            if not repo.assert_publication_ownership(lease):
                raise LocalValidationError(COMMAND_INVALID)
            publication = repo.get_publication(lease.publication_id)
            if publication is None or publication.task_id != lease.task_id:
                raise LocalValidationError(COMMAND_INVALID)
            if publication.target_id is None or publication.target_configuration_version is None:
                # A row that predates publishing targets. Non-executable by
                # contract; it must never be given a fabricated destination.
                raise LocalValidationError(TARGET_SNAPSHOT_MISSING)
            target = repo.get_publishing_target(publication.target_id)
            if target is None:
                # Missing, foreign workspace, or archived workspace are
                # deliberately indistinguishable.
                raise LocalValidationError(TARGET_NOT_FOUND)
            if target.workspace_id != publication.workspace_id:
                raise LocalValidationError(TARGET_NOT_FOUND)
            if target.provider_type is not PublishingProviderType.WORDPRESS:
                raise LocalValidationError(UNSUPPORTED_PROVIDER)
            if target.status is not TargetStatus.ACTIVE:
                raise LocalValidationError(TARGET_DISABLED)
            if target.configuration_version != publication.target_configuration_version:
                raise LocalValidationError(TARGET_CONFIGURATION_DRIFT)

        try:
            secret = self._secret_resolver.resolve(target.credential_reference)
        except SecretUnavailable:
            raise LocalValidationError(CREDENTIAL_UNAVAILABLE) from None
        except Exception:
            # A resolver fault is still a local failure; no request was made.
            raise LocalValidationError(CREDENTIAL_UNAVAILABLE) from None

        try:
            return WordPressConnection(base_url=target.base_url, username=target.username,
                                       application_password=secret)
        except Exception:
            # Never surface the message: it can embed the offending URL.
            raise LocalValidationError(CONNECTION_INVALID) from None

    def _build_command(self, lease: PublicationLease) -> PublishCommand:
        """Build the exact command through the existing authority chain.

        PublishCommandService opens its own transaction, so it must not be called
        from inside one; every caller here is outside a transaction by construction.
        """
        try:
            return self._command_service.build(lease)
        except PublishCommandUnavailable:
            raise LocalValidationError(COMMAND_INVALID) from None
        except Exception:
            raise LocalValidationError(COMMAND_INVALID) from None

    def _persist_outcome(self, workspace_id: str, lease: PublicationLease,
                         command: PublishCommand, outcome: PublishOutcome) -> None:
        """Map the gateway's classification onto the durable terminal state."""
        if not isinstance(outcome, PublishOutcome):
            self._consume(workspace_id, lease, 'INDETERMINATE', 'INVALID_RESPONSE',
                          reason='bad_outcome')
            return

        if outcome.kind is PublishOutcomeKind.CONFIRMED_SUCCESS:
            reference = outcome.remote
            # Fail closed on an inconsistent success rather than trusting it.
            # The remote resource exists on some collection; recording the wrong
            # collection would poison later reconciliation.
            if (reference is None
                    or reference.content_type is not command.content_type):
                log.warning("publication %s success outcome inconsistent with command",
                            lease.publication_id)
                self._consume(workspace_id, lease, 'INDETERMINATE', 'INVALID_RESPONSE',
                              reason='inconsistent_success')
                return
            with self._store.workspace_transaction(workspace_id) as repo:
                persisted = repo.complete_publication(
                    lease, reference.remote_resource_id, reference.remote_url, self._now())
            if not persisted:
                # Ownership was lost after a successful create. The resource
                # exists remotely with no local success recorded. That is a
                # reconciliation problem for 3C6, and retrying would duplicate it.
                log.warning("publication %s remote success lost fencing; "
                            "reconciliation required", lease.publication_id)
            return

        if outcome.kind is PublishOutcomeKind.CONFIRMED_FAILURE:
            self._consume(workspace_id, lease, 'FAILED', outcome.error_code or 'UNKNOWN')
            return

        self._consume(workspace_id, lease, 'INDETERMINATE',
                      outcome.error_code or 'UNKNOWN')

    def _consume(self, workspace_id: str, lease: PublicationLease, state: str,
                 error_code: str | None, reason: str | None = None) -> None:
        """Write a terminal state under the full lease predicate.

        A False result means ownership was already lost. Nothing is recovered and
        nothing is overwritten: a newer executor owns the row, and its state is
        the truth.
        """
        if reason is not None:
            log.info("publication %s stopping: %s", lease.publication_id, reason)
        # SUCCEEDED is deliberately unreachable here: this method carries no
        # remote evidence, and inventing an id would fabricate a confirmation.
        if state == 'SUCCEEDED':
            raise AssertionError('success must be persisted with real remote evidence')
        with self._store.workspace_transaction(workspace_id) as repo:
            if state == 'FAILED':
                persisted = repo.fail_publication(lease, error_code, self._now())
            else:
                persisted = repo.mark_publication_indeterminate(
                    lease, error_code or 'UNKNOWN', self._now())
        if not persisted:
            log.info("publication %s terminal write refused; lease was lost",
                     lease.publication_id)

    def _now(self) -> str:
        return self._clock()


__all__ = [
    "LocalValidationError",
    "PublicationExecutor",
    "UnsafeExecutionTiming",
]
