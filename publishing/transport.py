"""Narrow HTTP transport seam for the WordPress gateway.

The gateway is tested without a live WordPress, so the wire is expressed as two
tiny protocols plus a response value. Tests inject a deterministic fake; the
production adapter wraps ``requests`` once, here, and nowhere else.

Why the seam carries transmission state
---------------------------------------
A create that times out is not the same event as a create that never left the
process, but both surface as a Python exception. The gateway cannot classify an
outcome it cannot observe, so the transport reports what it actually knows:

``NOT_SENT``
    The request was provably never transmitted. Connection refused, DNS
    failure, and TLS handshake failure all occur before any byte reaches
    WordPress, so a create provably did not happen.

``UNKNOWN``
    The request may have been transmitted. A read timeout, a reset after the
    request was written, or a truncated response all leave the remote outcome
    unproven.

Only the first justifies CONFIRMED_FAILURE. The second must stay
OUTCOME_UNKNOWN, because a timed-out create may have created a post.

Retry posture
-------------
``MAX_RETRIES`` is pinned to 0 and redirects are disabled. urllib3 does not
retry by default, but ``requests.post`` follows redirects, and its
``rebuild_method`` preserves the method and body across 307/308 -- so a
redirect would replay a create and duplicate a remote resource. That is a
retry by another name, so redirects are refused rather than followed.
"""
from __future__ import annotations

import base64
import json as json_module
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol, runtime_checkable

# Pinned, not inherited. An adapter-level or session-level retry default must
# never be able to issue a second create.
MAX_RETRIES = 0

# WordPress core caps per_page at 100 for posts and pages.
MAX_PER_PAGE = 100

# A create must never wait on the network forever. Explicit and finite.
DEFAULT_TIMEOUT_SECONDS = 30.0

_USER_AGENT = "ai-wordpress-factory/1.0 (+publishing-gateway)"


class TransmissionState(str, Enum):
    """What the transport knows about whether the request reached WordPress."""

    NOT_SENT = "NOT_SENT"
    UNKNOWN = "UNKNOWN"


class TransportError(Exception):
    """A transport failure, classified by what is provable and nothing more.

    The message is intentionally generic. An exception string can carry a
    response body, a URL with embedded credentials, or a proxy message, so none
    of that is retained here. The safe code is the only thing that propagates.
    """

    def __init__(self, safe_code: str, state: TransmissionState):
        super().__init__(safe_code)
        self.safe_code = safe_code
        self.state = state


@dataclass(frozen=True, kw_only=True)
class HttpRequest:
    """One outbound request. Never carries credentials in the URL or body."""

    method: str
    url: str
    query: tuple[tuple[str, str], ...] = ()
    json_body: Any = None
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class HttpResponse:
    """A response reduced to exactly what classification is allowed to read.

    ``body_text`` is retained for status/size inspection and for decoding, but
    the gateway never copies it into an outcome, an error code, or a message.
    """

    status_code: int
    body_text: str
    headers: Mapping[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        """Decode the body as JSON, or raise ValueError.

        A truncated or non-JSON body is indistinguishable from a response the
        gateway cannot interpret, so both surface as ValueError and the caller
        maps that to a single conservative outcome.
        """
        return json_module.loads(self.body_text)


@runtime_checkable
class HttpTransport(Protocol):
    """The only way the gateway reaches the network."""

    def send(self, request: HttpRequest, *, timeout: float) -> HttpResponse:
        """Issue exactly one request.

        Implementations must not retry, must not follow redirects, and must
        raise TransportError rather than a library-specific exception so that
        pre-send and post-send uncertainty stay distinguishable.
        """
        ...


def basic_auth_header(username: str, application_password: str) -> str:
    """Build the WordPress application-password Authorization header.

    WordPress application passwords authenticate as HTTP Basic. The secret is
    base64-encoded, not encrypted, so it is only ever placed in a header and
    never in a URL, query string, payload, log line, or exception.
    """
    token = base64.b64encode(f"{username}:{application_password}".encode("utf-8"))
    return "Basic " + token.decode("ascii")


def default_headers(connection_auth_header: str) -> dict[str, str]:
    return {
        "Authorization": connection_auth_header,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": _USER_AGENT,
    }
