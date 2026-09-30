"""Execution timing policy for single-threaded publication execution.

3C5C is single-threaded by decision: there is no background heartbeat, so an
IN_PROGRESS lease is only ever refreshed between publications, never during one.
That makes the lease timeout and the gateway call duration directly comparable,
and the relationship between them a correctness invariant rather than a tuning
detail.

Why the nominal gateway timeout is not the bound
-----------------------------------------------
``requests`` resolves a scalar ``timeout`` into ``connect=timeout`` and
``read=timeout`` and urllib3 applies them to their own phases independently. A
single ``publish()`` can therefore occupy up to ``CONNECT + READ``, not one
timeout. Measured against the installed library:

    requests.adapters.HTTPAdapter.send
        connect, read = timeout
        resolved_timeout = TimeoutSauce(connect=connect, read=read)

With a 30 second timeout that is a 60 second worst case, which is exactly the
stale threshold the worker defaults suggest. Treating "30 < 60" as sufficient
would be wrong by construction, so the policy below compares against the
derived worst case, not the nominal value.

Residual risk, stated rather than hidden
----------------------------------------
DNS resolution happens in ``socket.getaddrinfo`` before any socket timeout
applies, so it is not bounded by this policy. A hung resolver can outlive any
margin. That is why 3C5C does not add a thread as a workaround: a thread would
hide the problem rather than bound it, and the honest response is a documented
limit plus INDETERMINATE semantics for whatever the transport cannot prove.
"""
from __future__ import annotations

# Fraction of a single phase budget that may elapse in each of the two phases.
# 1.0 is the real worst case; a small headroom factor keeps the derived bound
# above the theoretical maximum rather than exactly equal to it.
PHASE_HEADROOM = 1.0

DEFAULT_SAFETY_MARGIN_SECONDS = 5.0


class UnsafeExecutionTiming(ValueError):
    """The lease could normally expire during one gateway call.

    Raised instead of starting an executor that can lose its own lease mid-call.
    Losing the lease is survivable -- the fenced write simply returns False -- but
    a successful remote create would then be orphaned with no local success, which
    is exactly the case reconciliation exists to repair. Refusing to construct the
    executor is cheaper and more honest than discovering it in production.
    """


def worst_case_gateway_seconds(gateway_timeout: float) -> float:
    """Upper bound on one publish() call, derived from the transport's semantics.

    Connect and read are budgeted independently, so the total is their sum. A
    scalar timeout is what WordPressGateway passes through today.
    """
    if not isinstance(gateway_timeout, (int, float)) or isinstance(gateway_timeout, bool):
        raise ValueError("gateway_timeout must be a number")
    if gateway_timeout <= 0:
        raise ValueError("gateway_timeout must be positive")
    return 2.0 * gateway_timeout * PHASE_HEADROOM


def validate_execution_timing(gateway_timeout: float, stale_seconds: float,
                              safety_margin: float = DEFAULT_SAFETY_MARGIN_SECONDS) -> float:
    """Return the worst-case call duration, or refuse the configuration.

    Requires ``stale_seconds >= worst_case + margin``. Equality is accepted only
    because the margin is what makes it safe; without a positive margin the
    comparison would permit a lease that expires exactly at the boundary.
    """
    if not isinstance(stale_seconds, (int, float)) or isinstance(stale_seconds, bool):
        raise ValueError("stale_seconds must be a number")
    if stale_seconds <= 0:
        raise ValueError("stale_seconds must be positive")
    if not isinstance(safety_margin, (int, float)) or isinstance(safety_margin, bool):
        raise ValueError("safety_margin must be a number")
    if safety_margin <= 0:
        raise ValueError("safety_margin must be positive so the bound is not met exactly")

    bound = worst_case_gateway_seconds(gateway_timeout)
    if stale_seconds < bound + safety_margin:
        raise UnsafeExecutionTiming(
            "publication stale timeout must exceed the worst-case gateway call "
            "duration plus a safety margin")
    return bound


__all__ = [
    "DEFAULT_SAFETY_MARGIN_SECONDS",
    "UnsafeExecutionTiming",
    "PHASE_HEADROOM",
    "validate_execution_timing",
    "worst_case_gateway_seconds",
]
