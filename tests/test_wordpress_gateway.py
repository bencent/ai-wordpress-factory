"""Tests for the WordPress publishing gateway.

Every test runs against a deterministic fake transport. No live WordPress, no
network, and no monkeypatching of requests internals: the gateway talks to an
injected ``HttpTransport``, so a simulated timeout is a real code path through
real classification logic rather than a patched library.

The fake records every request it is handed, which is what makes the
no-automatic-retry and no-credential-leak assertions evidence rather than
assertions about intent.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from domain.contracts import ContentType
from domain.publication import (
    SAFE_PUBLICATION_ERROR_CODES,
    PublishCommand,
    PublishOutcome,
    PublishOutcomeKind,
    ReconciliationMatch,
    RemoteReference,
    build_publish_command,
    embedded_reconciliation_marker,
    reconciliation_marker,
)
from publishing.transport import (
    DEFAULT_TIMEOUT_SECONDS,
    HttpRequest,
    HttpResponse,
    TransportError,
    TransmissionState,
    basic_auth_header,
)
from publishing.wordpress import (
    GatewayInputRejected,
    ReconciliationLookupUnresolved,
    WordPressConnection,
    WordPressGateway,
)

ROOT = Path(__file__).resolve().parents[1]
SECRET = "abcd efgh ijkl mnop qrst uvwx"
BASE_URL = "https://wp.example.test"


# -- fakes ------------------------------------------------------------------


class FakeTransport:
    """Records every request and replays a scripted result.

    ``results`` may hold HttpResponse values or TransportError instances; the
    last entry repeats once exhausted, so a test can script an exact sequence
    and then assert on how many requests were actually made.
    """

    def __init__(self, results: list[Any]):
        self._results = list(results)
        self.requests: list[HttpRequest] = []
        self.timeouts: list[float] = []

    def send(self, request: HttpRequest, *, timeout: float) -> HttpResponse:
        self.requests.append(request)
        self.timeouts.append(timeout)
        index = min(len(self.requests) - 1, len(self._results) - 1)
        result = self._results[index]
        if isinstance(result, TransportError):
            raise result
        if isinstance(result, Exception):
            raise result
        return result

    @property
    def call_count(self) -> int:
        return len(self.requests)

    @property
    def methods(self) -> list[str]:
        return [r.method for r in self.requests]

    @property
    def urls(self) -> list[str]:
        return [r.url for r in self.requests]


def ok_response(payload: dict, *, status: int = 201,
                headers: dict | None = None) -> HttpResponse:
    return HttpResponse(status_code=status, body_text=json.dumps(payload),
                        headers=headers or {})


def created(remote_id: int = 42, link: str | None = "https://wp.example.test/?p=42"
            ) -> HttpResponse:
    return ok_response({"id": remote_id, "link": link, "status": "publish"})


# -- fixtures ---------------------------------------------------------------


def make_connection() -> WordPressConnection:
    return WordPressConnection(base_url=BASE_URL, username="alice",
                               application_password=SECRET)


def make_gateway(results: list[Any], *, connection: WordPressConnection | None = None,
                 timeout: float | None = None) -> tuple[WordPressGateway, FakeTransport]:
    transport = FakeTransport(results)
    kwargs = {} if timeout is None else {"timeout": timeout}
    gateway = WordPressGateway(connection or make_connection(), transport, **kwargs)
    return gateway, transport


def make_command(content_type: ContentType = ContentType.POST, **overrides) -> PublishCommand:
    """Build a real PublishCommand through the real 3C2 builder.

    The gateway is transport-only, so no store, task, or lease is involved: the
    3C2 command builder is the only thing that has to be real here, so that the
    marker and PAGE-taxonomy rules under test are the production ones.
    """
    from datetime import datetime, timezone
    from domain.contracts import ContentVersion, Status
    from domain.publication import PublicationRequest, PublicationState

    now = datetime.now(timezone.utc).isoformat()
    publication_id = overrides.pop("publication_id", str(uuid4()))
    content = overrides.pop("content", "<p>Body</p>")
    taxonomy = overrides.pop("taxonomy", None)
    if content_type is ContentType.POST and taxonomy is None:
        taxonomy = {"category_ids": [3, 5], "tag_ids": [9]}
    version = ContentVersion(
        content_version_id=overrides.pop("content_version_id", str(uuid4())),
        task_id=overrides.pop("task_id", str(uuid4())),
        run_id=str(uuid4()),
        version_number=1,
        content_type=content_type,
        title=overrides.pop("title", "Title"),
        content=content,
        validation_result={"quality": {"passed": True}},
        created_at=now,
        updated_at=now,
        status=Status.AWAITING_APPROVAL,
        excerpt=overrides.pop("excerpt", None),
        taxonomy=taxonomy,
        suggested_slug=overrides.pop("slug", None),
    )
    request = PublicationRequest(
        publication_id=publication_id,
        workspace_id=str(uuid4()),
        task_id=version.task_id,
        content_version_id=version.content_version_id,
        approved_run_id=version.run_id,
        content_type=content_type,
        idempotency_key=str(uuid4()),
        # PENDING on purpose: the gateway is handed a finished command and never
        # inspects publication state, so this fixture asserts no lease exists.
        # Claiming and lease handling belong to the future executor slice.
        state=PublicationState.PENDING,
        created_at=now,
        updated_at=now,
    )
    return build_publish_command(request, version)


def strip_comments_and_docstrings(source: str) -> str:
    """Return executable code only, so prose cannot satisfy a code assertion.

    Source scanning is only meaningful once comments and docstrings are gone:
    otherwise a docstring that *explains* the absence of a retry would itself
    contain the word "retry" and make the test pass for the wrong reason.
    """
    import ast
    import io
    import tokenize

    out: list[str] = []
    previous_type = tokenize.INDENT
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            continue
        if (token.type == tokenize.STRING and previous_type in
                (tokenize.INDENT, tokenize.NEWLINE, tokenize.NL, tokenize.DEDENT,
                 tokenize.ENCODING)):
            continue
        if token.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT,
                              tokenize.DEDENT):
            out.append(token.string)
        previous_type = token.type
    del ast
    return " ".join(out)


GATEWAY_MODULES = ("publishing/wordpress.py", "publishing/transport.py",
                   "publishing/requests_transport.py")


def gateway_code(module: str) -> str:
    return strip_comments_and_docstrings((ROOT / module).read_text(encoding="utf-8"))


# -- 1-2: endpoint mapping --------------------------------------------------


class TestEndpointMapping:
    def test_post_uses_posts_endpoint(self):
        gateway, transport = make_gateway([created()])
        gateway.publish(make_command(ContentType.POST))
        assert transport.urls == [f"{BASE_URL}/wp-json/wp/v2/posts"]

    def test_page_uses_pages_endpoint(self):
        gateway, transport = make_gateway([created()])
        gateway.publish(make_command(ContentType.PAGE))
        assert transport.urls == [f"{BASE_URL}/wp-json/wp/v2/pages"]

    def test_page_is_never_sent_to_posts_endpoint(self):
        gateway, transport = make_gateway([created()])
        gateway.publish(make_command(ContentType.PAGE))
        assert not any(url.endswith("/posts") for url in transport.urls)

    def test_no_endpoint_fallback_on_failure(self):
        """A 404 on /pages must not retry the create against /posts."""
        gateway, transport = make_gateway([HttpResponse(status_code=404, body_text="{}")])
        outcome = gateway.publish(make_command(ContentType.PAGE))
        assert outcome.kind is PublishOutcomeKind.CONFIRMED_FAILURE
        assert transport.call_count == 1
        assert transport.urls == [f"{BASE_URL}/wp-json/wp/v2/pages"]


# -- 3-7: payload mapping ---------------------------------------------------


class TestPayloadMapping:
    def test_post_payload_includes_categories_and_tags(self):
        gateway, transport = make_gateway([created()])
        gateway.publish(make_command(ContentType.POST))
        body = transport.requests[0].json_body
        assert body["categories"] == [3, 5]
        assert body["tags"] == [9]

    def test_page_payload_excludes_categories_and_tags(self):
        gateway, transport = make_gateway([created()])
        gateway.publish(make_command(ContentType.PAGE))
        body = transport.requests[0].json_body
        assert "categories" not in body
        assert "tags" not in body

    def test_post_sends_empty_taxonomy_lists_rather_than_omitting(self):
        command = make_command(ContentType.POST, taxonomy={"category_ids": [], "tag_ids": []})
        gateway, transport = make_gateway([created()])
        gateway.publish(command)
        body = transport.requests[0].json_body
        assert body["categories"] == []
        assert body["tags"] == []

    def test_slug_included_only_when_present(self):
        gateway, transport = make_gateway([created()])
        gateway.publish(make_command(ContentType.POST, slug="my-slug"))
        assert transport.requests[0].json_body["slug"] == "my-slug"

        gateway, transport = make_gateway([created()])
        gateway.publish(make_command(ContentType.POST))
        assert "slug" not in transport.requests[0].json_body

    def test_excerpt_included_only_when_present(self):
        gateway, transport = make_gateway([created()])
        gateway.publish(make_command(ContentType.POST, excerpt="Summary"))
        assert transport.requests[0].json_body["excerpt"] == "Summary"

        gateway, transport = make_gateway([created()])
        gateway.publish(make_command(ContentType.POST))
        assert "excerpt" not in transport.requests[0].json_body

    def test_featured_media_included_only_when_present(self):
        command = make_command(ContentType.POST)
        command = PublishCommand(**{**command.__dict__, "featured_media_id": 77})
        gateway, transport = make_gateway([created()])
        gateway.publish(command)
        assert transport.requests[0].json_body["featured_media"] == 77

        gateway, transport = make_gateway([created()])
        gateway.publish(make_command(ContentType.POST))
        assert "featured_media" not in transport.requests[0].json_body

    def test_status_is_always_publish(self):
        gateway, transport = make_gateway([created()])
        gateway.publish(make_command(ContentType.POST))
        assert transport.requests[0].json_body["status"] == "publish"

    def test_payload_has_no_legacy_fields(self):
        gateway, transport = make_gateway([created()])
        gateway.publish(make_command(ContentType.POST))
        body = transport.requests[0].json_body
        for legacy in ("comment_status", "ping_status", "yoast_meta", "meta"):
            assert legacy not in body

    def test_payload_contains_only_the_agreed_keys(self):
        gateway, transport = make_gateway([created()])
        gateway.publish(make_command(ContentType.POST, slug="s", excerpt="e"))
        assert set(transport.requests[0].json_body) == {
            "title", "content", "status", "slug", "excerpt", "categories", "tags"}


# -- 8-10: content fidelity and immutability --------------------------------


class TestContentFidelity:
    def test_content_is_command_content_byte_for_byte(self):
        content = "<p>Exact</p>\n\n<!-- author note -->\n\n<p>More</p>"
        command = make_command(ContentType.POST, content=content)
        gateway, transport = make_gateway([created()])
        gateway.publish(command)
        assert transport.requests[0].json_body["content"] == command.content

    def test_gateway_does_not_append_another_marker(self):
        command = make_command(ContentType.POST)
        gateway, transport = make_gateway([created()])
        gateway.publish(command)
        sent = transport.requests[0].json_body["content"]
        assert sent.count(command.reconciliation_marker) == 1
        assert sent == command.content

    def test_gateway_does_not_remove_or_rewrite_the_marker(self):
        command = make_command(ContentType.POST)
        gateway, transport = make_gateway([created()])
        gateway.publish(command)
        sent = transport.requests[0].json_body["content"]
        assert embedded_reconciliation_marker(command.publication_id) in sent

    def test_command_is_unchanged_after_publish(self):
        command = make_command(ContentType.POST, slug="s", excerpt="e")
        before = repr(command)
        fields_before = {f: getattr(command, f) for f in command.__dataclass_fields__}
        gateway, _ = make_gateway([created()])
        gateway.publish(command)
        assert repr(command) == before
        assert {f: getattr(command, f) for f in command.__dataclass_fields__} == fields_before
        with pytest.raises(Exception):
            command.title = "mutated"  # frozen

    def test_command_is_unchanged_after_failed_publish(self):
        command = make_command(ContentType.POST)
        before = repr(command)
        gateway, _ = make_gateway([HttpResponse(status_code=500, body_text="{}")])
        gateway.publish(command)
        assert repr(command) == before

    def test_publish_rejects_non_command_before_any_request(self):
        gateway, transport = make_gateway([created()])
        with pytest.raises(GatewayInputRejected):
            gateway.publish({"title": "not a command"})
        assert transport.call_count == 0


# -- 11-15: success classification -----------------------------------------


class TestSuccessClassification:
    def test_valid_create_response_is_confirmed_success(self):
        gateway, _ = make_gateway([created(42, "https://wp.example.test/?p=42")])
        outcome = gateway.publish(make_command(ContentType.POST))
        assert outcome.kind is PublishOutcomeKind.CONFIRMED_SUCCESS
        assert outcome.remote is not None
        assert outcome.remote.remote_resource_id == 42
        assert outcome.error_code is None

    def test_remote_link_is_preserved_when_valid(self):
        gateway, _ = make_gateway([created(7, "https://wp.example.test/hello/")])
        outcome = gateway.publish(make_command(ContentType.POST))
        assert outcome.remote.remote_url == "https://wp.example.test/hello/"

    def test_missing_link_yields_null_url_but_still_succeeds(self):
        gateway, _ = make_gateway([ok_response({"id": 5})])
        outcome = gateway.publish(make_command(ContentType.POST))
        assert outcome.kind is PublishOutcomeKind.CONFIRMED_SUCCESS
        assert outcome.remote.remote_url is None

    def test_zero_id_is_not_success(self):
        gateway, _ = make_gateway([ok_response({"id": 0, "link": "x"})])
        outcome = gateway.publish(make_command(ContentType.POST))
        assert outcome.kind is PublishOutcomeKind.OUTCOME_UNKNOWN
        assert outcome.error_code == "INVALID_RESPONSE"

    def test_negative_id_is_not_success(self):
        gateway, _ = make_gateway([ok_response({"id": -3})])
        outcome = gateway.publish(make_command(ContentType.POST))
        assert outcome.kind is PublishOutcomeKind.OUTCOME_UNKNOWN

    def test_boolean_id_is_not_success(self):
        gateway, _ = make_gateway([ok_response({"id": True})])
        outcome = gateway.publish(make_command(ContentType.POST))
        assert outcome.kind is PublishOutcomeKind.OUTCOME_UNKNOWN

    def test_string_id_is_not_success(self):
        gateway, _ = make_gateway([ok_response({"id": "42"})])
        outcome = gateway.publish(make_command(ContentType.POST))
        assert outcome.kind is PublishOutcomeKind.OUTCOME_UNKNOWN

    def test_id_is_never_inferred_from_slug_or_link(self):
        """A success body with no id is unproven even with a permalink."""
        gateway, _ = make_gateway(
            [ok_response({"slug": "hello", "link": "https://wp.example.test/hello/"})])
        outcome = gateway.publish(make_command(ContentType.POST, slug="hello"))
        assert outcome.kind is PublishOutcomeKind.OUTCOME_UNKNOWN
        assert outcome.remote is None

    def test_id_is_never_inferred_from_title(self):
        gateway, _ = make_gateway([ok_response({"title": {"rendered": "Title"}})])
        outcome = gateway.publish(make_command(ContentType.POST))
        assert outcome.kind is PublishOutcomeKind.OUTCOME_UNKNOWN

    def test_json_array_body_is_not_success(self):
        gateway, _ = make_gateway([HttpResponse(status_code=201, body_text="[1,2,3]")])
        outcome = gateway.publish(make_command(ContentType.POST))
        assert outcome.kind is PublishOutcomeKind.OUTCOME_UNKNOWN


# -- 15: malformed success --------------------------------------------------


class TestMalformedSuccess:
    def test_truncated_json_is_outcome_unknown(self):
        gateway, _ = make_gateway([HttpResponse(status_code=201, body_text='{"id": 42')])
        outcome = gateway.publish(make_command(ContentType.POST))
        assert outcome.kind is PublishOutcomeKind.OUTCOME_UNKNOWN
        assert outcome.error_code == "INVALID_RESPONSE"

    def test_html_error_page_with_success_status_is_outcome_unknown(self):
        gateway, _ = make_gateway([HttpResponse(status_code=201, body_text="<html>fatal error</html>")])
        outcome = gateway.publish(make_command(ContentType.POST))
        assert outcome.kind is PublishOutcomeKind.OUTCOME_UNKNOWN
        assert outcome.error_code == "INVALID_RESPONSE"

    def test_empty_body_with_success_status_is_outcome_unknown(self):
        gateway, _ = make_gateway([HttpResponse(status_code=201, body_text="")])
        outcome = gateway.publish(make_command(ContentType.POST))
        assert outcome.kind is PublishOutcomeKind.OUTCOME_UNKNOWN


# -- 16-19: failure taxonomy ------------------------------------------------


class TestFailureTaxonomy:
    def test_validation_4xx_is_confirmed_failure(self):
        gateway, _ = make_gateway([HttpResponse(status_code=400, body_text='{"code":"rest_invalid_param"}')])
        outcome = gateway.publish(make_command(ContentType.POST))
        assert outcome.kind is PublishOutcomeKind.CONFIRMED_FAILURE
        assert outcome.error_code == "INVALID_REQUEST"

    def test_422_is_confirmed_failure(self):
        gateway, _ = make_gateway([HttpResponse(status_code=422, body_text="{}")])
        outcome = gateway.publish(make_command(ContentType.POST))
        assert outcome.kind is PublishOutcomeKind.CONFIRMED_FAILURE

    def test_401_is_confirmed_failure_with_auth_code(self):
        gateway, _ = make_gateway([HttpResponse(status_code=401, body_text="{}")])
        outcome = gateway.publish(make_command(ContentType.POST))
        assert outcome.kind is PublishOutcomeKind.CONFIRMED_FAILURE
        assert outcome.error_code == "AUTHENTICATION"

    def test_403_is_confirmed_failure_with_permission_code(self):
        gateway, _ = make_gateway([HttpResponse(status_code=403, body_text="{}")])
        outcome = gateway.publish(make_command(ContentType.POST))
        assert outcome.kind is PublishOutcomeKind.CONFIRMED_FAILURE
        assert outcome.error_code == "PERMISSION"

    def test_429_is_confirmed_failure_and_not_retried(self):
        """A 429 is a completed refusal, so nothing was created."""
        gateway, transport = make_gateway([HttpResponse(status_code=429, body_text="{}")])
        outcome = gateway.publish(make_command(ContentType.POST))
        assert outcome.kind is PublishOutcomeKind.CONFIRMED_FAILURE
        assert outcome.error_code == "RATE_LIMIT"
        assert transport.call_count == 1

    def test_408_is_outcome_unknown_because_body_may_be_processed(self):
        gateway, _ = make_gateway([HttpResponse(status_code=408, body_text="{}")])
        outcome = gateway.publish(make_command(ContentType.POST))
        assert outcome.kind is PublishOutcomeKind.OUTCOME_UNKNOWN

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_5xx_is_outcome_unknown(self, status):
        gateway, _ = make_gateway([HttpResponse(status_code=status, body_text="{}")])
        outcome = gateway.publish(make_command(ContentType.POST))
        assert outcome.kind is PublishOutcomeKind.OUTCOME_UNKNOWN
        assert outcome.error_code == "UNAVAILABLE"

    def test_unexpected_status_is_outcome_unknown(self):
        gateway, _ = make_gateway([HttpResponse(status_code=302, body_text="{}")])
        outcome = gateway.publish(make_command(ContentType.POST))
        assert outcome.kind is PublishOutcomeKind.OUTCOME_UNKNOWN


# -- 20-22: transport failures ----------------------------------------------


class TestTransportFailure:
    def test_read_timeout_is_outcome_unknown(self):
        gateway, _ = make_gateway(
            [TransportError("READ_TIMEOUT", TransmissionState.UNKNOWN)])
        outcome = gateway.publish(make_command(ContentType.POST))
        assert outcome.kind is PublishOutcomeKind.OUTCOME_UNKNOWN
        assert outcome.error_code == "TIMEOUT"

    def test_connection_reset_is_outcome_unknown(self):
        gateway, _ = make_gateway(
            [TransportError("CONNECTION_LOST", TransmissionState.UNKNOWN)])
        outcome = gateway.publish(make_command(ContentType.POST))
        assert outcome.kind is PublishOutcomeKind.OUTCOME_UNKNOWN

    def test_proven_pre_send_failure_is_confirmed_failure(self):
        """A connection that never established proves nothing was created."""
        gateway, _ = make_gateway(
            [TransportError("CONNECTION_NOT_ESTABLISHED", TransmissionState.NOT_SENT)])
        outcome = gateway.publish(make_command(ContentType.POST))
        assert outcome.kind is PublishOutcomeKind.CONFIRMED_FAILURE
        assert outcome.error_code == "UNAVAILABLE"

    def test_ambiguous_connection_failure_is_never_confirmed_failure(self):
        gateway, _ = make_gateway(
            [TransportError("CONNECTION_LOST", TransmissionState.UNKNOWN)])
        outcome = gateway.publish(make_command(ContentType.POST))
        assert outcome.kind is not PublishOutcomeKind.CONFIRMED_FAILURE


class TestRequestsAdapterClassification:
    """The real adapter must map library exceptions onto honest states."""

    @staticmethod
    def _classify(exc):
        import requests
        import urllib3.exceptions as ue
        from publishing.requests_transport import _classify as classify
        return classify(exc)

    def test_connect_timeout_is_proven_not_sent(self):
        import requests
        outcome = self._classify(requests.exceptions.ConnectTimeout("t"))
        assert outcome.state is TransmissionState.NOT_SENT

    def test_dns_failure_is_proven_not_sent(self):
        import requests
        import urllib3.exceptions as ue
        cause = ue.NameResolutionError("wp.example.test", None, OSError("getaddrinfo failed"))
        # requests raises ConnectionError(e, request=request): the urllib3 error
        # is positional, so the classifier must read args, not __cause__.
        exc = requests.exceptions.ConnectionError(cause, request=None)
        assert exc.__cause__ is None
        assert self._classify(exc).state is TransmissionState.NOT_SENT

    def test_connection_refused_is_proven_not_sent(self):
        import requests
        import urllib3.exceptions as ue
        exc = requests.exceptions.ConnectionError(
            ue.NewConnectionError("wp.example.test", "connection refused"))
        assert self._classify(exc).state is TransmissionState.NOT_SENT

    def test_tls_failure_is_proven_not_sent(self):
        import requests
        assert self._classify(
            requests.exceptions.SSLError("bad cert")).state is TransmissionState.NOT_SENT

    def test_read_timeout_is_unknown(self):
        import requests
        assert self._classify(
            requests.exceptions.ReadTimeout("t")).state is TransmissionState.UNKNOWN

    def test_reset_after_send_is_unknown(self):
        import requests
        import urllib3.exceptions as ue
        exc = requests.exceptions.ConnectionError(ue.ProtocolError("reset by peer"))
        assert self._classify(exc).state is TransmissionState.UNKNOWN

    def test_truncated_body_is_unknown(self):
        import requests
        assert self._classify(
            requests.exceptions.ChunkedEncodingError("truncated")).state is TransmissionState.UNKNOWN

    def test_connect_timeout_wins_over_generic_connection_error(self):
        """requests.ConnectTimeout subclasses ConnectionError; order matters."""
        import requests
        exc = requests.exceptions.ConnectTimeout("t")
        assert isinstance(exc, requests.exceptions.ConnectionError)
        assert self._classify(exc).state is TransmissionState.NOT_SENT


# -- 23-26: no automatic retry ---------------------------------------------


class TestNoAutomaticRetry:
    def test_one_publish_makes_exactly_one_request(self):
        gateway, transport = make_gateway([created()])
        gateway.publish(make_command(ContentType.POST))
        assert transport.call_count == 1

    def test_timeout_triggers_no_retry(self):
        gateway, transport = make_gateway(
            [TransportError("READ_TIMEOUT", TransmissionState.UNKNOWN)])
        gateway.publish(make_command(ContentType.POST))
        assert transport.call_count == 1

    def test_429_triggers_no_retry(self):
        gateway, transport = make_gateway([HttpResponse(status_code=429, body_text="{}")])
        gateway.publish(make_command(ContentType.POST))
        assert transport.call_count == 1

    def test_5xx_triggers_no_retry(self):
        gateway, transport = make_gateway([HttpResponse(status_code=503, body_text="{}")])
        gateway.publish(make_command(ContentType.POST))
        assert transport.call_count == 1

    def test_connection_reset_triggers_no_retry(self):
        gateway, transport = make_gateway(
            [TransportError("CONNECTION_LOST", TransmissionState.UNKNOWN)])
        gateway.publish(make_command(ContentType.POST))
        assert transport.call_count == 1

    def test_repeated_calls_still_issue_one_request_each(self):
        gateway, transport = make_gateway([created()])
        command = make_command(ContentType.POST)
        for _ in range(3):
            gateway.publish(command)
        assert transport.call_count == 3  # one per explicit call, never more

    def test_transport_adapter_pins_zero_retries(self):
        import requests
        from publishing.requests_transport import MAX_RETRIES, RequestsHttpTransport
        assert MAX_RETRIES == 0
        transport = RequestsHttpTransport()
        for adapter in transport._session.adapters.values():
            assert adapter.max_retries.total == 0

    def test_transport_disables_redirect_following(self):
        """A 307/308 redirect replays the body, which is a duplicate create."""
        import inspect

        import requests
        from publishing.requests_transport import RequestsHttpTransport
        source = inspect.getsource(RequestsHttpTransport.send)
        assert "allow_redirects=False" in source
        # requests would replay a 307/308 body; confirm that is really the case.
        from requests.sessions import SessionRedirectMixin
        assert "rebuild_method" in inspect.getsource(
            SessionRedirectMixin.resolve_redirects)

    def test_gateway_code_contains_no_retry_loop(self):
        code = gateway_code("publishing/wordpress.py").lower()
        for token in ("max_retries", "backoff", "retry", "for _ in range", "attempts"):
            assert token not in code, f"gateway code contains {token!r}"

    def test_gateway_has_no_loop_that_reissues_a_create(self):
        """The only loop over a request collection is the read-only lookup scan."""
        import ast
        tree = ast.parse((ROOT / "publishing" / "wordpress.py").read_text(encoding="utf-8"))
        publish = next(n for n in ast.walk(tree)
                       if isinstance(n, ast.FunctionDef) and n.name == "publish")
        assert not any(isinstance(n, (ast.For, ast.While))
                       for n in ast.walk(publish)), "publish() must not loop"


# -- 27-29: safe codes and secret hygiene -----------------------------------


class TestSafeCodesAndSecrets:
    @pytest.mark.parametrize("status", [400, 401, 403, 404, 408, 422, 429, 500, 503, 302])
    def test_every_status_yields_a_safe_error_code(self, status):
        gateway, _ = make_gateway([HttpResponse(status_code=status, body_text="{}")])
        outcome = gateway.publish(make_command(ContentType.POST))
        assert outcome.error_code in SAFE_PUBLICATION_ERROR_CODES

    @pytest.mark.parametrize("state,code", [
        (TransmissionState.UNKNOWN, "READ_TIMEOUT"),
        (TransmissionState.UNKNOWN, "CONNECTION_LOST"),
        (TransmissionState.NOT_SENT, "CONNECTION_NOT_ESTABLISHED"),
    ])
    def test_every_transport_failure_yields_a_safe_code(self, state, code):
        gateway, _ = make_gateway([TransportError(code, state)])
        outcome = gateway.publish(make_command(ContentType.POST))
        assert outcome.error_code in SAFE_PUBLICATION_ERROR_CODES

    @pytest.mark.parametrize("body", [
        "{\"code\":\"rest_forbidden\",\"message\":\"Anonymous user cannot edit\"}",
        "<html><body>Fatal error: allowed memory size exhausted</body></html>",
        "Traceback (most recent call last): ...",
    ])
    def test_response_body_never_reaches_the_outcome(self, body):
        gateway, _ = make_gateway([HttpResponse(status_code=400, body_text=body)])
        outcome = gateway.publish(make_command(ContentType.POST))
        assert "Fatal" not in repr(outcome)
        assert "Traceback" not in repr(outcome)
        assert "Anonymous" not in repr(outcome)
        assert outcome.error_code == "INVALID_REQUEST"

    def test_secret_is_absent_from_connection_repr(self):
        connection = make_connection()
        assert SECRET not in repr(connection)
        assert SECRET not in str(connection)
        assert SECRET not in f"{connection}"

    def test_secret_is_absent_from_publish_outcome(self):
        gateway, _ = make_gateway([created()])
        outcome = gateway.publish(make_command(ContentType.POST))
        assert SECRET not in repr(outcome)
        assert SECRET not in str(outcome)

    def test_secret_is_absent_from_failure_outcome(self):
        gateway, _ = make_gateway([HttpResponse(status_code=500, body_text=SECRET)])
        outcome = gateway.publish(make_command(ContentType.POST))
        assert SECRET not in repr(outcome)

    def test_secret_is_absent_from_gateway_repr(self):
        gateway, _ = make_gateway([created()])
        assert SECRET not in repr(gateway)

    def test_secret_is_never_in_the_url(self):
        gateway, transport = make_gateway([created()])
        gateway.publish(make_command(ContentType.POST))
        assert SECRET not in transport.urls[0]
        assert "@" not in transport.urls[0].split("://", 1)[1].split("/")[0]

    def test_secret_is_never_in_a_query_string(self):
        gateway, transport = make_gateway([page_response([]), page_response([])])
        gateway.find_by_marker(reconciliation_marker(str(uuid4())))
        for request in transport.requests:
            for key, value in request.query:
                assert SECRET not in key and SECRET not in value

    def test_credentials_travel_in_the_authorization_header_only(self):
        gateway, transport = make_gateway([created()])
        gateway.publish(make_command(ContentType.POST))
        request = transport.requests[0]
        assert request.headers["Authorization"].startswith("Basic ")
        assert SECRET not in str(request.json_body)

    def test_no_header_other_than_authorization_carries_a_secret(self):
        """A secret must not ride along in User-Agent or any other header."""
        gateway, transport = make_gateway([created()])
        gateway.publish(make_command(ContentType.POST))
        for request in transport.requests:
            for name, value in request.headers.items():
                if name.lower() == "authorization":
                    continue
                assert SECRET not in value, f"secret leaked into header {name}"

    def test_user_agent_is_a_fixed_constant_not_derived_from_credentials(self):
        import base64
        gateway, transport = make_gateway([created()])
        gateway.publish(make_command(ContentType.POST))
        agent = transport.requests[0].headers["User-Agent"]
        assert SECRET not in agent
        assert base64.b64encode(f"alice:{SECRET}".encode()).decode() not in agent
        # The same constant regardless of who publishes.
        assert agent == "ai-wordpress-factory/1.0 (+publishing-gateway)"

    def test_basic_auth_header_matches_wordpress_application_passwords(self):
        import base64
        header = basic_auth_header("alice", SECRET)
        assert header == "Basic " + base64.b64encode(
            f"alice:{SECRET}".encode()).decode()

    def test_base_url_with_embedded_credentials_is_rejected(self):
        with pytest.raises(ValueError):
            WordPressConnection(base_url="https://user:pass@wp.example.test",
                                username="a", application_password="b")

    def test_non_http_base_url_is_rejected(self):
        with pytest.raises(ValueError):
            WordPressConnection(base_url="ftp://wp.example.test",
                                username="a", application_password="b")

    def test_empty_credentials_are_rejected(self):
        for kwargs in ({"username": "", "application_password": "x"},
                       {"username": "u", "application_password": "  "}):
            with pytest.raises(ValueError):
                WordPressConnection(base_url=BASE_URL, **kwargs)


# -- 30-32: timeout and coupling --------------------------------------------


class TestTimeoutAndCoupling:
    def test_timeout_is_explicitly_finite(self):
        assert 0 < DEFAULT_TIMEOUT_SECONDS < float("inf")
        gateway, transport = make_gateway([created()])
        gateway.publish(make_command(ContentType.POST))
        assert transport.timeouts == [DEFAULT_TIMEOUT_SECONDS]

    def test_timeout_is_configurable(self):
        gateway, transport = make_gateway([created()], timeout=5.0)
        gateway.publish(make_command(ContentType.POST))
        assert transport.timeouts == [5.0]

    def test_non_positive_timeout_is_rejected(self):
        for bad in (0, -1, -0.5):
            with pytest.raises(GatewayInputRejected):
                make_gateway([created()], timeout=bad)

    def test_boolean_timeout_is_rejected(self):
        with pytest.raises(GatewayInputRejected):
            make_gateway([created()], timeout=True)

    def test_gateway_does_not_read_ambient_state(self):
        for module in GATEWAY_MODULES:
            code = gateway_code(module)
            for banned in ("os.environ", "getenv", "environ", "config.json",
                           "load_config", "Config("):
                assert banned not in code, f"{module} reads ambient state: {banned}"

    def test_gateway_imports_no_os_module(self):
        for module in GATEWAY_MODULES:
            code = gateway_code(module)
            assert "import os" not in code
            assert "from os" not in code

    def test_gateway_imports_no_persistence(self):
        for module in GATEWAY_MODULES:
            code = gateway_code(module)
            for banned in ("sqlite3", "repository", "PublicationRequest",
                           "PublicationLease", "claim_publication", "persistence",
                           "workspace", "TaskRun", "RunLease"):
                assert banned not in code, f"{module} references {banned}"

    def test_gateway_does_not_know_task_run_or_lease(self):
        code = gateway_code("publishing/wordpress.py")
        for banned in ("TaskRun", "PublicationLease", "RunLease", "claim_publication",
                       "heartbeat", "expire", "fencing"):
            assert banned not in code, f"gateway code references {banned}"

    def test_publishing_package_exposes_no_executor(self):
        import ast
        source = (ROOT / "publishing" / "wordpress.py").read_text(encoding="utf-8")
        code = strip_comments_and_docstrings(source)
        assert "PublishCommandService" not in code
        tree = ast.parse(source)
        public = {n.name for n in tree.body
                  if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")}
        assert public == set()  # no free functions: the gateway is the only entry

    def test_domain_publication_module_makes_no_network_call(self):
        code = gateway_code("domain/publication.py")
        for banned in ("requests", "urllib", "http.client", "socket", "urlopen"):
            assert banned not in code, f"domain code references {banned}"

    def test_legacy_publisher_is_untouched(self):
        """3C3 must not extend tools/wordpress.py."""
        import subprocess
        result = subprocess.run(["git", "diff", "--name-only", "--", "tools/wordpress.py"],
                                cwd=ROOT, capture_output=True, text=True)
        assert result.stdout.strip() == ""


# -- 33-42: reconciliation lookup ------------------------------------------


def marker_item(remote_id: int, *, marker: str | None = None, link: str | None = None,
                content_type: ContentType = ContentType.POST) -> dict:
    return {
        "id": remote_id,
        "link": link,
        "content": {"raw": f"<p>Body</p>\n\n<!-- {marker} -->" if marker else "<p>Body</p>"},
    }


def page_response(items: list[dict], total_pages: int = 1) -> HttpResponse:
    return HttpResponse(status_code=200, body_text=json.dumps(items),
                        headers={"x-wp-totalpages": str(total_pages)})


class TestReconciliationLookup:
    def test_lookup_searches_posts_and_pages(self):
        gateway, transport = make_gateway([page_response([]), page_response([])])
        gateway.find_by_marker(reconciliation_marker(str(uuid4())))
        urls = transport.urls
        assert f"{BASE_URL}/wp-json/wp/v2/posts" in urls
        assert f"{BASE_URL}/wp-json/wp/v2/pages" in urls

    def test_lookup_finds_marker_in_a_post(self):
        publication_id = str(uuid4())
        marker = reconciliation_marker(publication_id)
        gateway, _ = make_gateway([
            page_response([marker_item(11, marker=marker)]), page_response([])])
        matches = gateway.find_by_marker(marker)
        assert [m.remote_resource_id for m in matches] == [11]
        assert matches[0].content_type is ContentType.POST

    def test_lookup_finds_marker_in_a_page(self):
        publication_id = str(uuid4())
        marker = reconciliation_marker(publication_id)
        gateway, _ = make_gateway([
            page_response([]), page_response([marker_item(22, marker=marker)])])
        matches = gateway.find_by_marker(marker)
        assert [m.remote_resource_id for m in matches] == [22]
        assert matches[0].content_type is ContentType.PAGE

    def test_post_and_page_ids_are_not_conflated(self):
        """Posts and pages have independent id spaces in WordPress."""
        publication_id = str(uuid4())
        marker = reconciliation_marker(publication_id)
        gateway, _ = make_gateway([
            page_response([marker_item(42, marker=marker)]),
            page_response([marker_item(42, marker=marker)])])
        matches = gateway.find_by_marker(marker)
        assert len(matches) == 2
        assert {m.content_type for m in matches} == {ContentType.POST, ContentType.PAGE}

    def test_all_matches_are_returned_and_none_is_chosen(self):
        publication_id = str(uuid4())
        marker = reconciliation_marker(publication_id)
        gateway, _ = make_gateway([
            page_response([marker_item(9, marker=marker),
                           marker_item(3, marker=marker)]), page_response([])])
        matches = gateway.find_by_marker(marker)
        assert sorted(m.remote_resource_id for m in matches) == [3, 9]

    def test_exact_marker_matching_is_required(self):
        """A near-miss, a different publication id, and case variants must not match."""
        publication_id = str(uuid4())
        marker = reconciliation_marker(publication_id)
        wrong_id = reconciliation_marker(str(uuid4()))
        body = [
            marker_item(1, marker=marker.replace("v1:", "v2:")),   # version differs
            marker_item(2, marker=wrong_id),                        # id differs
            marker_item(3, marker=marker[:-1]),                     # truncated
            marker_item(4, marker=marker.upper()),                  # case differs
        ]
        gateway, _ = make_gateway([page_response(body), page_response([])])
        assert gateway.find_by_marker(marker) == ()

    def test_slug_is_not_used_for_reconciliation(self):
        publication_id = str(uuid4())
        marker = reconciliation_marker(publication_id)
        items = [{"id": 8, "slug": "shared-slug", "link": "https://wp.example.test/shared-slug/",
                  "content": {"raw": "<p>no marker here</p>"}}]
        gateway, _ = make_gateway([page_response(items), page_response([])])
        assert gateway.find_by_marker(marker) == ()

    def test_title_is_not_used_for_reconciliation(self):
        publication_id = str(uuid4())
        marker = reconciliation_marker(publication_id)
        items = [{"id": 8, "title": {"rendered": "Title"},
                  "content": {"raw": "<p>no marker</p>"}}]
        gateway, _ = make_gateway([page_response(items), page_response([])])
        assert gateway.find_by_marker(marker) == ()

    def test_search_parameter_is_not_used(self):
        """WordPress search= is tokenized and cannot support exact matching."""
        publication_id = str(uuid4())
        gateway, transport = make_gateway([page_response([]), page_response([])])
        gateway.find_by_marker(reconciliation_marker(publication_id))
        assert transport.requests, "lookup issued no request"
        for request in transport.requests:
            keys = {k for k, _ in request.query}
            assert "search" not in keys

    def test_empty_result_is_returned_only_for_a_complete_scan(self):
        gateway, _ = make_gateway([page_response([]), page_response([])])
        assert gateway.find_by_marker(reconciliation_marker(str(uuid4()))) == ()

    def test_failed_page_never_becomes_empty_result(self):
        gateway, _ = make_gateway([
            TransportError("READ_TIMEOUT", TransmissionState.UNKNOWN),
            page_response([])])
        with pytest.raises(ReconciliationLookupUnresolved):
            gateway.find_by_marker(reconciliation_marker(str(uuid4())))

    def test_non_200_lookup_page_is_unresolved(self):
        gateway, _ = make_gateway([HttpResponse(status_code=401, body_text="{}"), page_response([])])
        with pytest.raises(ReconciliationLookupUnresolved):
            gateway.find_by_marker(reconciliation_marker(str(uuid4())))

    def test_unparseable_lookup_page_is_unresolved(self):
        gateway, _ = make_gateway([HttpResponse(status_code=200, body_text="{not json"), page_response([])])
        with pytest.raises(ReconciliationLookupUnresolved):
            gateway.find_by_marker(reconciliation_marker(str(uuid4())))

    def test_missing_raw_content_is_unresolved(self):
        """Without content.raw, absence cannot be proven either way."""
        items = [{"id": 1, "content": {"rendered": "<p>Body</p>"}}]
        gateway, _ = make_gateway([page_response(items), page_response([])])
        with pytest.raises(ReconciliationLookupUnresolved):
            gateway.find_by_marker(reconciliation_marker(str(uuid4())))

    def test_lookup_failure_is_distinguishable_from_zero_matches(self):
        failing, _ = make_gateway([HttpResponse(status_code=500, body_text="{}"), page_response([])])
        empty, _ = make_gateway([page_response([]), page_response([])])
        marker = reconciliation_marker(str(uuid4()))
        with pytest.raises(ReconciliationLookupUnresolved):
            failing.find_by_marker(marker)
        assert empty.find_by_marker(marker) == ()

    def test_pagination_follows_total_pages_and_terminates(self):
        publication_id = str(uuid4())
        marker = reconciliation_marker(publication_id)
        gateway, transport = make_gateway([
            page_response([marker_item(1)], total_pages=3),
            page_response([marker_item(2)], total_pages=3),
            page_response([marker_item(3, marker=marker)], total_pages=3),
            # The /pages scan that follows must see its own scripted pages.
            page_response([marker_item(99, marker=marker)], total_pages=1),
        ])
        matches = gateway.find_by_marker(marker)
        # Found once in posts and once in pages; neither scan dropped it.
        assert sorted(m.remote_resource_id for m in matches) == [3, 99]
        assert transport.call_count == 4  # 3 post pages + 1 page page

    def test_pagination_terminates_without_a_total_header(self):
        """A short page ends the scan when X-WP-TotalPages is absent."""
        full_page = [marker_item(i) for i in range(100)]      # per_page, so continue
        short_page = [marker_item(1000)]                     # fewer than per_page, so stop
        gateway, transport = make_gateway([
            HttpResponse(status_code=200, body_text=json.dumps(full_page)),
            HttpResponse(status_code=200, body_text=json.dumps(short_page)),
            page_response([]),
        ])
        gateway.find_by_marker(reconciliation_marker(str(uuid4())))
        # 2 posts pages (short page ends it) + 1 pages page.
        assert transport.call_count == 3

    def test_scan_stops_immediately_on_an_empty_first_page(self):
        gateway, transport = make_gateway([page_response([]), page_response([])])
        assert gateway.find_by_marker(reconciliation_marker(str(uuid4()))) == ()
        assert transport.call_count == 2  # one page per collection, nothing more

    def test_marker_found_on_later_page_is_not_missed(self):
        publication_id = str(uuid4())
        marker = reconciliation_marker(publication_id)
        gateway, _ = make_gateway([
            page_response([marker_item(1)], total_pages=2),
            page_response([marker_item(2, marker=marker)], total_pages=2),
            page_response([]),
        ])
        matches = gateway.find_by_marker(marker)
        assert [m.remote_resource_id for m in matches] == [2]

    def test_bounded_page_size_is_requested(self):
        gateway, transport = make_gateway([page_response([]), page_response([])])
        gateway.find_by_marker(reconciliation_marker(str(uuid4())))
        for request in transport.requests:
            query = dict(request.query)
            assert query["per_page"] == "100"
            assert query["context"] == "edit"

    def test_pagination_never_loops_forever(self):
        """A server that always claims more pages must still terminate."""
        gateway, transport = make_gateway([page_response([marker_item(1)], total_pages=10_000)])
        with pytest.raises(ReconciliationLookupUnresolved):
            gateway.find_by_marker(reconciliation_marker(str(uuid4())))
        assert transport.call_count == 1000

    def test_lookup_requires_context_edit_for_raw_content(self):
        code = gateway_code("publishing/wordpress.py")
        assert "context" in code and "edit" in code
        # raw content is the only field the matcher reads.
        assert '"raw"' in code

    def test_malformed_item_is_unresolved(self):
        gateway, _ = make_gateway([page_response(["not a dict"]), page_response([])])
        with pytest.raises(ReconciliationLookupUnresolved):
            gateway.find_by_marker(reconciliation_marker(str(uuid4())))

    def test_match_without_valid_id_is_unresolved(self):
        publication_id = str(uuid4())
        marker = reconciliation_marker(publication_id)
        items = [{"id": 0, "content": {"raw": f"<!-- {marker} -->"}}]
        gateway, _ = make_gateway([page_response(items), page_response([])])
        with pytest.raises(ReconciliationLookupUnresolved):
            gateway.find_by_marker(marker)

    def test_lookup_rejects_blank_marker(self):
        gateway, _ = make_gateway([page_response([]), page_response([])])
        for bad in ("", "   ", None):
            with pytest.raises(GatewayInputRejected):
                gateway.find_by_marker(bad)

    def test_gateway_does_not_classify_matches_itself(self):
        """Found/NOT_FOUND/AMBIGUOUS policy stays in the domain."""
        code = gateway_code("publishing/wordpress.py")
        assert "classify_reconciliation" not in code
        assert "ReconciliationMatch" not in code
        assert "NOT_FOUND" not in code
        assert "AMBIGUOUS" not in code

    def test_matches_are_returned_not_a_domain_outcome(self):
        publication_id = str(uuid4())
        marker = reconciliation_marker(publication_id)
        gateway, _ = make_gateway([
            page_response([marker_item(5, marker=marker)]), page_response([])])
        result = gateway.find_by_marker(marker)
        assert isinstance(result, tuple)
        assert all(isinstance(m, RemoteReference) for m in result)

    def test_caller_can_apply_domain_policy_to_gateway_matches(self):
        from domain.publication import classify_reconciliation
        publication_id = str(uuid4())
        marker = reconciliation_marker(publication_id)
        gateway, _ = make_gateway([
            page_response([marker_item(1, marker=marker),
                           marker_item(2, marker=marker)]), page_response([])])
        matches = gateway.find_by_marker(marker)
        kind, references = classify_reconciliation(list(matches))
        assert kind.value == "AMBIGUOUS"
        assert len(references) == 2


# -- 43-45: prior contracts stay green --------------------------------------


class TestSharedContractDefects:
    def test_no_shared_contract_defect_was_silently_worked_around(self):
        """3C3 reports shared-contract issues instead of editing tools/."""
        import subprocess
        changed = subprocess.run(["git", "status", "--porcelain", "--", "tools/"],
                                 cwd=ROOT, capture_output=True, text=True)
        assert changed.stdout.strip() == ""

    def test_publish_command_contract_is_unchanged_in_shape(self):
        import dataclasses
        fields = {f.name for f in dataclasses.fields(PublishCommand)}
        assert fields == {
            "publication_id", "task_id", "content_version_id", "content_type", "title",
            "content", "reconciliation_marker", "slug", "category_ids", "tag_ids",
            "excerpt", "featured_media_id"}

    def test_remote_reference_carries_exactly_the_agreed_identity(self):
        """content_type joined the record in 3C3A; nothing else was added."""
        import dataclasses
        assert {f.name for f in dataclasses.fields(RemoteReference)} == {
            "content_type", "remote_resource_id", "remote_url"}

    def test_remote_reference_carries_no_endpoint_or_local_identity(self):
        import dataclasses
        names = {f.name for f in dataclasses.fields(RemoteReference)}
        for banned in ("endpoint", "collection", "url_path", "slug", "title", "marker",
                       "workspace_id", "task_id", "publication_id", "username",
                       "application_password"):
            assert banned not in names


class TestRemoteIdentityIsContentTypeScoped:
    """3C3A: remote identity is (content_type, remote_resource_id).

    WordPress allocates post and page ids from independent sequences, so post
    100 and page 100 are two different resources. 3C3 proved this and 3C2 keyed
    uniqueness on the bare integer, which rejected that valid result.
    """

    def test_domain_policy_accepts_a_post_and_page_id_collision(self):
        from domain.publication import classify_reconciliation
        post = RemoteReference(content_type=ContentType.POST, remote_resource_id=100,
                               remote_url="https://x/?p=100")
        page = RemoteReference(content_type=ContentType.PAGE, remote_resource_id=100,
                               remote_url="https://x/?page_id=100")
        kind, matches = classify_reconciliation([post, page])
        assert kind is ReconciliationMatch.AMBIGUOUS
        assert matches == (post, page)

    def test_post_100_and_page_100_are_different_references(self):
        post = RemoteReference(content_type=ContentType.POST, remote_resource_id=100)
        page = RemoteReference(content_type=ContentType.PAGE, remote_resource_id=100)
        assert post != page
        assert post.remote_identity != page.remote_identity

    def test_duplicate_post_identity_still_fails_closed(self):
        from domain.publication import classify_reconciliation
        with pytest.raises(ValueError, match="distinct remote resources"):
            classify_reconciliation([
                RemoteReference(content_type=ContentType.POST, remote_resource_id=100),
                RemoteReference(content_type=ContentType.POST, remote_resource_id=100)])

    def test_duplicate_page_identity_still_fails_closed(self):
        from domain.publication import classify_reconciliation
        with pytest.raises(ValueError, match="distinct remote resources"):
            classify_reconciliation([
                RemoteReference(content_type=ContentType.PAGE, remote_resource_id=100),
                RemoteReference(content_type=ContentType.PAGE, remote_resource_id=100)])

    def test_gateway_returns_both_collisions_with_distinct_types(self):
        publication_id = str(uuid4())
        marker = reconciliation_marker(publication_id)
        gateway, _ = make_gateway([
            page_response([marker_item(100, marker=marker)]),
            page_response([marker_item(100, marker=marker)]),
        ])
        matches = gateway.find_by_marker(marker)
        assert len(matches) == 2
        assert {m.content_type for m in matches} == {ContentType.POST, ContentType.PAGE}
        # And the domain policy now accepts the gateway's own output.
        from domain.publication import classify_reconciliation
        assert classify_reconciliation(matches)[0] is ReconciliationMatch.AMBIGUOUS

    def test_ambiguity_is_still_never_resolved_by_the_gateway(self):
        publication_id = str(uuid4())
        marker = reconciliation_marker(publication_id)
        gateway, _ = make_gateway([
            page_response([marker_item(100, marker=marker)]),
            page_response([marker_item(100, marker=marker)]),
        ])
        assert len(gateway.find_by_marker(marker)) == 2

    def test_remote_match_wrapper_is_gone(self):
        """No second source of truth for collection metadata."""
        import publishing.wordpress as gateway_module
        assert not hasattr(gateway_module, "RemoteMatch")
        assert "RemoteMatch" not in gateway_module.__all__
        code = gateway_code("publishing/wordpress.py")
        assert "RemoteMatch" not in code


class TestPublishCarriesContentTypeIntoRemoteIdentity:
    def test_post_success_returns_a_post_reference(self):
        gateway, _ = make_gateway([created(100)])
        outcome = gateway.publish(make_command(ContentType.POST))
        assert outcome.kind is PublishOutcomeKind.CONFIRMED_SUCCESS
        assert outcome.remote.content_type is ContentType.POST
        assert outcome.remote.remote_resource_id == 100

    def test_page_success_returns_a_page_reference(self):
        gateway, _ = make_gateway([created(100)])
        outcome = gateway.publish(make_command(ContentType.PAGE))
        assert outcome.kind is PublishOutcomeKind.CONFIRMED_SUCCESS
        assert outcome.remote.content_type is ContentType.PAGE
        assert outcome.remote.remote_resource_id == 100

    def test_same_id_from_two_content_types_is_two_outcomes(self):
        post_gateway, _ = make_gateway([created(100)])
        page_gateway, _ = make_gateway([created(100)])
        post = post_gateway.publish(make_command(ContentType.POST)).remote
        page = page_gateway.publish(make_command(ContentType.PAGE)).remote
        assert post != page
        assert post.remote_identity != page.remote_identity

    def test_remote_url_is_still_preserved_with_the_type(self):
        gateway, _ = make_gateway([created(100, "https://x/?p=100")])
        outcome = gateway.publish(make_command(ContentType.POST))
        assert outcome.remote.remote_url == "https://x/?p=100"
        assert outcome.remote.content_type is ContentType.POST

    def test_success_invariants_are_unchanged(self):
        """The type addition must not relax the outcome contract."""
        gateway, _ = make_gateway([created()])
        outcome = gateway.publish(make_command(ContentType.POST))
        assert outcome.error_code is None
        assert outcome.publication_state.value == "SUCCEEDED"
        with pytest.raises(ValueError):
            PublishOutcome(kind=PublishOutcomeKind.CONFIRMED_FAILURE, error_code="TIMEOUT",
                           remote=RemoteReference(content_type=ContentType.POST,
                                                  remote_resource_id=1))

    def test_reference_requires_a_content_type(self):
        for bad in (None, "POST", "post", 1):
            with pytest.raises(ValueError):
                RemoteReference(content_type=bad, remote_resource_id=1)

    def test_omitting_content_type_is_refused(self):
        """Identity is not optional: a reference with no type is unbuildable.

        It may surface as TypeError (missing required argument) or ValueError
        (a defaulted None fails the ContentType check); both are closed refusals.
        A regression that let a typeless reference through would make
        (content_type, id) non-unique and silently break reconciliation.
        """
        with pytest.raises((TypeError, ValueError)):
            RemoteReference(remote_resource_id=1)
        with pytest.raises((TypeError, ValueError)):
            RemoteReference()


class TestLookupCarriesRealContentType:
    def test_post_match_reports_post(self):
        publication_id = str(uuid4())
        marker = reconciliation_marker(publication_id)
        gateway, _ = make_gateway([
            page_response([marker_item(11, marker=marker)]), page_response([])])
        matches = gateway.find_by_marker(marker)
        assert [m.content_type for m in matches] == [ContentType.POST]

    def test_page_match_reports_page(self):
        publication_id = str(uuid4())
        marker = reconciliation_marker(publication_id)
        gateway, _ = make_gateway([
            page_response([]), page_response([marker_item(22, marker=marker)])])
        matches = gateway.find_by_marker(marker)
        assert [m.content_type for m in matches] == [ContentType.PAGE]

    def test_lookup_type_comes_from_the_collection_scanned(self):
        """Not from the marker's publication, but from where it was found."""
        publication_id = str(uuid4())
        marker = reconciliation_marker(publication_id)
        gateway, _ = make_gateway([
            page_response([marker_item(5, marker=marker)]), page_response([])])
        assert gateway.find_by_marker(marker)[0].content_type is ContentType.POST

    def test_marker_semantics_are_unchanged_by_the_identity_fix(self):
        """3C3A touches identity only; the marker format must not drift."""
        publication_id = str(uuid4())
        assert reconciliation_marker(publication_id) == (
            f"ai-wordpress-factory:publication:v1:{publication_id}")
        assert embedded_reconciliation_marker(publication_id) == (
            f"<!-- ai-wordpress-factory:publication:v1:{publication_id} -->")
        command = make_command(ContentType.POST)
        gateway, transport = make_gateway([created()])
        gateway.publish(command)
        assert transport.requests[0].json_body["content"].count(
            command.reconciliation_marker) == 1


class TestNoSchemaMigrationRequired:
    """The durable row already carries both halves of remote identity."""

    def test_publication_table_persists_content_type_and_remote_id(self):
        import re
        sql = (ROOT / "persistence" / "migrations" / "0010_publication_claim.sql").read_text()
        assert re.search(r"content_type\s+TEXT\s+NOT NULL", sql)
        assert re.search(r"remote_resource_id\s+INTEGER", sql)

    def test_content_type_is_immutable_once_written(self):
        """Identity cannot drift after the fact if the column cannot change."""
        import re
        sql = (ROOT / "persistence" / "migrations" / "0010_publication_claim.sql").read_text()
        assert "NEW.content_type IS NOT OLD.content_type" in sql

    def test_no_migration_was_added_by_this_hotfix(self):
        import subprocess
        result = subprocess.run(["git", "status", "--porcelain", "--", "persistence/"],
                                cwd=ROOT, capture_output=True, text=True)
        assert result.stdout.strip() == ""

    def test_persisted_identity_is_interpretable_from_the_existing_row(self):
        """(publication.content_type, remote_resource_id) reconstructs identity."""
        from domain.publication import PublicationRequest, PublicationState
        now = "2026-01-01T00:00:00+00:00"
        request = PublicationRequest(
            publication_id=str(uuid4()), workspace_id=str(uuid4()), task_id=str(uuid4()),
            content_version_id=str(uuid4()), approved_run_id=str(uuid4()),
            content_type=ContentType.PAGE, idempotency_key=str(uuid4()),
            state=PublicationState.SUCCEEDED, created_at=now, updated_at=now,
            remote_resource_id=100, remote_url="https://x/?page_id=100")
        reference = RemoteReference(content_type=request.content_type,
                                    remote_resource_id=request.remote_resource_id,
                                    remote_url=request.remote_url)
        assert reference.remote_identity == (ContentType.PAGE, 100)
    def test_outcome_types_are_unchanged(self):
        assert {k.value for k in PublishOutcomeKind} == {
            "CONFIRMED_SUCCESS", "CONFIRMED_FAILURE", "OUTCOME_UNKNOWN"}

    def test_publish_command_module_has_no_network_dependency(self):
        code = gateway_code("domain/publication.py")
        assert "requests" not in code
        assert "publishing" not in code
