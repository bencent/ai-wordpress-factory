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
from datetime import datetime, timezone
from pathlib import Path

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
    # It does not invent rotation semantics: no write beyond the additive one.
    for forbidden in ("update_publishing_target_configuration",
                      "update_publishing_target_status", "with_configuration"):
        assert forbidden not in CLI_SOURCE, forbidden


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
