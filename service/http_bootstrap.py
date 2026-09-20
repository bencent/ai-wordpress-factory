"""Trusted local composition, outside the HTTP controller. No credential resolution."""
import json,os
from pathlib import Path
from domain.submission import SubmissionProfile,ProfileError
from persistence.connection import ConnectionFactory
from persistence.repository import SQLiteStore
from service.task_http import TaskHTTPService

def build_http_service():
    store=SQLiteStore(ConnectionFactory(os.environ.get('AIWF_DATABASE','data/aiwf.sqlite3')))
    # A deployment-owned file is an allowlist, not client-supplied identity data.
    def resolve(context,site_id,brand_id):
        path=os.environ.get('AIWF_PROFILES_FILE')
        if not path: raise ProfileError()
        records=json.loads(Path(path).read_text(encoding='utf-8'))
        for row in records:
            if (row.get('workspace_id')==context.workspace_id and row.get('site_id')==site_id
                    and row.get('brand_profile_id')==brand_id):
                return SubmissionProfile(workspace_id=row['workspace_id'],site_id=site_id,
                    brand_profile_id=brand_id,client_profile_id=row.get('client_profile_id'),
                    provider_connection_id=row['provider_connection_id'],snapshot=row.get('snapshot',{}))
        raise ProfileError()
    return TaskHTTPService(store,resolve)
