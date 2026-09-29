"""Production ``requests`` adapter for the WordPress transport seam.

This is the only module in the gateway that imports ``requests``. It exists to
convert one library's exception taxonomy into the seam's honest vocabulary:
provably-not-sent versus outcome-unknown.

Ordering is load-bearing
------------------------
``requests.exceptions.ConnectTimeout`` subclasses ``ConnectionError`` *and*
``Timeout``, and urllib3's ``NewConnectionError`` subclasses
``ConnectTimeoutError`` which subclasses ``TimeoutError``. The ``except``
clauses below are therefore ordered from most specific to least, and the
urllib3 connect-phase causes are inspected before the generic connection
branch, so a DNS failure is never mistaken for a read timeout.
"""
from __future__ import annotations

import requests
import urllib3.exceptions as urllib3_errors

from publishing.transport import (
    MAX_RETRIES,
    HttpRequest,
    HttpResponse,
    TransportError,
    TransmissionState,
)

# Safe codes only. No library message survives this boundary.
_NOT_SENT_CODE = "CONNECTION_NOT_ESTABLISHED"
_READ_TIMEOUT_CODE = "READ_TIMEOUT"
_CONNECTION_LOST_CODE = "CONNECTION_LOST"


def _urllib3_cause_is_connect_phase(error: BaseException) -> bool:
    """True when the failure provably happened before any request byte was sent.

    urllib3 distinguishes these at the cause level. Connection refused, DNS
    failure, and proxy connect failure all mean the request never reached
    WordPress, which is the only evidence that permits CONFIRMED_FAILURE.

    The cause is inspected in ``args`` as well as in ``__cause__``/``__context__``
    because ``requests.adapters`` raises ``ConnectionError(e, request=request)``
    -- passing the urllib3 error positionally, so it lands in ``args`` and no
    ``from`` clause is set. Reading only ``__cause__`` would silently classify
    every real DNS failure as outcome-unknown.
    """
    candidates: list[BaseException] = [error, error.__cause__, error.__context__]
    candidates.extend(arg for arg in getattr(error, "args", ()) if isinstance(arg, BaseException))
    connect_phase = (urllib3_errors.NewConnectionError,
                     urllib3_errors.NameResolutionError,
                     urllib3_errors.ProxyError,
                     urllib3_errors.ConnectTimeoutError)
    return any(isinstance(candidate, connect_phase) for candidate in candidates)


def _classify(error: BaseException) -> TransportError:
    """Map a requests/urllib3 failure onto the seam's transmission state."""
    # Connect-phase first: requests.ConnectTimeout subclasses ConnectionError,
    # so it must be caught before the generic connection branch below.
    if isinstance(error, requests.exceptions.ConnectTimeout):
        return TransportError(_NOT_SENT_CODE, TransmissionState.NOT_SENT)
    if isinstance(error, requests.exceptions.SSLError):
        # TLS negotiation completes before the request line is written, so
        # nothing was transmitted. A certificate failure cannot have created
        # a post.
        return TransportError(_NOT_SENT_CODE, TransmissionState.NOT_SENT)
    if isinstance(error, requests.exceptions.ReadTimeout):
        # The request was written and WordPress may be processing it. The
        # create may have succeeded with the response lost.
        return TransportError(_READ_TIMEOUT_CODE, TransmissionState.UNKNOWN)
    if isinstance(error, requests.exceptions.ConnectionError):
        if _urllib3_cause_is_connect_phase(error):
            return TransportError(_NOT_SENT_CODE, TransmissionState.NOT_SENT)
        # A reset or broken pipe after transmission: the create may have landed.
        return TransportError(_CONNECTION_LOST_CODE, TransmissionState.UNKNOWN)
    if isinstance(error, requests.exceptions.Timeout):
        # A timeout that is neither connect nor read phase. Unproven.
        return TransportError(_READ_TIMEOUT_CODE, TransmissionState.UNKNOWN)
    if isinstance(error, requests.exceptions.RequestException):
        # Includes ChunkedEncodingError: a truncated response body. WordPress
        # may have created the post and failed while serialising the reply.
        return TransportError(_CONNECTION_LOST_CODE, TransmissionState.UNKNOWN)
    raise error


class RequestsHttpTransport:
    """A real transport that never retries and never follows a redirect."""

    def __init__(self) -> None:
        self._session = requests.Session()
        # Pin retries to zero at the adapter so no session-level default can
        # add a second create.
        adapter = requests.adapters.HTTPAdapter(max_retries=MAX_RETRIES)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    def send(self, request: HttpRequest, *, timeout: float) -> HttpResponse:
        try:
            response = self._session.request(
                request.method,
                request.url,
                params=dict(request.query) or None,
                json=request.json_body,
                headers=dict(request.headers),
                timeout=timeout,
                # A 307/308 redirect replays method and body, which would create
                # a second post. A redirect is refused, not followed.
                allow_redirects=False,
            )
        except requests.exceptions.RequestException as error:
            raise _classify(error) from None
        return HttpResponse(
            status_code=response.status_code,
            body_text=response.text,
            headers={k.lower(): v for k, v in response.headers.items()},
        )

    def close(self) -> None:
        self._session.close()
