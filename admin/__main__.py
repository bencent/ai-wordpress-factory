"""Operator provisioning CLI: ``python -m admin``.

Why this exists
---------------
4F0 found that the publication path was complete but inert: nothing in the
application could create a ``PublishingTarget``, so the only way to configure a
publishing destination was to write a throwaway script or edit the database by
hand. This module is the supported way to do it.

What it is, and what it deliberately is not
-------------------------------------------
* It is a **provisioning** command. It records configuration; it does not
  validate that the credential works. Establishing that a site is reachable and
  that the credential authenticates is the publication worker's job, and it
  happens on the first real publication, not here.
* It stores a credential **reference** only -- the same ``env:NAME`` pointer the
  ``ai_provider_connections`` table already uses. The secret itself is never
  accepted as an argument, never read from the environment here, and never
  written. There is deliberately no ``--password`` or ``--application-password``
  flag: accepting one would put a value in shell history, in the process table,
  and in anything that echoes the invocation.
* It invents no new authority path. Validation is the domain object's
  (``PublishingTarget.__post_init__``), the workspace and duplicate checks are
  the scoped repository's existing reads, and the write is
  ``add_publishing_target`` -- the same call the persistence tests use, which
  writes the target and its version-1 history atomically. No SQL is issued here.

Conventions
-----------
This mirrors ``worker/__main__.py``: argparse, production imports deferred until
after parsing so ``--help`` has no database or credential side effects, an
integer exit code, and ``sys.exit(main())``.
"""
import argparse
import os
import sys


def _build_parser():
    """Build the argument parser.

    Separated from :func:`_parse_args` so a test can inspect the declared
    argument set directly. That inspection is the point: "this CLI has no
    ``--password`` flag" is a claim about the parser, and it should be provable
    from the parser rather than from a string search over the source.
    """
    parser = argparse.ArgumentParser(
        prog='python -m admin',
        description='AI WordPress Factory operator provisioning',
    )
    commands = parser.add_subparsers(dest='command', required=True)

    target = commands.add_parser(
        'publishing-target',
        help='Inspect and provision WordPress publishing destinations',
    )
    target_commands = target.add_subparsers(dest='target_command', required=True)

    add = target_commands.add_parser(
        'add',
        help='Provision an ACTIVE WordPress publishing target for an existing workspace',
        description=(
            'Records an ACTIVE WORDPRESS publishing target and its version-1 history. '
            'The credential is named by --credential-reference (an env:NAME pointer); '
            'the value it points at is never read, printed, or stored here.'
        ),
    )
    add.add_argument('--workspace-id', required=True,
                     help='Existing ACTIVE workspace that owns the target')
    add.add_argument('--base-url', required=True,
                     help='WordPress base URL, e.g. https://example.test (no credentials, '
                          'no query string, no trailing slash)')
    add.add_argument('--username', required=True,
                     help='WordPress username the Application Password belongs to')
    add.add_argument('--credential-reference', required=True,
                     metavar='env:NAME',
                     help='Credential reference to store, e.g. env:AIWF_WP_APP_PASSWORD. '
                          'This is the pointer only; the secret is never passed here')

    provider = commands.add_parser(
        'provider',
        help='Operate on the workspace\'s AI provider connections',
    )
    provider_commands = provider.add_subparsers(dest='provider_command', required=True)

    update = provider_commands.add_parser(
        'update',
        help='Set the default TEXT model on an existing provider connection',
        description=(
            'Replaces default_model and increments configuration_version by one. '
            'Every other field is carried over unchanged, including the credential '
            'reference, which is preserved as configuration and never dereferenced. '
            'This command makes no provider call, so it cannot confirm that a model '
            'exists on the account; it records the operator\'s choice.'
        ),
    )
    update.add_argument('--workspace-id', required=True,
                        help='Workspace that must own the provider connection')
    update.add_argument('--provider-connection-id', required=True,
                        help='Provider connection to update')
    update.add_argument('--default-model', required=True, metavar='MODEL',
                        help='New default TEXT model, e.g. gpt-4.1. Validated by the '
                             'AIProviderConnection contract, not against a provider')
    return parser


def _parse_args(argv=None):
    return _build_parser().parse_args(argv)


def _open_store():
    """Open the same database the application uses, via the existing bootstrap shape.

    ``AIWF_DATABASE`` with the ``data/aiwf.sqlite3`` default is the single
    configuration mechanism in this project; provisioning does not add a second
    one. Migrations run here exactly as ``build_publication_worker`` runs them,
    so a fresh database can be provisioned without a separate step.

    Production imports stay inside the function so that importing this module --
    and running ``--help`` -- opens no database and reads no secret.
    """
    from persistence.connection import ConnectionFactory
    from persistence.migration_runner import migrate
    from persistence.repository import SQLiteStore

    factory = ConnectionFactory(os.environ.get('AIWF_DATABASE', 'data/aiwf.sqlite3'))
    migrate(factory)
    return SQLiteStore(factory)


def _build_target(args, now):
    """Construct the target through the domain contract, and nothing else.

    Every field is validated by ``PublishingTarget.__post_init__`` -- the base
    URL grammar, the username, and the credential-reference pattern -- so this
    function adds no validation of its own and cannot drift from the domain.
    """
    from domain.publishing_target import PublishingTarget, PublishingProviderType, TargetStatus
    from uuid import uuid4

    return PublishingTarget(
        target_id=str(uuid4()),
        workspace_id=args.workspace_id,
        provider_type=PublishingProviderType.WORDPRESS,
        status=TargetStatus.ACTIVE,
        base_url=args.base_url,
        username=args.username,
        credential_reference=args.credential_reference,
        # Version 1 is the only version a new target can have: the history row
        # written alongside it is the record of this configuration.
        configuration_version=1,
        created_at=now,
        updated_at=now,
    )


def _add_publishing_target(args) -> int:
    from datetime import datetime, timezone
    from domain.credential_reference import is_credential_reference

    # Checked before anything is written, and reported precisely, because the
    # value is the operator's typing and the domain error deliberately does not
    # echo it. Everything else is validated by the domain object below.
    if not is_credential_reference(args.credential_reference):
        print('Credential reference is invalid: expected env:NAME, for example '
              'env:AIWF_WP_APP_PASSWORD.', file=sys.stderr)
        return 2

    try:
        target = _build_target(args, datetime.now(timezone.utc).isoformat())
    except ValueError:
        # base_url, username and the credential reference are all validated by
        # PublishingTarget.__post_init__. The reason is not printed: the domain
        # messages are safe, but one invalid-argument report for all three keeps
        # this handler from becoming a parser for what an operator typed.
        print('Publishing target configuration rejected. --base-url must be an http(s) '
              'URL with a host and no embedded credentials, query or fragment; '
              '--username must be non-empty.', file=sys.stderr)
        return 2

    store = _open_store()
    with store.workspace_transaction(args.workspace_id) as repo:
        # repo.workspace() resolves only an ACTIVE workspace, so a missing and an
        # archived workspace fail together here. They are deliberately reported
        # as one condition rather than separated by a private SQL read.
        if repo.workspace() is None:
            print('Workspace not found or not ACTIVE. No target was written.',
                  file=sys.stderr)
            return 2
        # A pre-flight for a clear message. The single-ACTIVE-per-workspace
        # partial unique index is still the authority: this read races harmlessly
        # because a concurrent winner makes the write below fail closed.
        if repo.active_publishing_target() is not None:
            print('An ACTIVE WordPress publishing target already exists for this '
                  'workspace. This command does not replace it. Use the target '
                  'configuration update path to rotate a destination.',
                  file=sys.stderr)
            return 2
        # The repository writes the target and its version-1 history atomically,
        # and re-checks workspace ownership and ACTIVE status itself.
        repo.add_publishing_target(target)

    print('Provisioned ACTIVE WordPress publishing target {target_id} for workspace '
          '{workspace_id}.'.format(target_id=target.target_id,
                                   workspace_id=target.workspace_id))
    print('Credential reference recorded: {ref}'.format(ref=target.credential_reference))
    print('Export the referenced variable in the API and worker environment. The value '
          'is never read, printed, or stored by this command.')
    return 0


def _update_provider(args) -> int:
    """Set an existing provider connection's default TEXT model.

    Scope, deliberately narrow: exactly three values move --
    ``default_model``, ``configuration_version``, and ``updated_at``. Every other
    field is carried over from the record that was just read, so this command
    cannot re-point a workspace, re-type a provider, or touch a credential.

    Three conventions are reused rather than reinvented:

    * **The read is the scoped repository's.** ``get_provider_connection`` filters on
      workspace AND on an ACTIVE workspace, so cross-workspace and archived-workspace
      access fail here, in a read-only transaction, before anything can be written.
      It is the only provider lookup authority used.
    * **The write is the internal repository's existing method.**
      ``update_provider_connection`` -> ``_replace_configuration`` already exists and
      is the same authority the persistence tests use. No SQL, no second write path.
    * **The version moves because the project says configuration changes are
      versioned.** ``PublishingTarget.with_configuration`` increments
      ``configuration_version`` unconditionally and never compares against the old
      value, so this does the same -- including when the requested model is
      unchanged. The caller cannot supply a version.

    No credential is dereferenced. ``credential_reference`` is carried across as
    configuration and is never printed, and no resolver is ever constructed, so this
    command succeeds whether or not the referenced variable exists.
    """
    from dataclasses import replace
    from datetime import datetime, timezone
    from domain.providers import ContractError

    store = _open_store()
    workspace_id = args.workspace_id
    connection_id = args.provider_connection_id

    # Transaction 1: read-only. Cannot mutate, so a failure here leaves no trace.
    with store.workspace_transaction(workspace_id) as scoped:
        current = scoped.get_provider_connection(connection_id)
    if current is None:
        # One message for "no such provider in this workspace" and "workspace is not
        # ACTIVE" is deliberate: both are the scoped read returning nothing, and
        # separating them would need a private SQL read this command must not do.
        print('Provider connection not found in that workspace, or the workspace is '
              'not ACTIVE. Nothing was changed.', file=sys.stderr)
        return 2

    # Validated by AIProviderConnection.__post_init__ via replace(). A rejected model
    # fails before any write transaction is opened.
    try:
        candidate = replace(
            current,
            default_model=args.default_model,
            configuration_version=current.configuration_version + 1,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
    except ContractError:
        print('Model rejected by the provider contract. A model name must match the '
              'AIProviderConnection grammar. Nothing was changed.', file=sys.stderr)
        return 2

    # Transaction 2: the existing repository update authority, nothing else.
    with store.transaction() as internal:
        internal.update_provider_connection(candidate)

    print('Updated provider connection {id} in workspace {ws}.'.format(
        id=candidate.provider_connection_id, ws=workspace_id))
    print('default_model         : {old} -> {new}'.format(
        old=current.default_model, new=candidate.default_model))
    print('configuration_version : {old} -> {new}'.format(
        old=current.configuration_version, new=candidate.configuration_version))
    print('No credential was read, resolved, or printed, and no provider call was made. '
          'Runs already queued keep the model they snapshotted.')
    return 0


def main(argv=None) -> int:
    args = _parse_args(argv)
    if args.command == 'publishing-target' and args.target_command == 'add':
        try:
            return _add_publishing_target(args)
        except Exception as error:
            # Type only. A persistence failure can carry a constraint name or a
            # row value, and this frame is the outermost one.
            print('Publishing target provisioning failed ({type}). No target was '
                  'written.'.format(type=type(error).__name__), file=sys.stderr)
            return 2
    if args.command == 'provider' and args.provider_command == 'update':
        try:
            return _update_provider(args)
        except Exception as error:
            print('Provider update failed ({type}). The connection was not '
                  'changed.'.format(type=type(error).__name__), file=sys.stderr)
            return 2
    return 2


if __name__ == '__main__':
    sys.exit(main())
