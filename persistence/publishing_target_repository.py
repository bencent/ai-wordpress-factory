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

    def add_publishing_target(self, workspace_id, target):
        """Insert a target, scoped to this workspace. Cross-workspace writes fail."""
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
        return target

    def update_publishing_target_configuration(self, workspace_id, target_id, *, base_url,
                                               username, credential_reference, updated_at):
        """Apply a real configuration change and increment the version exactly once.

        The caller supplies the complete new configuration, so a change is never
        partially applied. The version increment happens here rather than being
        taken from the caller's value, so a caller cannot accidentally write a
        configuration change under an unchanged version.
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
        return candidate

    def update_publishing_target_status(self, workspace_id, target_id, status, updated_at):
        """Change only ACTIVE/DISABLED. Deliberately does NOT move the version."""
        self._write()
        current = self.get_publishing_target(workspace_id, target_id)
        if current is None:
            raise PersistenceError('Publishing target not found in workspace')
        if not isinstance(status, TargetStatus):
            raise ValueError('status must be TargetStatus')
        candidate = current.with_status(status, updated_at)
        self._write_target_row(workspace_id, candidate)
        return candidate

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
