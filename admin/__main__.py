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
    return 2


if __name__ == '__main__':
    sys.exit(main())
