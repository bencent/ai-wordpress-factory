"""Environment-backed SecretResolver adapter.

This is the FIRST resolver, not the publishing architecture's assumption about
where secrets live. It reads the process environment, which is exactly why it
lives in its own module: ``publishing/secret_resolver.py`` stays import-clean of
``os`` so that a future secret store can replace this adapter without any domain
or repository code changing.

The environment is process-global, so this adapter cannot by itself isolate one
workspace's secret from another's. That is a known property of the adapter, not
a property of the publishing contract: what keeps tenants apart is that the
reference itself is stored workspace-scoped on the target row, so the resolver is
only ever asked for a reference that the workspace already owns.

Nothing here logs. A resolver is exactly the kind of code where a
``logger.debug("resolved %s", secret)`` is one careless line away, and the
failure mode is a credential in a log file that outlives the deployment.
"""
import os

from domain.credential_reference import ENV_REFERENCE_PREFIX, validate_credential_reference
from publishing.secret_resolver import SecretUnavailable


class EnvironmentSecretResolver:
    """Resolves ``env:NAME`` references from the process environment."""

    def resolve(self, reference: str) -> str:
        validate_credential_reference(reference)
        secret = os.environ.get(reference[len(ENV_REFERENCE_PREFIX):])
        # A blank value is treated as absent, and whitespace counts as blank. An
        # empty or padded credential would still produce a real authentication
        # attempt against the remote, which looks like a configured-but-rejected
        # credential rather than a missing one, and sends an operator hunting for
        # a permission problem they do not have.
        if type(secret) is not str or not secret.strip():
            raise SecretUnavailable('credential reference could not be resolved')
        return secret


__all__ = ['EnvironmentSecretResolver']
