"""The secret-resolution boundary.

A SecretResolver turns a credential *reference* into secret *material*. That is
the whole contract, and it is deliberately the narrowest possible interface so
that every place a secret can exist is a place an auditor can find.

This module imports no ``os`` and touches no persistence. A resolver must not
read the database, must not mutate publication state, and must not make a
network call, because the material it returns is a live credential and the
narrower this interface is, the fewer places it can leak from.

The returned ``str`` is intentionally a bare string rather than a dataclass. A
wrapper type would be convenient, but every wrapper eventually needs a ``repr``,
and a ``repr`` for a secret is a liability: it is one ``print`` or one traceback
away from a log. A bare ``str`` is not accidentally printable in a structured
record, and callers hold it in a local variable that they must pass straight to
the transport.
"""
from typing import Protocol, runtime_checkable

from domain.credential_reference import validate_credential_reference


class SecretUnavailable(RuntimeError):
    """A credential reference could not be resolved to usable material.

    Carries no secret, no reference value, and no environment name: a resolver
    failure is a configuration problem, and the configuration is often the thing
    being protected.
    """


@runtime_checkable
class SecretResolver(Protocol):
    """Resolves a credential reference to secret material."""

    def resolve(self, reference: str) -> str:
        """Return the secret named by ``reference``.

        Must validate the reference against the shared grammar before using it,
        and must fail closed on a missing or blank secret rather than returning
        an empty string. An empty credential is worse than an absent one: it can
        be sent to a remote and produce an authentication attempt that looks
        configured but is not.
        """
        ...


__all__ = ['SecretResolver', 'SecretUnavailable', 'validate_credential_reference']
