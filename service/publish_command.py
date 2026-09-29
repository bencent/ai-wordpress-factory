"""Builds the outbound publish command from durable authority. No external side effect.

The command is produced only for a publication this executor currently owns, so
content is never assembled before exclusive execution ownership is held. Authority
is the PublicationRequest's own content_version_id binding; approval history is not
re-resolved and the task's latest version pointer is never consulted.
"""
from domain.publication import (PublicationLease, PublishCommand, PublishCommandUnavailable,
                                build_publish_command)
from service.workspace_bootstrap import default_workspace_context


class PublishCommandService:
    """Workspace-scoped reader for the exact command a leased execution should send."""

    def __init__(self, store, *, context_provider=default_workspace_context):
        self.store = store
        self.context_provider = context_provider

    def build(self, lease):
        """Return the PublishCommand for a publication owned by this lease.

        Fails closed unless the lease still owns an IN_PROGRESS publication and the
        publication, task, and content version ownership line up exactly.
        """
        if not isinstance(lease, PublicationLease):
            raise PublishCommandUnavailable("a PublicationLease is required to build a command")
        context = self.context_provider(self.store)
        with self.store.workspace_transaction(context.workspace_id) as repo:
            if not repo.assert_publication_ownership(lease):
                raise PublishCommandUnavailable("Lease no longer owns this publication")
            publication = repo.get_publication(lease.publication_id)
            if publication is None or publication.task_id != lease.task_id:
                raise PublishCommandUnavailable("Publication is not visible in this workspace")
            version = repo.get_content_version(publication.content_version_id)
        if version is None:
            raise PublishCommandUnavailable("Approved content version is missing")
        return build_publish_command(publication, version)
