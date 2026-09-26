"""Trusted application context, supplied by backend composition, never request data."""
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WorkspaceContext:
    workspace_id: str

    def __post_init__(self):
        if type(self.workspace_id) is not str or not self.workspace_id.strip():
            raise ValueError('Invalid workspace context')
