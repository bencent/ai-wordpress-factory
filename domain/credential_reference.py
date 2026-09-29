"""The single credential-reference grammar.

A credential reference is a POINTER to secret material, never the material
itself. The repository already established this shape for AI providers: the
``ai_provider_connections.credential_reference`` column is constrained in SQL so
that it cannot hold a secret (``GLOB 'env:[A-Z]*'`` plus a character-class check),
and :class:`domain.providers.AIProviderConnection` re-validates the same pattern
in Python.

This module is the one place that pattern is written down. Publishing targets
must not invent a second, looser grammar: a reference accepted by one domain and
rejected by another would make "the row was valid at write time and invalid at
resolve time" reachable, which is exactly the class of bug that turns a
configuration problem into an INDETERMINATE publication.

Only ``env:NAME`` is defined here. That is the first adapter, not a promise that
environment variables are the final SaaS secret store: the grammar describes how
a reference is *named*, and a future secret store adds new reference forms
without touching any domain that depends on this contract.
"""
import re

# Uppercase shell-style name. Deliberately bounded so a reference cannot smuggle
# path separators, spaces, or shell metacharacters into a resolver.
CREDENTIAL_REFERENCE_PATTERN = re.compile(r'env:[A-Z][A-Z0-9_]{0,127}')

ENV_REFERENCE_PREFIX = 'env:'


class CredentialReferenceError(ValueError):
    """A credential reference does not match the supported grammar.

    The message names the field and never the value, so a malformed reference
    cannot leak into a log line or an error response.
    """


def validate_credential_reference(value, field='credential_reference'):
    """Return the reference unchanged, or raise CredentialReferenceError.

    Validating here means a PublishingTarget can never be constructed holding a
    reference a resolver would later refuse, so target rows and secret lookups
    agree by construction rather than by convention.
    """
    if type(value) is not str or not CREDENTIAL_REFERENCE_PATTERN.fullmatch(value):
        raise CredentialReferenceError(f'{field} must be a supported credential reference')
    return value


def is_credential_reference(value):
    return type(value) is str and CREDENTIAL_REFERENCE_PATTERN.fullmatch(value) is not None
