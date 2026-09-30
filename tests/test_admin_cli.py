"""Tests for the operator provisioning CLI (8.3-4F1).

What is under test
------------------
``python -m admin publishing-target add`` is the supported way to make the
publication path configurable. It must be a thin, honest front end over contracts
that already exist -- the ``PublishingTarget`` domain object, the scoped
repository's workspace and active-target reads, and ``add_publishing_target`` --
and it must never become a second authority or a place a secret can appear.

Isolation is proven against real SQLite, not a mock, for the same reason
``test_publishing_target.py`` is: the guarantee that matters most here (one
ACTIVE target per workspace) is a partial unique index in the database, and a
mock would accept whatever the CLI happened to do.

Nothing in this file contacts WordPress, and nothing resolves a credential.
"""
from __future__ import annotations

import ast
import io
import os
import re
import sqlite3
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from admin import __main__ as cli
from domain.providers import Workspace, WorkspaceStatus
from persistence.connection import ConnectionFactory
from persistence.migration_runner import migrate
from persistence.repository import SQLiteStore

ROOT = Path(__file__).resolve().parents[1]
CLI_SOURCE = (ROOT / "admin" / "__main__.py").read_text(encoding="utf-8")
DOCS = (ROOT / "docs" / "http-api.md").read_text(encoding="utf-8")

# A value that must never reach the database, the console, or an error message.
FAKE_SECRET = 'fake-application-password-value-9f2c'


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """An isolated migrated database, reached through the CLI's own configuration."""
    database = tmp_path / "provision.sqlite3"
    monkeypatch.setenv("AIWF_DATABASE", str(database))
    # The referenced variable is deliberately set, so a test that proved the CLI
    # resolved it would pass loudly here rather than fail for a missing variable.
    monkeypatch.setenv("AIWF_WP_APP_PASSWORD", FAKE_SECRET)
    return database


def _workspace_id(database):
    factory = ConnectionFactory(str(database))
    migrate(factory)
    handle = SQLiteStore(factory)
    with handle.reader() as repo:
        return repo.default_workspace().workspace_id


def _archive(database, workspace_id):
    handle = SQLiteStore(ConnectionFactory(str(database)))
    with handle.transaction() as internal:
        row = internal._conn.execute(
            "SELECT * FROM workspaces WHERE workspace_id=?", (workspace_id,)).fetchone()
        data = dict(row)
        data['status'] = WorkspaceStatus.ARCHIVED.value
        internal.update_workspace(Workspace(**data))


def _run(*argv):
    """Invoke the CLI exactly as an operator would, capturing its streams."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def _add_args(workspace_id, **overrides):
    args = {
        "--workspace-id": workspace_id,
        "--base-url": "https://example.test",
        "--username": "wp-user",
        "--credential-reference": "env:AIWF_WP_APP_PASSWORD",
    }
    args.update(overrides)
    argv = ["publishing-target", "add"]
    for flag, value in args.items():
        argv += [flag, value]
    return argv


# -- 1-3: a valid target is provisioned and persisted exactly ---------------

def test_valid_target_is_provisioned_and_persisted_exactly(store):
    workspace_id = _workspace_id(store)
    code, out, err = _run(*_add_args(workspace_id))

    assert code == 0, err
    assert workspace_id in out
    handle = SQLiteStore(ConnectionFactory(str(store)))
    with handle.workspace_transaction(workspace_id) as repo:
        target = repo.active_publishing_target()
        assert target is not None
        # 2: provider and lifecycle come from the existing domain contract.
        assert target.provider_type.value == "WORDPRESS"
        assert target.status.value == "ACTIVE"
        # 3: the operator's values, with base_url normalized by the domain grammar.
        assert target.workspace_id == workspace_id
        assert target.base_url == "https://example.test"
        assert target.username == "wp-user"
        assert target.credential_reference == "env:AIWF_WP_APP_PASSWORD"
        # The new target's only possible version is 1, and its history exists.
        assert target.configuration_version == 1
        versions = repo.publishing_target_versions(target.target_id)
        assert [v.configuration_version for v in versions] == [1]
        assert versions[0].credential_reference == "env:AIWF_WP_APP_PASSWORD"
        # 4: the referenced secret is nowhere in the durable state.
        assert versions[0].base_url == "https://example.test"


def test_trailing_slash_is_normalized_by_the_domain_not_the_cli(store):
    workspace_id = _workspace_id(store)
    code, _, err = _run(*_add_args(workspace_id, **{"--base-url": "https://example.test/"}))
    assert code == 0, err
    handle = SQLiteStore(ConnectionFactory(str(store)))
    with handle.workspace_transaction(workspace_id) as repo:
        assert repo.active_publishing_target().base_url == "https://example.test"


# -- 4: the secret is never persisted, printed, or read ---------------------

def test_secret_value_is_never_persisted_or_printed(store):
    workspace_id = _workspace_id(store)
    code, out, err = _run(*_add_args(workspace_id))

    assert code == 0, err
    # Neither stream may contain the resolved value.
    assert FAKE_SECRET not in out
    assert FAKE_SECRET not in err
    # The whole database must be free of it, not just the target row.
    connection = sqlite3.connect(str(store))
    try:
        dump = "\n".join(connection.iterdump())
    finally:
        connection.close()
    assert FAKE_SECRET not in dump
    # The reference is stored; the value it points at is not.
    assert "env:AIWF_WP_APP_PASSWORD" in dump
    # Provisioning does not validate the credential, so it must not read it.
    assert "EnvironmentSecretResolver" not in CLI_SOURCE
    assert "os.environ.get('AIWF_WP_APP_PASSWORD')" not in CLI_SOURCE
    assert "environ[" not in CLI_SOURCE
    # Only AIWF_DATABASE is read from the environment.
    assert set(re.findall(r"os\.environ(?:\.get\(|\[)['\"]([A-Z_]+)", CLI_SOURCE)) == {"AIWF_DATABASE"}


def _code_only(text: str) -> str:
    """Strip comments so explanatory prose cannot satisfy or trip a code guard."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"#[^\n]*", "", text)


def _code_symbols(path=None):
    """Every string literal, attribute call and name in a module, docstrings excluded.

    Source scanning cannot answer "does this code touch a credential", because a
    comment or a docstring that *describes* the guarantee would satisfy it. This
    walks the AST and returns only what the code actually contains.
    """
    tree = ast.parse(CLI_SOURCE if path is None else Path(path).read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and isinstance(body, list) and body \
                and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            docstrings.add(id(body[0].value))
    strings = [n.value for n in ast.walk(tree)
               if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings]
    calls = [n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
    names = ({n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
             | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
             | {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                for a in n.names})
    return strings, calls, names


def _subparser(parser, name):
    """One level down into an argparse subparser tree."""
    for action in parser._actions:
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict) and name in choices:
            return choices[name]
    raise AssertionError(f"no subparser named {name}")


def test_no_secret_argument_exists():
    """The strongest form of the claim: assert the parser's complete argument set.

    Searching the source for "--password" would be satisfied by a comment saying
    there is no --password flag. Walking the built parser cannot be.
    """
    add = _subparser(_subparser(cli._build_parser(), "publishing-target"), "add")
    declared = set()
    for action in add._actions:
        declared.update(getattr(action, "option_strings", ()))
    assert declared == {
        "-h", "--help",
        "--workspace-id", "--base-url", "--username", "--credential-reference",
    }
    for forbidden in ("--password", "--application-password", "--app-password",
                      "--secret", "--token", "--credential", "--wp-password"):
        assert forbidden not in declared, forbidden
    # The metavar keeps an operator from mistaking the reference for the value.
    reference = next(a for a in add._actions if a.dest == "credential_reference")
    assert reference.metavar == "env:NAME"
    assert reference.required is True


# -- 6-9: every invalid input fails closed ---------------------------------

@pytest.mark.parametrize("reference", [
    "password123", "AIWF_WP_APP_PASSWORD", "env:", "env:lowercase",
    "env:AIWF WP", "env:AIWF-WP", "", "env:AIWF_WP_APP_PASSWORD; rm -rf /",
])
def test_invalid_credential_reference_is_rejected(store, reference):
    workspace_id = _workspace_id(store)
    code, out, err = _run(*_add_args(workspace_id, **{"--credential-reference": reference}))
    assert code == 2
    assert out == ""
    assert "Credential reference is invalid" in err
    # Nothing was written.
    handle = SQLiteStore(ConnectionFactory(str(store)))
    with handle.workspace_transaction(workspace_id) as repo:
        assert repo.active_publishing_target() is None
        assert repo.publishing_targets() == []


@pytest.mark.parametrize("base_url", [
    "ftp://example.test", "not-a-url", "https://", "https://user:pass@example.test",
    "https://example.test?a=1", "https://example.test#frag", "",
])
def test_invalid_base_url_is_rejected(store, base_url):
    workspace_id = _workspace_id(store)
    code, out, err = _run(*_add_args(workspace_id, **{"--base-url": base_url}))
    assert code == 2
    assert "rejected" in err
    handle = SQLiteStore(ConnectionFactory(str(store)))
    with handle.workspace_transaction(workspace_id) as repo:
        assert repo.publishing_targets() == []


@pytest.mark.parametrize("username", ["", "   "])
def test_invalid_username_is_rejected(store, username):
    workspace_id = _workspace_id(store)
    code, _, err = _run(*_add_args(workspace_id, **{"--username": username}))
    assert code == 2
    assert "rejected" in err


def test_unknown_workspace_is_rejected(store):
    _workspace_id(store)
    code, out, err = _run(*_add_args("no-such-workspace"))
    assert code == 2
    assert out == ""
    assert "Workspace not found or not ACTIVE" in err


def test_archived_workspace_is_rejected(store):
    workspace_id = _workspace_id(store)
    _archive(store, workspace_id)
    code, out, err = _run(*_add_args(workspace_id))
    assert code == 2
    assert out == ""
    assert "Workspace not found or not ACTIVE" in err
    # And nothing was written, not even a DISABLED row.
    handle = SQLiteStore(ConnectionFactory(str(store)))
    with handle.transaction() as internal:
        rows = internal._conn.execute(
            "SELECT COUNT(*) FROM publishing_targets").fetchone()[0]
    assert rows == 0


def test_duplicate_active_target_is_rejected_and_never_replaced(store):
    workspace_id = _workspace_id(store)
    assert _run(*_add_args(workspace_id))[0] == 0

    # A second add with different values must not overwrite the first.
    code, out, err = _run(*_add_args(
        workspace_id,
        **{"--base-url": "https://other.test", "--username": "someone-else",
           "--credential-reference": "env:OTHER_SECRET"}))

    assert code == 2
    assert out == ""
    assert "already exists" in err
    assert "does not replace it" in err

    handle = SQLiteStore(ConnectionFactory(str(store)))
    with handle.workspace_transaction(workspace_id) as repo:
        targets = repo.publishing_targets()
        # Exactly one row, and it is still the original configuration.
        assert len(targets) == 1
        assert targets[0].base_url == "https://example.test"
        assert targets[0].username == "wp-user"
        assert targets[0].credential_reference == "env:AIWF_WP_APP_PASSWORD"
        assert targets[0].configuration_version == 1
        # Its history was not rewritten either.
        versions = repo.publishing_target_versions(targets[0].target_id)
        assert [v.configuration_version for v in versions] == [1]


# -- 10: the CLI is not a second authority --------------------------------

def test_cli_issues_no_sql_and_reuses_existing_contracts():
    tree = ast.parse(CLI_SOURCE)
    # No raw SQL of any kind: no execute, no DML string, no sqlite3 import.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr != "execute", "the CLI must not issue SQL"
        if isinstance(node, ast.Import):
            assert all(alias.name != "sqlite3" for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            assert node.module != "sqlite3"
    for forbidden in ("INSERT", "UPDATE ", "DELETE FROM", "executemany", "executescript"):
        assert forbidden not in CLI_SOURCE, forbidden
    # It composes the existing domain and repository contracts by name.
    assert "from domain.publishing_target import PublishingTarget" in CLI_SOURCE
    assert "from domain.credential_reference import is_credential_reference" in CLI_SOURCE
    assert "repo.add_publishing_target(target)" in CLI_SOURCE
    assert "repo.active_publishing_target()" in CLI_SOURCE
    assert "repo.workspace()" in CLI_SOURCE
    # Database configuration is the single existing mechanism.
    assert "AIWF_DATABASE" in CLI_SOURCE
    assert "data/aiwf.sqlite3" in CLI_SOURCE
    # It does not invent rotation semantics: no call beyond the additive one. Checked
    # against the AST so a docstring naming the analogue cannot trip the guard.
    _strings, _calls, _names = _code_symbols()
    for forbidden in ("update_publishing_target_configuration",
                      "update_publishing_target_status", "with_configuration"):
        assert forbidden not in _calls, forbidden
        assert forbidden not in _names, forbidden


def test_cli_does_not_import_publication_runtime():
    for forbidden in ("publication_executor", "publication_reconciler", "wordpress",
                      "publication_bootstrap", "publication_loop", "request_publication"):
        assert forbidden not in CLI_SOURCE, forbidden


def test_help_requires_no_database_or_credential(tmp_path, monkeypatch, capsys):
    """`--help` must be inert: no AIWF_DATABASE, no AIWF_WP_APP_PASSWORD."""
    monkeypatch.delenv("AIWF_DATABASE", raising=False)
    monkeypatch.delenv("AIWF_WP_APP_PASSWORD", raising=False)
    with pytest.raises(SystemExit) as exit_info:
        cli._parse_args(["publishing-target", "add", "--help"])
    assert exit_info.value.code == 0
    assert "credential-reference" in capsys.readouterr().out


# -- 12-13: documentation matches actual startup behavior ------------------

def _code_blocks():
    return re.findall(r"```[a-z]*\n(.*?)```", DOCS, re.S)


def test_docs_document_the_working_uvicorn_factory_command():
    working = "python -m uvicorn api.app:get_app --factory --host 127.0.0.1 --port 8000"
    # The working form is present, and presented as the runnable command.
    assert working in DOCS
    assert any(working in block for block in _code_blocks())
    # The broken form must not be presented as anything an operator should run.
    # It may still be named in prose explaining why it fails, so the check is
    # scoped to code blocks rather than to the whole document.
    assert not any("api.app:app" in block for block in _code_blocks())
    # And the reason is documented, so the pitfall is not rediscovered later.
    assert "--factory" in DOCS
    assert "NoneType" in DOCS


def test_docs_do_not_recommend_exporting_the_env_prefixed_reference():
    # `export env:NAME=...` is not valid shell and would suggest the reference
    # itself is the secret. Only the bare variable name may be exported.
    assert re.search(r"^\s*export\s+env:", DOCS, re.M) is None
    assert "export env:" not in DOCS


def test_docs_state_the_secret_is_not_stored():
    assert "AIWF_WP_APP_PASSWORD" in DOCS
    assert "env:AIWF_WP_APP_PASSWORD" in DOCS
    lowered = DOCS.lower()
    assert "not stored" in lowered or "never stored" in lowered


# ---------------------------------------------------------------------------
# 8.3-4F2C Provider operational configuration: focused contract proofs
# ---------------------------------------------------------------------------

CONNECTION_ID = "9ac50c29-9366-4a03-9180-ae223b53e472"
WORKSPACE_ID = "9bf33b12-307b-4e08-a9fb-b84d88e1c162"
# A reference that must never be dereferenced. The command is expected to succeed
# with this variable absent from the environment.
SENTINEL_REFERENCE = "env:SHOULD_NOT_BE_RESOLVED"


@pytest.fixture(autouse=True)
def no_network():
    """Requirement 24: nothing in this module may reach a socket or the SDK."""
    with patch("openai.OpenAI", side_effect=AssertionError("SDK prohibited")), \
         patch("socket.socket.connect", side_effect=AssertionError("network prohibited")):
        yield


def _connection_id(store):
    """The seeded OPENAI connection id, read through the scoped repository."""
    handle = SQLiteStore(ConnectionFactory(str(store)))
    with handle.workspace_reader(WORKSPACE_ID) as repo:
        return repo.get_provider_connection(CONNECTION_ID).provider_connection_id


def _connection(store):
    """The stored connection, migrating first so test ordering never matters."""
    factory = ConnectionFactory(str(store))
    migrate(factory)
    handle = SQLiteStore(factory)
    with handle.workspace_reader(WORKSPACE_ID) as repo:
        return repo.get_provider_connection(CONNECTION_ID)


def _update_args(**overrides):
    args = {
        "--workspace-id": WORKSPACE_ID,
        "--provider-connection-id": CONNECTION_ID,
        "--default-model": "gpt-4.1",
    }
    args.update(overrides)
    argv = ["provider", "update"]
    for flag, value in args.items():
        argv += [flag, value]
    return argv


def _update_output(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


# -- 1-4: the command and its required arguments ---------------------------

def test_provider_update_command_exists():
    parser = cli._build_parser()
    provider = _subparser(parser, "provider")
    update = _subparser(provider, "update")
    names = {a.dest for a in update._actions if a.dest != "help"}
    assert names == {"workspace_id", "provider_connection_id", "default_model"}


@pytest.mark.parametrize("missing", ["--workspace-id", "--provider-connection-id", "--default-model"])
def test_provider_update_requires_each_argument(missing):
    argv = _update_args()
    index = argv.index(missing)
    del argv[index:index + 2]
    with pytest.raises(SystemExit) as exit_info:
        cli._parse_args(argv)
    assert exit_info.value.code != 0


# -- 5-7 + 16: the three permitted changes, and created_at untouched --------

def test_update_changes_model_version_and_updated_at(store):
    before = _connection(store)
    assert before.default_model == "gpt-4"
    assert before.configuration_version == 1

    code, out, err = _update_output(*_update_args())

    assert code == 0, err
    after = _connection(store)
    assert after.default_model == "gpt-4.1"
    assert after.configuration_version == 2
    assert after.updated_at != before.updated_at
    assert after.created_at == before.created_at


# -- 8-15: everything else is carried across untouched ----------------------

@pytest.mark.parametrize("attribute,expected", [
    ("provider_connection_id", CONNECTION_ID),
    ("workspace_id", WORKSPACE_ID),
    ("provider_type", "OPENAI"),
    ("provider_mode", "PLATFORM_MANAGED"),
    ("credential_reference", "env:OPENAI_API_KEY"),
    ("verification_status", "UNVERIFIED"),
])
def test_preserved_scalar_fields(store, attribute, expected):
    before = _connection(store)
    code, _, err = _update_output(*_update_args())
    assert code == 0, err
    after = _connection(store)
    assert getattr(after, attribute) == expected == getattr(before, attribute)


def test_preserved_capabilities_and_non_secret_configuration(store):
    before = _connection(store)
    code, _, err = _update_output(*_update_args())
    assert code == 0, err
    after = _connection(store)
    assert after.capabilities == before.capabilities
    assert [c.value for c in after.capabilities] == ["TEXT", "IMAGE", "VISUAL_QUALITY"]
    assert after.non_secret_configuration == before.non_secret_configuration == {}


# -- 5: version semantics --------------------------------------------------

def test_same_model_still_increments_the_version(store):
    """Project convention: a configuration change is versioned unconditionally.

    PublishingTarget.with_configuration increments configuration_version without
    ever comparing the old value, so the provider connection does the same.
    """
    assert _update_output(*_update_args())[0] == 0
    assert _connection(store).configuration_version == 2
    code, _, err = _update_output(*_update_args())
    assert code == 0, err
    after = _connection(store)
    assert after.default_model == "gpt-4.1"
    assert after.configuration_version == 3, "a same-value update is still versioned"


def test_caller_cannot_supply_the_version(store):
    with pytest.raises(SystemExit):
        cli._parse_args(_update_args() + ["--configuration-version", "9"])
    assert _connection(store).configuration_version == 1


# -- 17-20: failure paths leave the record alone ---------------------------

def test_cross_workspace_update_is_rejected(store):
    _workspace_id(store)
    other = "00000000-0000-0000-0000-000000000000"
    code, out, err = _update_output(*_update_args(**{"--workspace-id": other}))
    assert code == 2
    assert out == ""
    assert "not found in that workspace" in err
    assert _connection(store).default_model == "gpt-4"
    assert _connection(store).configuration_version == 1


def test_missing_provider_is_rejected(store):
    _workspace_id(store)
    code, out, err = _update_output(*_update_args(**{
        "--provider-connection-id": "11111111-1111-1111-1111-111111111111"}))
    assert code == 2
    assert out == ""
    assert "not found in that workspace" in err
    assert _connection(store).default_model == "gpt-4"


@pytest.mark.parametrize("model", ["bad model!", "gpt 4.1", "", "  ", "a" * 200])
def test_invalid_model_fails_through_domain_validation(store, model):
    _workspace_id(store)
    code, out, err = _update_output(*_update_args(**{"--default-model": model}))
    assert code == 2
    assert out == ""
    assert "rejected by the provider contract" in err
    # Requirement 20: nothing moved.
    after = _connection(store)
    assert after.default_model == "gpt-4"
    assert after.configuration_version == 1
    assert after.updated_at == _connection(store).updated_at


def test_failed_update_writes_no_row_and_creates_no_task(store):
    """A rejected update must not fall through to any other write."""
    _workspace_id(store)
    before = _connection(store)
    _update_output(*_update_args(**{"--workspace-id": "00000000-0000-0000-0000-000000000000"}))
    handle = SQLiteStore(ConnectionFactory(str(store)))
    with handle.reader() as repo:
        assert repo._conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert repo._conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0
        assert repo._conn.execute(
            "SELECT COUNT(*) FROM ai_invocations").fetchone()[0] == 0
    assert _connection(store) == before


# -- 21-25: the security contract -------------------------------------------

def test_parser_exposes_no_credential_option():
    update = _subparser(_subparser(cli._build_parser(), "provider"), "update")
    declared = set()
    for action in update._actions:
        declared.update(getattr(action, "option_strings", ()))
    assert declared == {"-h", "--help", "--workspace-id",
                        "--provider-connection-id", "--default-model"}
    for forbidden in ("--password", "--api-key", "--apikey", "--secret", "--token",
                      "--credential-reference", "--credential"):
        assert forbidden not in declared, forbidden


def test_implementation_never_dereferences_a_credential():
    strings, calls, names = _code_symbols()
    # No resolver is ever constructed or invoked, so no reference can be dereferenced.
    assert "EnvironmentSecretResolver" not in names
    assert "resolve" not in calls
    assert not any(re.search(r"(OPENAI|GROQ)_API_KEY", s) for s in strings)
    # credential_reference is legitimately a stored field for the 4F1 target command,
    # so its mere presence is not the assertion. What must not exist is a dereference
    # of it; that it is never *printed* is proven by the success-output test below.
    # No provider verification and no HTTP client of any kind.
    for absent in ("openai", "OpenAI", "requests", "httpx", "urllib", "verify",
                   "urlopen", "Client", "http"):
        assert absent not in names, absent


def test_implementation_reads_only_the_database_variable():
    read = set(re.findall(r"os\.environ(?:\.get\(|\[)['\"]([A-Z_]+)", CLI_SOURCE))
    assert read == {"AIWF_DATABASE"}
    for provider_secret in ("OPENAI_API_KEY", "GROQ_API_KEY", "SHOULD_NOT_BE_RESOLVED"):
        assert provider_secret not in CLI_SOURCE


def test_sentinel_credential_reference_is_preserved_and_never_needed(store, monkeypatch):
    """The command succeeds with the referenced variable absent, and preserves it."""
    monkeypatch.delenv("SHOULD_NOT_BE_RESOLVED", raising=False)
    # Repoint the stored reference through the ordinary repository authority, so the
    # test sets up configuration rather than reaching into the row itself.
    factory = ConnectionFactory(str(store))
    migrate(factory)
    handle = SQLiteStore(factory)
    with handle.workspace_reader(WORKSPACE_ID) as scoped:
        current = scoped.get_provider_connection(CONNECTION_ID)
    with handle.transaction() as internal:
        internal.update_provider_connection(
            replace(current, credential_reference=SENTINEL_REFERENCE))

    before = _connection(store)
    assert before.credential_reference == SENTINEL_REFERENCE
    code, out, err = _update_output(*_update_args())
    assert code == 0, err
    after = _connection(store)
    assert after.credential_reference == SENTINEL_REFERENCE
    assert after.default_model == "gpt-4.1"


def test_success_output_contains_no_secret_material(store):
    _workspace_id(store)
    code, out, err = _update_output(*_update_args())
    assert code == 0, err
    for leak in ("OPENAI_API_KEY", "env:", "SHOULD_NOT_BE_RESOLVED", "sk-"):
        assert leak not in out, leak
    # Non-secret operational data is present, so the success path is actually informative.
    assert "gpt-4.1" in out
    assert "configuration_version" in out
    # No traceback for an expected operator error.
    assert "Traceback" not in err


# -- TaskRun history boundary ----------------------------------------------

def _submit_one_run(store):
    """A real task, so task_runs carries a real provider snapshot."""
    from service.submission import TaskSubmissionService
    from service.workspace_bootstrap import default_workspace_context
    from domain.submission import SubmissionProfile

    handle = SQLiteStore(ConnectionFactory(str(store)))
    factory = ConnectionFactory(str(store))
    migrate(factory)
    context = default_workspace_context(handle)
    service = TaskSubmissionService(
        handle,
        lambda site, brand: SubmissionProfile(
            site_id=site, brand_profile_id=brand, client_profile_id=None, snapshot={}),
    )
    body = {"site_id": "site", "brand_profile_id": "brand", "content_type": "POST",
            "topic": "版本快照邊界主題", "brief": "這是一段足夠長度用於驗證快照邊界的完整需求說明文字。",
            "target_audience": "讀者"}
    return service.submit("f2c-snapshot", body).task


def _run_snapshots(store, task_id):
    """Read-only snapshot of the run history, workspace-filtered."""
    handle = SQLiteStore(ConnectionFactory(str(store)))
    with handle.reader() as repo:
        rows = repo._conn.execute(
            "SELECT r.run_id,r.model,r.provider_configuration_version FROM task_runs r "
            "JOIN tasks t ON t.task_id=r.task_id "
            "WHERE t.workspace_id=? AND r.task_id=? ORDER BY r.created_at",
            (WORKSPACE_ID, task_id)).fetchall()
    return [(r["run_id"], r["model"], r["provider_configuration_version"]) for r in rows]


def test_task_run_history_is_not_modified_by_a_provider_update(store):
    """The provider moves to model B / v2; the queued run keeps model A / v1."""
    task = _submit_one_run(store)
    before = _run_snapshots(store, task.task_id)
    assert before and before[0][1] == "gpt-4" and before[0][2] == 1

    code, out, err = _update_output(*_update_args())
    assert code == 0, err

    connection = _connection(store)
    assert (connection.default_model, connection.configuration_version) == ("gpt-4.1", 2)
    # The historical run is byte-identical. Nothing in the admin path writes task_runs.
    assert _run_snapshots(store, task.task_id) == before


def test_update_does_not_create_a_run_or_change_task_state(store):
    task = _submit_one_run(store)
    task_id = task.task_id
    handle = SQLiteStore(ConnectionFactory(str(store)))
    with handle.workspace_reader(WORKSPACE_ID) as scoped:
        before = scoped.get_task(task_id).status.value
    with handle.reader() as repo:
        runs_before = repo._conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0]
        events_before = repo._conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]

    assert _update_output(*_update_args())[0] == 0

    with handle.workspace_reader(WORKSPACE_ID) as scoped:
        assert scoped.get_task(task_id).status.value == before
    with handle.reader() as repo:
        assert repo._conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == runs_before
        assert repo._conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == events_before
        assert repo._conn.execute("SELECT COUNT(*) FROM ai_invocations").fetchone()[0] == 0
