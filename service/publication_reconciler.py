"""Read-only investigation of INDETERMINATE publications.

This worker answers exactly one question, and only ever in one direction: *did a
remote resource appear whose stored content carries this publication's marker?*

It is evidence gathering. It never creates, updates, or deletes anything on
WordPress. The only gateway call permitted is ``find_by_marker``, which is a
sequence of authenticated GETs. There is no path from this module to
``WordPressGateway.publish``, and tests assert that at the HTTP-method level
rather than trusting the code to stay honest.

Why nothing here can say FAILED
-------------------------------
A scan that observes zero exact marker matches has not proven the resource is
absent. The marker may have been stripped by a sanitizer, the post may be in the
trash, the credentials may have been narrowed, or pagination may have been cut
short. Every outcome that is not a positive proof of presence therefore leaves
the publication INDETERMINATE, and the only terminal transition available is
INDETERMINATE -> SUCCEEDED on a single exact, correctly-typed match.

``classify_reconciliation`` returns a member named ``NOT_FOUND`` for an empty
match set. That is a statement about what the scan SAW, not about what exists,
and it must never be read as a publication failure. This module keeps the two
apart by routing every non-success through ``complete_reconciliation_unresolved``,
which cannot write FAILED.

Transaction discipline
----------------------
The same rule 3C5C established for the create path: no database transaction may
remain open across network I/O. Claim commits, the scan runs with no transaction
at all, and the result is written in a fresh short transaction. Holding a write
lock across a multi-page scan would block every other writer in the database for
as long as the scan took.
"""
from __future__ import annotations

import logging

from domain.publication import (
    PublicationState,
    ReconciliationMatch,
    SAFE_RECONCILIATION_ERROR_CODES,
    classify_reconciliation,
    reconciliation_marker,
)
from domain.publishing_target import PublishingProviderType
from publishing.secret_resolver import SecretResolver, SecretUnavailable
from publishing.wordpress import WordPressConnection

log = logging.getLogger(__name__)

# The six safe diagnostics, bound locally so a typo becomes an AttributeError at
# import time rather than a rejected write at run time.
_ZERO_MATCH = 'RECONCILIATION_ZERO_MATCH'
_AMBIGUOUS = 'RECONCILIATION_AMBIGUOUS'
_TYPE_MISMATCH = 'RECONCILIATION_CONTENT_TYPE_MISMATCH'
_UNAVAILABLE = 'RECONCILIATION_UNAVAILABLE'
_AUTHORIZATION = 'RECONCILIATION_AUTHORIZATION'
_TARGET_UNAVAILABLE = 'RECONCILIATION_TARGET_UNAVAILABLE'

assert {_ZERO_MATCH, _AMBIGUOUS, _TYPE_MISMATCH, _UNAVAILABLE, _AUTHORIZATION,
        _TARGET_UNAVAILABLE} == set(SAFE_RECONCILIATION_ERROR_CODES), \
    "a local diagnostic is not in the persisted vocabulary"


class ReconciliationUnavailable(Exception):
    """A local precondition failed; the attempt cannot proceed.

    Raised only for conditions that are known BEFORE any network call, so a
    caller can distinguish "we did not look" from "we looked and could not
    tell". Its own text is never persisted or logged.
    """


class PublicationReconciler:
    """Consume at most one explicitly requested reconciliation claim.

    Mirrors ``PublicationExecutor.run_once`` deliberately -- injected
    dependencies, no work signal as ``0``, one unit of work as ``1`` -- but
    shares none of its semantics. The executor owns a CREATE attempt and may
    move a publication to IN_PROGRESS; this owns a read-only investigation and
    leaves the publication INDETERMINATE unless it can prove success.

    One call handles at most one claim. There is no loop, no sleep, no
    scheduler and no background thread here or anywhere it is called from:
    a second attempt requires a second explicit operator request, which
    3C6C's request-consumption semantics make durable.
    """

    def __init__(self, store, *, owner_id: str, secret_resolver: SecretResolver,
                 gateway_factory, clock, gateway_timeout: float = 30.0):
        self._store = store
        self._owner_id = owner_id
        self._secret_resolver = secret_resolver
        self._gateway_factory = gateway_factory
        self._clock = clock
        self._gateway_timeout = gateway_timeout

    # -- public surface ----------------------------------------------------

    def run_once(self, workspace_id: str) -> int:
        """Claim and investigate at most one requested publication.

        Returns 0 when nothing was claimable and 1 when one claim was consumed,
        regardless of how the attempt ended. Fencing loss, a missing historical
        destination, an unreachable site and a proven success are all "one unit
        of work done"; the caller learns which by reading the publication row,
        not from this number.
        """
        # The claim commits at the closing of this block. Nothing below it may
        # run while a write transaction is open.
        with self._store.workspace_transaction(workspace_id) as repo:
            lease = repo.claim_publication_for_reconciliation(self._owner_id, self._now())
        if lease is None:
            return 0
        self._investigate(workspace_id, lease)
        return 1

    # -- investigation -----------------------------------------------------

    def _investigate(self, workspace_id: str, lease) -> None:
        """Run one attempt. Every exit path leaves the claim resolved or released.

        The blanket ``except`` is the safety net that Section N depends on: a
        bug in any step below must not leave a claim stuck forever, must not
        write FAILED, must not resolve SUCCEEDED, and must not re-arm the
        request. Finishing unresolved satisfies all four.
        """
        try:
            self._attempt(workspace_id, lease)
        except Exception as error:  # noqa: BLE001 - deliberate catch-all
            # The exception text is deliberately not logged: a transport or
            # resolver failure can carry connection or credential detail. The
            # class name and a safe code carry the whole meaning.
            log.warning("reconciliation %s attempt raised %s",
                        lease.publication_id, type(error).__name__)
            self._finish_unresolved(lease, _UNAVAILABLE)

    def _attempt(self, workspace_id: str, lease) -> None:
        publication = self._load_publication(lease)
        if publication is None:
            return  # ownership or state was already lost; nothing to do

        evidence = self._load_historical_target(publication)
        if evidence is None:
            self._finish_unresolved(lease, _TARGET_UNAVAILABLE)
            return

        connection = self._build_connection(evidence)
        if connection is None:
            self._finish_unresolved(lease, _UNAVAILABLE)
            return

        # Re-assert ownership immediately before the network call. A claim can be
        # lost between the reads above -- an expiry sweep, or an archived
        # workspace -- and there is no point searching a remote site for a claim
        # this worker no longer holds. See _finish_unresolved for the archived
        # case, which cannot be completed and is released by expiry instead.
        if not self._heartbeat(lease):
            return

        # ---- no database transaction is open from here to the write below ----
        gateway = self._gateway_factory(connection, self._gateway_timeout)
        matches = self._lookup(gateway, publication)
        # ------------------------------------------------------------------------

        self._classify_and_persist(lease, publication, matches)

    # -- local authority ---------------------------------------------------

    def _load_publication(self, lease):
        """Load the exact publication this lease names, or None if it is gone.

        The publication is the authority. Nothing here consults a task, a latest
        content version, an idempotency key, or the currently-active target: a
        publication that snapshotted a destination is reconciled against THAT
        destination, and inferring anything else would answer a different
        question than the one that was asked.
        """
        with self._store.workspace_reader(lease.workspace_id) as repo:
            if not repo.assert_reconciliation_ownership(lease):
                return None
            publication = repo.get_publication(lease.publication_id)
        if publication is None:
            return None
        if publication.state is not PublicationState.INDETERMINATE:
            # Someone resolved it between the claim and now.
            return None
        if publication.may_send_at is None:
            # A create was provably never attempted, so there is nothing to find.
            return None
        if publication.target_id is None or publication.target_configuration_version is None:
            return None
        return publication

    def _load_historical_target(self, publication):
        """Resolve the EXACT destination this publication was sent to, or None.

        Uses the ACTIVE-agnostic evidence read: "does this configuration exist"
        and "may this workspace do network work" are different questions, and
        conflating them would let an archived workspace be reported as a missing
        remote destination.

        There is deliberately no fallback. Not to the current target, not to the
        active one, not to the nearest or latest version. The current
        configuration may sit at v9 while the publication used v2, and searching
        v9 would scan a different site -- where a single match would be a false
        success and a zero-match a fabricated absence.
        """
        with self._store.workspace_reader(publication.workspace_id) as repo:
            evidence = repo.get_publishing_target_version_evidence(
                publication.target_id, publication.target_configuration_version)
        if evidence is None:
            return None
        if evidence.provider_type is not PublishingProviderType.WORDPRESS:
            return None
        return evidence

    def _build_connection(self, evidence):
        """Construct a connection from the HISTORICAL values, or None.

        The secret is resolved from the historical ``credential_reference``, not
        the current target's. Rotation behind an unchanged reference therefore
        works for free -- the same property execution has always had.

        Every failure below maps to ``RECONCILIATION_UNAVAILABLE``, and
        ``RECONCILIATION_AUTHORIZATION`` is deliberately NOT used here.
        That code is defined as "we were not permitted to look": a 401, a 403,
        or a ``content.raw`` we were not entitled to. A missing local environment
        variable is not a permission decision by WordPress, and labelling it
        AUTHORIZATION would invent a remote fact that never happened. The lookup
        simply could not be attempted, which is what UNAVAILABLE means.

        ``RECONCILIATION_AUTHORIZATION`` is therefore currently unreachable from
        this worker: the gateway raises ``ReconciliationLookupUnresolved`` for
        401 and 403 exactly as it does for a 5xx, with no structured reason to
        read. Distinguishing them would mean changing the gateway's failure
        contract for a diagnostic improvement, which is more invasive than this
        slice should be, and the outcome is identical either way --
        INDETERMINATE.
        """
        try:
            secret = self._secret_resolver.resolve(evidence.credential_reference)
        except SecretUnavailable:
            # Local inability to obtain a credential is NOT an authorization
            # failure at the remote end, and RECONCILIATION_AUTHORIZATION is
            # defined as "we were not permitted to look". UNAVAILABLE is the
            # honest word: the lookup could not be attempted.
            return None
        except Exception as error:  # noqa: BLE001
            log.warning("secret resolution raised %s for reconciliation",
                        type(error).__name__)
            return None
        if type(secret) is not str or not secret.strip():
            return None
        try:
            return WordPressConnection(base_url=evidence.base_url,
                                       username=evidence.username,
                                       application_password=secret)
        except Exception as error:  # noqa: BLE001
            # The message can embed the offending URL, so it is never surfaced.
            log.warning("connection construction raised %s for reconciliation",
                        type(error).__name__)
            return None

    # -- remote lookup -----------------------------------------------------

    def _lookup(self, gateway, publication):
        """The one permitted remote call: an authenticated read-only scan.

        Returns the matches, or raises. Every failure mode -- DNS, TLS,
        connect, read timeout, reset, 429, 5xx, malformed JSON, a truncated
        pagination walk, a missing ``content.raw``, a 401, a 403 -- surfaces as
        ``ReconciliationLookupUnresolved``, and none of them distinguishes
        itself. The caller therefore records UNAVAILABLE for all of them.

        That is not a loss of fidelity being papered over: the gateway raises
        rather than returning an empty tuple precisely so a failed scan cannot
        be mistaken for a complete one. The distinction that would matter --
        "we looked and found nothing" versus "we could not look" -- is preserved
        by the exception, which is why 401 and 5xx are both UNAVAILABLE here
        instead of guessing from message text.
        """
        return gateway.find_by_marker(reconciliation_marker(publication.publication_id))

    # -- classification and persistence ------------------------------------

    def _classify_and_persist(self, lease, publication, matches) -> None:
        """Decide from the evidence, and write exactly one durable outcome."""
        try:
            result, references = classify_reconciliation(matches)
        except Exception as error:  # noqa: BLE001
            # A malformed match set is not evidence of success. Fails closed.
            log.warning("match classification raised %s for reconciliation",
                        type(error).__name__)
            self._finish_unresolved(lease, _UNAVAILABLE)
            return

        if result is ReconciliationMatch.NOT_FOUND:
            # A COMPLETE scan observed zero exact matches. That is an observation
            # about the scan, not proof of absence, so the publication stays
            # INDETERMINATE and stays un-claimable until someone asks again.
            self._finish_unresolved(lease, _ZERO_MATCH)
            return

        if result is ReconciliationMatch.AMBIGUOUS:
            # Choosing one of several candidates would hide that the side effect
            # may have happened more than once. No ordering preference is
            # applied: not lowest id, not highest, not expected type, not the one
            # with a URL.
            self._finish_unresolved(lease, _AMBIGUOUS)
            return

        reference = references[0]
        if reference.content_type is not publication.content_type:
            # The resource exists, but on a collection this publication did not
            # target. That is a real finding and not this publication's success.
            self._finish_unresolved(lease, _TYPE_MISMATCH)
            return

        if reference.remote_resource_id <= 0:
            self._finish_unresolved(lease, _UNAVAILABLE)
            return

        # Every condition for a positive proof of presence is now met, and the
        # call below re-checks ownership. It is the ONLY path in this module
        # that can move a publication out of INDETERMINATE.
        self._resolve_succeeded(lease, reference)

    def _resolve_succeeded(self, lease, reference) -> None:
        with self._store.workspace_transaction(lease.workspace_id) as repo:
            persisted = repo.resolve_reconciliation_succeeded(
                lease, reference.remote_resource_id, reference.remote_url, self._now())
        if persisted:
            log.info("reconciliation %s resolved SUCCEEDED to remote id %s",
                     lease.publication_id, reference.remote_resource_id)
        else:
            # Ownership was lost while the scan ran. The newer owner or state
            # stands; this worker writes nothing and retries nothing.
            log.info("reconciliation %s lost fencing before resolution",
                     lease.publication_id)

    def _finish_unresolved(self, lease, error_code) -> None:
        """Record a safe diagnostic and release the claim, keeping INDETERMINATE.

        The request stays consumed. A second lookup requires a second explicit
        request, which is what stops an operator-triggered design from becoming
        an automatic retry loop.

        If ownership is already gone this is a no-op, and that is correct: a
        stale worker must not overwrite a newer owner's diagnostic.
        """
        with self._store.workspace_transaction(lease.workspace_id) as repo:
            persisted = repo.complete_reconciliation_unresolved(
                lease, error_code, self._now())
        if persisted:
            log.info("reconciliation %s unresolved: %s",
                     lease.publication_id, error_code)
        else:
            log.info("reconciliation %s lost ownership before completion",
                     lease.publication_id)

    def _heartbeat(self, lease) -> bool:
        """Refresh the lease and re-confirm ownership, immediately pre-network.

        One heartbeat, immediately before the scan, is the smallest design that
        keeps the existing stale semantics honest without adding a thread.

        It is NOT sufficient for an arbitrarily long scan: 3C5C established the
        HTTP timeout is per-phase, so a many-page lookup has no fixed upper
        bound, and a scan that outlasts the stale window will lose fencing. That
        is an OPERATIONAL CONSTRAINT, not a correctness hole -- the outcome is a
        CAS failure, the remote id is not persisted by a stale worker, and the
        request stays consumed. Beating it would need a heartbeat between page
        requests, which means changing the gateway's lookup API, and that is
        more invasive than this slice should be.
        """
        with self._store.workspace_transaction(lease.workspace_id) as repo:
            return repo.heartbeat_reconciliation(lease, self._now())

    def _now(self) -> str:
        return self._clock()


__all__ = ['PublicationReconciler', 'ReconciliationUnavailable']
