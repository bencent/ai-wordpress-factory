"""Workspace-scoped publishing target reads and versioned configuration writes.

Every read here is workspace-scoped and joins ``workspaces`` for ACTIVE status,
so a foreign workspace's target is indistinguishable from a missing one. That is
the same convention :mod:`persistence.scoped_repository` already uses for tasks,
runs, and provider connections, and it is the only behaviour that keeps one
tenant from learning that another tenant's target exists.

Writes are versioned deliberately. Changing ``base_url``, ``username`` or
``credential_reference`` must move ``configuration_version``, because a
publication that snapshotted version N must be able to detect that its target no
longer looks the way it did when the request was made. A status-only transition
does not move the version, so a publication snapshotted while its target was
ACTIVE is not invalidated by that target later being DISABLED.

No method performs external I/O and none of them touch secret material: a
credential reference is a name, and only a SecretResolver turns it into a value.
"""
from domain.publishing_target import (
    PublishingProviderType,
    PublishingTarget,
    PublishingTargetVersion,
    TargetStatus,
)
from persistence.connection import PersistenceError

# Configuration fields that make a target a different destination or credential.
# A change to any of these must increment configuration_version.
_CONFIGURATION_FIELDS = ('base_url', 'username', 'credential_reference')


class PublishingTargetRepositoryMixin:
    def get_publishing_target(self, workspace_id, target_id):
        """Read one target in this workspace. A foreign workspace reads nothing.

        A DISABLED target is still returned. That is required, not incidental: a
        publication that snapshotted this target must still be able to resolve
        the exact destination it was bound to, even after another target became
        the active one. Resolvability is what lets drift be detected instead of
        the publication being silently redirected.
        """
        if type(target_id) is not str or not target_id.strip():
            raise ValueError('target_id is required')
        row = self._conn.execute(
            "SELECT t.* FROM publishing_targets t "
            "JOIN workspaces w ON w.workspace_id=t.workspace_id "
            "WHERE t.workspace_id=? AND t.target_id=? AND w.status='ACTIVE'",
            (workspace_id, target_id)).fetchone()
        return self._decode_target(row)

    def active_publishing_target(self, workspace_id, provider_type=PublishingProviderType.WORDPRESS):
        """Return this workspace's single ACTIVE target of a provider type, or None.

        This is the REQUEST-TIME resolution used when recording publish intent.
        It is not an execution-time lookup: once a PublicationRequest exists, the
        snapshotted target_id is authoritative and this must not be consulted
        again, or a publication could be sent to whichever target happens to be
        active at the moment an executor happens to run.
        """
        if not isinstance(provider_type, PublishingProviderType):
            raise ValueError('provider_type must be PublishingProviderType')
        row = self._conn.execute(
            "SELECT t.* FROM publishing_targets t "
            "JOIN workspaces w ON w.workspace_id=t.workspace_id "
            "WHERE t.workspace_id=? AND t.provider_type=? AND t.status='ACTIVE' "
            "AND w.status='ACTIVE'",
            (workspace_id, provider_type.value)).fetchone()
        return self._decode_target(row)

    def publishing_targets(self, workspace_id):
        """All targets in this workspace, ordered for deterministic selection."""
        rows = self._conn.execute(
            "SELECT t.* FROM publishing_targets t "
            "JOIN workspaces w ON w.workspace_id=t.workspace_id "
            "WHERE t.workspace_id=? AND w.status='ACTIVE' ORDER BY t.target_id",
            (workspace_id,))
        return [self._decode_target(row) for row in rows]

    def _active_workspace(self, workspace_id):
        row = self._conn.execute(
            "SELECT workspace_id FROM workspaces WHERE workspace_id=? AND status='ACTIVE'",
            (workspace_id,)).fetchone()
        return row

    def _decode_target(self, row):
        if row is None:
            return None
        data = dict(row)
        return PublishingTarget(
            target_id=data['target_id'],
            workspace_id=data['workspace_id'],
            provider_type=PublishingProviderType(data['provider_type']),
            status=TargetStatus(data['status']),
            base_url=data['base_url'],
            username=data['username'],
            credential_reference=data['credential_reference'],
            configuration_version=data['configuration_version'],
            created_at=data['created_at'],
            updated_at=data['updated_at'],
        )

    def _decode_target_version(self, row):
        if row is None:
            return None
        data = dict(row)
        return PublishingTargetVersion(
            target_id=data['target_id'],
            workspace_id=data['workspace_id'],
            provider_type=PublishingProviderType(data['provider_type']),
            base_url=data['base_url'],
            username=data['username'],
            credential_reference=data['credential_reference'],
            configuration_version=data['configuration_version'],
            created_at=data['created_at'],
        )

    def add_publishing_target(self, workspace_id, target):
        """Insert a target AND its version-1 historical record, atomically.

        Both rows are written here, in the caller's transaction, so a target can
        never exist without a resolvable configuration identity. If the history
        insert fails, the whole transaction rolls back and neither row survives;
        there is deliberately no ordering in which the current row commits alone,
        because a target with no version is a destination that can never be
        resolved again once its configuration is overwritten.
        """
        self._write()
        if type(target) is not PublishingTarget:
            raise PersistenceError('Invalid publishing target contract')
        # Explicit workspace check rather than trusting the record: the internal
        # add() is unscoped, and provider connections are already written through
        # it. This path must not repeat that weakness.
        if target.workspace_id != workspace_id:
            raise PersistenceError('Scoped write rejected')
        if self._active_workspace(workspace_id) is None:
            raise PersistenceError('Scoped write rejected')
        self.add(target)
        self._insert_target_version(workspace_id, target)
        return target

    def get_publishing_target_version(self, workspace_id, target_id, configuration_version):
        """Read one EXACT historical configuration, or None.

        This is the read a future reconciler needs: given only the two values a
        PublicationRequest carries, return the destination those values name.

        It never falls back. Not to the current target, not to the active target,
        not to the nearest or latest version, and not to a version-number
        comparison. Every one of those fallbacks is a way to search the wrong
        WordPress and report a confident, false answer, which is the specific
        harm this table exists to prevent. A version with no record returns None
        and the caller must treat that as "unknowable", not as "unchanged".

        Legacy publications may reference a version that was overwritten before
        this table existed. None is the truthful answer for them and is the reason
        this method must not guess.
        """
        if type(target_id) is not str or not target_id.strip():
            raise ValueError('target_id is required')
        if type(configuration_version) is not int or configuration_version < 1:
            raise ValueError('configuration_version must be a positive int')
        row = self._conn.execute(
            "SELECT v.* FROM publishing_target_versions v "
            "JOIN workspaces w ON w.workspace_id=v.workspace_id "
            "WHERE v.workspace_id=? AND v.target_id=? AND v.configuration_version=? "
            "AND w.status='ACTIVE'",
            (workspace_id, target_id, configuration_version)).fetchone()
        if row is None:
            return None
        data = dict(row)
        return PublishingTargetVersion(
            target_id=data['target_id'],
            workspace_id=data['workspace_id'],
            provider_type=PublishingProviderType(data['provider_type']),
            base_url=data['base_url'],
            username=data['username'],
            credential_reference=data['credential_reference'],
            configuration_version=data['configuration_version'],
            created_at=data['created_at'],
        )

    def get_publishing_target_version_evidence(self, workspace_id, target_id,
                                               configuration_version):
        """Does an exact historical configuration record EXIST? ACTIVE-agnostic.

        The narrow evidence read, added by 8.3-3C6C for one reason.

        ``get_publishing_target_version`` joins ``workspaces`` for ACTIVE status,
        which is the right convention for reading a destination you are about to
        USE. But it silently conflates two unrelated questions for a reconciler:

            "does this historical configuration exist?"
            "is this workspace currently allowed to do network work?"

        With the ACTIVE join those collapse, and an archived workspace makes
        missing evidence indistinguishable from an absent workspace. A
        reconciler would then report RECONCILIATION_TARGET_UNAVAILABLE -- a claim
        about the destination -- when the truth is that the workspace is closed.
        That is a false statement about a remote site, produced by a local
        policy, which is the exact confusion this method removes.

        So existence and permission are separated: this answers existence only,
        and the caller decides separately whether it may act on the answer.

        Workspace scoping is NOT weakened. The ``workspace_id`` predicate is
        still mandatory and still joins nothing that would let one tenant read
        another's history; only the ACTIVE-status filter is absent, and that
        filter was never a tenancy boundary. A target row is scoped to exactly one
        workspace by its foreign key, so there is nothing else to cross.
        """
        if type(target_id) is not str or not target_id.strip():
            raise ValueError('target_id is required')
        if type(configuration_version) is not int or configuration_version < 1:
            raise ValueError('configuration_version must be a positive int')
        row = self._conn.execute(
            "SELECT * FROM publishing_target_versions "
            "WHERE workspace_id=? AND target_id=? AND configuration_version=?",
            (workspace_id, target_id, configuration_version)).fetchone()
        return self._decode_target_version(row)

    def publishing_target_versions(self, workspace_id, target_id):
        if type(target_id) is not str or not target_id.strip():
            raise ValueError('target_id is required')
        rows = self._conn.execute(
            "SELECT v.* FROM publishing_target_versions v "
            "JOIN workspaces w ON w.workspace_id=v.workspace_id "
            "WHERE v.workspace_id=? AND v.target_id=? AND w.status='ACTIVE' "
            "ORDER BY v.configuration_version",
            (workspace_id, target_id)).fetchall()
        return [self._decode_target_version(row) for row in rows]

    def update_publishing_target_configuration(self, workspace_id, target_id, *, base_url,
                                               username, credential_reference, updated_at):
        """Apply a real configuration change and increment the version exactly once.

        The caller supplies the complete new configuration, so a change is never
        partially applied. The version increment happens here rather than being
        taken from the caller's value, so a caller cannot accidentally write a
        configuration change under an unchanged version.

        The previous configuration is preserved and the new one is recorded in the
        SAME transaction as the current-row update. All three steps -- preserve,
        insert, update -- either land together or not at all. The current row is
        updated first so that the version guard, the single-active index and the
        identity trigger all reject a bad change before any history is written;
        the history insert then fails closed on the composite primary key if a
        concurrent writer already claimed that version, which rolls the current
        row back with it. Either way there is no externally visible partial state.
        """
        self._write()
        current = self.get_publishing_target(workspace_id, target_id)
        if current is None:
            raise PersistenceError('Publishing target not found in workspace')
        candidate = current.with_configuration(
            base_url=base_url, username=username,
            credential_reference=credential_reference,
            status=current.status, updated_at=updated_at)
        self._write_target_row(workspace_id, candidate)
        self._insert_target_version(workspace_id, candidate)
        return candidate

    def update_publishing_target_status(self, workspace_id, target_id, status, updated_at):
        """Change only ACTIVE/DISABLED. Deliberately does NOT move the version.

        A status flip is not a configuration change: base_url, username and
        credential_reference are untouched, so no new historical record is
        written. Recording one would be a lie -- the destination did not change --
        and it would also make a disabled target look like a different place,
        which is exactly what a future reconciler must not conclude.
        """
        self._write()
        current = self.get_publishing_target(workspace_id, target_id)
        if current is None:
            raise PersistenceError('Publishing target not found in workspace')
        if not isinstance(status, TargetStatus):
            raise ValueError('status must be TargetStatus')
        candidate = current.with_status(status, updated_at)
        self._write_target_row(workspace_id, candidate)
        return candidate

    def _insert_target_version(self, workspace_id, target):
        """Record one immutable configuration version.

        Deliberately not routed through the generic ``add()``: this table's
        integrity depends on every insert arriving from the two paths above, in
        the same transaction as the current-row write. A generic record insert
        would let a caller add a version with no corresponding target, or with a
        version that does not match, and the atomicity guarantee would become a
        convention rather than a property of the code.

        The INSERT is intentionally not INSERT OR IGNORE / OR REPLACE. A
        conflicting version must abort the transaction so the current-row update
        rolls back with it; silently ignoring would leave the current row at a
        version whose history describes a different configuration.
        """
        if type(target) is not PublishingTarget:
            raise PersistenceError('Invalid publishing target contract')
        self._write()
        self._conn.execute(
            "INSERT INTO publishing_target_versions "
            "(workspace_id, target_id, configuration_version, provider_type, base_url, "
            "username, credential_reference, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (target.workspace_id, target.target_id, target.configuration_version,
             target.provider_type.value, target.base_url, target.username,
             target.credential_reference, target.created_at))

    def _write_target_row(self, workspace_id, target):
        """Persist a versioned target update, letting the database re-check it.

        Identity columns are excluded so an update can never re-point a target at
        another workspace or a different provider type. The database's own
        immutability trigger, version guard, single-active index, and no-delete
        trigger all remain the final authority; this method does not try to
        reimplement them, it just issues the deliberate UPDATE.
        """
        self._conn.execute(
            "UPDATE publishing_targets SET provider_type=?,status=?,base_url=?,username=?,"
            "credential_reference=?,configuration_version=?,updated_at=? "
            "WHERE target_id=? AND workspace_id=?",
            (target.provider_type.value, target.status.value, target.base_url,
             target.username, target.credential_reference,
             target.configuration_version, target.updated_at,
             target.target_id, workspace_id))
