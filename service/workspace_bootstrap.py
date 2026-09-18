"""Single-workspace compatibility composition. Never used to authorize request data."""
from dataclasses import replace
from domain.workspace import WorkspaceContext
from domain.providers import WorkspaceStatus
from domain.submission import SubmissionProfile, ProfileError


def default_workspace_context(store):
    # One explicit backend bootstrap read; application services subsequently use scoped views only.
    with store.reader() as internal:
        workspace = internal.default_workspace()
    if workspace is None or workspace.status != WorkspaceStatus.ACTIVE:
        raise ProfileError()
    return WorkspaceContext(workspace.workspace_id)


def default_profile_resolver(store, context, legacy_resolver):
    def resolve(actual_context, site_id, brand_profile_id):
        if actual_context != context:
            raise ProfileError()
        profile = legacy_resolver(site_id, brand_profile_id)
        if not isinstance(profile,SubmissionProfile):
            raise ProfileError()
        # Only legacy omissions are filled; explicit mismatched ownership is never overwritten.
        workspace_id = profile.workspace_id if profile.workspace_id is not None else context.workspace_id
        connection_id = profile.provider_connection_id
        if connection_id is None:
            with store.workspace_reader(context.workspace_id) as repo:
                providers = repo.text_connections()
            if len(providers) != 1:
                raise ProfileError()
            connection_id = providers[0].provider_connection_id
        return replace(profile,workspace_id=workspace_id,provider_connection_id=connection_id)
    return resolve
