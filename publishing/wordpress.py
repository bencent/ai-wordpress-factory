"""WordPress publishing gateway: one PublishCommand in, one PublishOutcome out.

This module is transport only. It imports no repository, no SQLite, and no
persistence module, and it never reads a workspace, a task, a lease, or a
publication row. It has no opinion about publication state: the caller decides
what a ``PublishOutcome`` means, and this gateway only reports what the wire
actually proved.

Two rules shape everything below.

One create, at most
-------------------
``publish()`` issues at most one create request. There is no retry adapter, no
redirect following, and no "try once more" branch. A create whose response is
lost may have created a post, so retrying it would duplicate a remote resource.
Every uncertain path returns OUTCOME_UNKNOWN and leaves the decision to
reconcile, not to this transport.

Certainty is never assumed
--------------------------
CONFIRMED_FAILURE requires positive evidence that the resource was not
created: a definite HTTP rejection, or a connection that provably never
established. Everything that merely *might* have reached WordPress -- a read
timeout, a reset, a 5xx, an unparseable success body -- is OUTCOME_UNKNOWN.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse, urlunparse

from domain.contracts import ContentType
from domain.publication import (
    PublishCommand,
    PublishOutcome,
    PublishOutcomeKind,
    RemoteReference,
    embedded_reconciliation_marker,
)
from publishing.transport import (
    DEFAULT_TIMEOUT_SECONDS,
    HttpRequest,
    HttpResponse,
    HttpTransport,
    TransportError,
    TransmissionState,
    basic_auth_header,
    default_headers,
)

# Core REST collection per content type. There is no fallback: a PAGE is never
# sent to /posts, because that would create the wrong resource type.
_COLLECTIONS: dict[ContentType, str] = {
    ContentType.POST: "posts",
    ContentType.PAGE: "pages",
}

_POSTS_PATH = "wp-json/wp/v2/posts"
_PAGES_PATH = "wp-json/wp/v2/pages"

# A create is a POST. WordPress answers 201 on success; 200 is accepted because
# some proxies rewrite it, but no 2xx without a valid id is ever a success.
_SUCCESS_STATUSES = frozenset({200, 201})

# Statuses that are a definite refusal to create anything. A 4xx other than the
# two below carries a completed server-side decision, so the create provably
# did not happen.
_AUTH_STATUSES = frozenset({401, 403})

# 429 is a definite rejection: the server answered instead of processing. This
# is CONFIRMED_FAILURE and is NOT retried, because a retry would be an
# automatic second create attempt against a server that already refused.
_RATE_LIMIT_STATUS = 429

# 408 is the one 4xx that is NOT definite: the server timed out waiting, and
# the request body may already have been processed.
_UNCERTAIN_CLIENT_STATUS = 408

# Defensive bound. Pagination follows the server's own total, but a server that
# never converges must not spin forever.
_MAX_PAGES = 1000


class GatewayInputRejected(ValueError):
    """A caller/domain contract violation detected before any network use.

    This is not a remote failure and never becomes a PublishOutcome: nothing was
    sent, so there is no remote outcome to classify.
    """


class ReconciliationLookupUnresolved(RuntimeError):
    """The remote state could not be proven, so absence was not proven either.

    Raised instead of returning an empty match list. An empty list is a claim
    that a complete scan found nothing; a failed scan is not that claim, and
    collapsing the two would let a transport blip fabricate a NOT_FOUND.
    """


@dataclass(frozen=True, kw_only=True)
class WordPressConnection:
    """Explicit transport credentials for one WordPress site.

    Passed in by a future credential resolver. The gateway never reads
    ``os.environ``, ``config.json``, or the process-global ``Config``, and never
    looks up a workspace, so a secret cannot arrive from ambient state.

    ``application_password`` is excluded from ``repr`` and from ``str`` so a
    secret cannot reach a log line or a traceback through ordinary printing.
    """

    base_url: str
    username: str
    application_password: str

    def __post_init__(self) -> None:
        for name in ("base_url", "username", "application_password"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
        normalized = self._normalize_base_url(self.base_url)
        object.__setattr__(self, "base_url", normalized)

    @staticmethod
    def _normalize_base_url(base_url: str) -> str:
        """Normalize to a scheme+host+optional-path root with no trailing slash.

        Credentials embedded in a URL are rejected outright: they would land in
        request URLs, where they can be logged or cached by intermediaries.
        """
        candidate = base_url.strip()
        parsed = urlparse(candidate)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("base_url must be an http or https URL")
        if not parsed.netloc:
            raise ValueError("base_url must include a host")
        if parsed.username or parsed.password:
            raise ValueError("base_url must not embed credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("base_url must not carry a query string or fragment")
        path = parsed.path.rstrip("/")
        return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))

    def __repr__(self) -> str:
        # Never render the secret, not even truncated.
        return (f"WordPressConnection(base_url={self.base_url!r}, "
                f"username={self.username!r}, application_password='***')")


class WordPressGateway:
    """Converts one PublishCommand into one classified PublishOutcome."""

    def __init__(self, connection: WordPressConnection, transport: HttpTransport, *,
                 timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        if not isinstance(connection, WordPressConnection):
            raise GatewayInputRejected("connection must be a WordPressConnection")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
            raise GatewayInputRejected("timeout must be a number")
        if timeout <= 0:
            raise GatewayInputRejected("timeout must be a positive finite number")
        self._connection = connection
        self._transport = transport
        self._timeout = float(timeout)

    # -- internals ---------------------------------------------------------

    @property
    def _auth_header(self) -> str:
        return basic_auth_header(self._connection.username,
                                 self._connection.application_password)

    def _url(self, path: str) -> str:
        return f"{self._connection.base_url}/{path}"

    def _headers(self) -> dict[str, str]:
        return default_headers(self._auth_header)

    @staticmethod
    def _build_payload(command: PublishCommand) -> dict[str, object]:
        """Project the command onto the minimal WordPress create payload.

        ``content`` is copied byte-for-byte. The reconciliation marker is
        already embedded by 3C2: it is never regenerated, appended, removed, or
        rewritten here, and the command is not mutated.
        """
        if not isinstance(command, PublishCommand):
            raise GatewayInputRejected("publish requires a PublishCommand")
        payload: dict[str, object] = {
            "title": command.title,
            "content": command.content,
            "status": "publish",
        }
        if command.slug is not None:
            payload["slug"] = command.slug
        if command.excerpt is not None:
            payload["excerpt"] = command.excerpt
        if command.featured_media_id is not None:
            payload["featured_media"] = command.featured_media_id
        if command.content_type is ContentType.POST:
            # Taxonomy is POST-only. A PAGE never receives these keys, not even
            # empty ones.
            payload["categories"] = list(command.category_ids)
            payload["tags"] = list(command.tag_ids)
        return payload

    def _decode_create_reference(self, response: HttpResponse,
                                 content_type: ContentType) -> RemoteReference | None:
        """Extract a positive remote id from a create response, else None.

        The id is the only evidence of creation. A slug, title, or permalink is
        never used to infer one, and a response whose id is missing or invalid
        returns None so the caller can treat it as unproven.

        The command's own content type is carried into the reference so that a
        post and a page sharing an integer stay distinguishable resources.
        """
        try:
            body = response.json()
        except ValueError:
            return None
        if not isinstance(body, dict):
            return None
        raw_id = body.get("id")
        # bool is an int subclass; a boolean id is malformed, not an id.
        if type(raw_id) is not int or raw_id <= 0:
            return None
        raw_link = body.get("link")
        link = raw_link if isinstance(raw_link, str) and raw_link.strip() else None
        return RemoteReference(content_type=content_type, remote_resource_id=raw_id,
                               remote_url=link)

    def _classify_status(self, status_code: int) -> PublishOutcome:
        """Map a non-success HTTP status onto the most conservative outcome.

        A definite refusal is CONFIRMED_FAILURE. Anything that may have been
        processed, including every 5xx, is OUTCOME_UNKNOWN.
        """
        if status_code in _AUTH_STATUSES:
            code = "AUTHENTICATION" if status_code == 401 else "PERMISSION"
            return PublishOutcome(kind=PublishOutcomeKind.CONFIRMED_FAILURE, error_code=code)
        if status_code == _RATE_LIMIT_STATUS:
            return PublishOutcome(kind=PublishOutcomeKind.CONFIRMED_FAILURE,
                                  error_code="RATE_LIMIT")
        if status_code == _UNCERTAIN_CLIENT_STATUS:
            return PublishOutcome(kind=PublishOutcomeKind.OUTCOME_UNKNOWN,
                                  error_code="TIMEOUT")
        if 500 <= status_code <= 599:
            # A 5xx can follow a successful insert -- a proxy error after the
            # backend created the post, or a failure while rendering the reply.
            return PublishOutcome(kind=PublishOutcomeKind.OUTCOME_UNKNOWN,
                                  error_code="UNAVAILABLE")
        if 400 <= status_code <= 499:
            # A completed 4xx decision: the request was refused, so nothing was
            # created.
            return PublishOutcome(kind=PublishOutcomeKind.CONFIRMED_FAILURE,
                                  error_code="INVALID_REQUEST")
        return PublishOutcome(kind=PublishOutcomeKind.OUTCOME_UNKNOWN, error_code="UNKNOWN")

    # -- public surface ----------------------------------------------------

    def publish(self, command: PublishCommand) -> PublishOutcome:
        """Create one remote post or page and classify the result.

        Issues at most one request. Never raises for a remote outcome: every
        wire result is expressed as a PublishOutcome. GatewayInputRejected is
        raised only for a caller contract violation, before any request.
        """
        if not isinstance(command, PublishCommand):
            raise GatewayInputRejected("publish requires a PublishCommand")
        if command.content_type not in _COLLECTIONS:
            raise GatewayInputRejected("content_type must be POST or PAGE")
        path = _POSTS_PATH if command.content_type is ContentType.POST else _PAGES_PATH
        request = HttpRequest(
            method="POST",
            url=self._url(path),
            json_body=self._build_payload(command),
            headers=self._headers(),
        )
        try:
            response = self._transport.send(request, timeout=self._timeout)
        except TransportError as error:
            return self._classify_transport_error(error)
        if response.status_code not in _SUCCESS_STATUSES:
            return self._classify_status(response.status_code)
        reference = self._decode_create_reference(response, command.content_type)
        if reference is None:
            # Success status with no usable id. The post may well exist; the
            # gateway simply cannot prove it, so it does not claim it.
            return PublishOutcome(kind=PublishOutcomeKind.OUTCOME_UNKNOWN,
                                  error_code="INVALID_RESPONSE")
        return PublishOutcome(kind=PublishOutcomeKind.CONFIRMED_SUCCESS, remote=reference)

    @staticmethod
    def _classify_transport_error(error: TransportError) -> PublishOutcome:
        if error.state is TransmissionState.NOT_SENT:
            # Provable: the request never reached WordPress, so nothing exists.
            return PublishOutcome(kind=PublishOutcomeKind.CONFIRMED_FAILURE,
                                  error_code="UNAVAILABLE")
        # Everything else may have been transmitted. TIMEOUT and UNKNOWN both
        # mean "cannot prove"; the distinction is diagnostic, not decisional.
        code = "TIMEOUT" if error.safe_code.endswith("TIMEOUT") else "UNAVAILABLE"
        return PublishOutcome(kind=PublishOutcomeKind.OUTCOME_UNKNOWN, error_code=code)

    def find_by_marker(self, marker: str) -> tuple[RemoteReference, ...]:
        """Return every remote post and page whose raw content holds ``marker``.

        Strategy, and why
        -----------------
        WordPress core ``search=`` is deliberately not used. Its matching is
        tokenized and case-insensitive across rendered content, so it can both
        miss a marker and match unrelated text; it cannot support exact
        matching. Slug and title are not identity either -- 3C2 established that
        the marker is the reconciliation identity, and two publications may
        share a slug.

        Instead both collections are listed read-only and each item's
        ``content.raw`` is compared against the exact marker comment. ``raw`` is
        the stored ``post_content`` value, which is what the create wrote, so an
        exact substring test there is a real identity test.

        Credential precondition
        -----------------------
        This requires ``context=edit`` so ``content.raw`` is returned. If the
        site strips HTML comments on save (WordPress applies kses filtering for
        users without ``unfiltered_html``), the marker is absent from storage and
        this lookup cannot find it. The gateway cannot detect that from a read,
        so it is reported as a credential precondition rather than papered over.

        Failure is never absence
        ------------------------
        Any failed page, a missing ``content.raw``, or exhausted pagination
        raises ReconciliationLookupUnresolved. Returning an empty tuple would
        assert a complete scan found nothing, and a caller would reasonably
        treat that as NOT_FOUND. Deciding FOUND/NOT_FOUND/AMBIGUOUS remains the
        domain's job via ``classify_reconciliation``.
        """
        if not isinstance(marker, str) or not marker.strip():
            raise GatewayInputRejected("marker is required")
        expected = _expected_marker_text(marker)
        matches: list[RemoteReference] = []
        for content_type, path in ((ContentType.POST, _POSTS_PATH), (ContentType.PAGE, _PAGES_PATH)):
            matches.extend(self._scan_collection(path, content_type, expected))
        return tuple(matches)

    def _scan_collection(self, path: str, content_type: ContentType,
                         expected: str) -> list[RemoteReference]:
        """Walk one collection page by page, matching the exact marker."""
        found: list[RemoteReference] = []
        page = 1
        while page <= _MAX_PAGES:
            query = (("context", "edit"), ("per_page", "100"), ("page", str(page)),
                     ("status", "publish"))
            try:
                response = self._transport.send(
                    HttpRequest(method="GET", url=self._url(path), query=query,
                                headers=self._headers()),
                    timeout=self._timeout)
            except TransportError as error:
                # A page that failed means the scan is incomplete. An incomplete
                # scan is not an absence proof.
                raise ReconciliationLookupUnresolved(
                    "reconciliation scan did not complete") from None
            if response.status_code != 200:
                raise ReconciliationLookupUnresolved(
                    "reconciliation scan did not complete")
            try:
                body = response.json()
            except ValueError:
                raise ReconciliationLookupUnresolved(
                    "reconciliation scan did not complete") from None
            if not isinstance(body, list):
                raise ReconciliationLookupUnresolved(
                    "reconciliation scan did not complete")
            for item in body:
                if not isinstance(item, dict):
                    raise ReconciliationLookupUnresolved(
                        "reconciliation scan did not complete")
                content = item.get("content")
                if not isinstance(content, dict) or "raw" not in content:
                    # Without raw content this item cannot be proven either
                    # way, so the whole scan is unresolved.
                    raise ReconciliationLookupUnresolved(
                        "reconciliation scan did not complete")
                raw = content.get("raw")
                if not isinstance(raw, str):
                    raise ReconciliationLookupUnresolved(
                        "reconciliation scan did not complete")
                if expected not in raw:
                    continue
                item_id = item.get("id")
                if type(item_id) is not int or item_id <= 0:
                    raise ReconciliationLookupUnresolved(
                        "reconciliation scan did not complete")
                link = item.get("link")
                # The collection being scanned is this item's real content type,
                # so a page is never reported as a post even when the two share
                # an integer id.
                found.append(RemoteReference(
                    content_type=content_type,
                    remote_resource_id=item_id,
                    remote_url=link if isinstance(link, str) and link.strip() else None))
            total_pages = _total_pages(response)
            if total_pages is not None:
                if page >= total_pages:
                    break
            elif len(body) < _PAGE_SIZE:
                break
            page += 1
        else:
            # Pagination never converged within the hard bound.
            raise ReconciliationLookupUnresolved("reconciliation scan did not complete")
        return found


_PAGE_SIZE = 100


def _expected_marker_text(marker: str) -> str:
    """Normalize a marker into the exact text that appears in stored content.

    Accepts either the bare marker or the already-embedded comment so a caller
    cannot accidentally search for a string that was never written. Comparison
    stays an exact substring test: no case folding, no tokenizing, no slug.
    """
    if not isinstance(marker, str) or not marker.strip():
        raise GatewayInputRejected("marker is required")
    text = marker.strip()
    if text.startswith("<!--") and text.endswith("-->"):
        return text
    _, _, publication_id = text.rpartition(":")
    if not publication_id:
        raise GatewayInputRejected("marker must end with a publication id")
    return embedded_reconciliation_marker(publication_id)


def _total_pages(response: HttpResponse) -> int | None:
    """Read X-WP-TotalPages, or None when the header is absent or unusable."""
    raw = response.headers.get("x-wp-totalpages")
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


__all__ = [
    "GatewayInputRejected",
    "ReconciliationLookupUnresolved",
    "WordPressConnection",
    "WordPressGateway",
]
