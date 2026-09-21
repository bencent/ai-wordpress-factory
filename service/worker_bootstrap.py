"""Production Worker bootstrap composition. Shared by CLI entry point."""
import json
import os
import signal
import threading
from pathlib import Path
from typing import Callable, Optional

from config import Config
from persistence.connection import ConnectionFactory
from persistence.repository import SQLiteStore
from persistence.migration_runner import migrate
from worker.adapter import FactoryAdapter
from worker.loop import Worker


class WorkerBootstrapError(RuntimeError):
    """Worker bootstrap configuration error."""
    pass


def _load_profiles(workspace_id: str):
    """Load profiles from AIWF_PROFILES_FILE, filter by workspace_id."""
    path = os.environ.get('AIWF_PROFILES_FILE')
    if not path:
        raise WorkerBootstrapError('AIWF_PROFILES_FILE environment variable not set')
    try:
        records = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as e:
        raise WorkerBootstrapError('Failed to read profiles file') from e
    if not isinstance(records, list):
        raise WorkerBootstrapError('Profiles file must contain a JSON array')
    return [r for r in records if r.get('workspace_id') == workspace_id]


def _make_config_resolver(store: SQLiteStore, workspace_id: str) -> Callable[[str], Config]:
    """Build config resolver matching HTTP API behavior."""
    profiles = _load_profiles(workspace_id)
    if not profiles:
        raise WorkerBootstrapError('No profiles found for workspace')

    def resolve(site_id: str) -> Config:
        for row in profiles:
            if row.get('site_id') == site_id and row.get('brand_profile_id'):
                return Config(agents=row.get('snapshot', {}).get('agents', {}))
        raise WorkerBootstrapError(f'No profile for site_id={site_id}')
    return resolve


def build_worker(
    database_path: Optional[str] = None,
    profiles_path: Optional[str] = None,
    poll_seconds: float = 1.0,
    heartbeat_seconds: float = 10.0,
    stale_seconds: float = 60.0,
    run_once: bool = False,
    image_root: Optional[str] = None,
    provider_factory=None,
    credential_resolver=None,
) -> Worker:
    """
    Construct a fully-wired Worker from production dependencies.

    Args:
        database_path: SQLite database file (default: AIWF_DATABASE env or data/aiwf.sqlite3)
        profiles_path: Profiles JSON file (default: AIWF_PROFILES_FILE env)
        poll_seconds: Poll interval between runs (default 1.0)
        heartbeat_seconds: Heartbeat interval (default 10.0)
        stale_seconds: Stale threshold (default 60.0)
        run_once: If True, caller should call run_once() instead of run()
        image_root: Local image storage root (default: artifacts/images)

    Returns:
        Worker instance ready to run.
    """
    if poll_seconds <= 0:
        raise WorkerBootstrapError('poll_seconds must be positive')
    if heartbeat_seconds <= 0:
        raise WorkerBootstrapError('heartbeat_seconds must be positive')
    if stale_seconds <= heartbeat_seconds:
        raise WorkerBootstrapError('stale_seconds must exceed heartbeat_seconds')

    db_path = database_path or os.environ.get('AIWF_DATABASE', 'data/aiwf.sqlite3')
    profiles_file = profiles_path or os.environ.get('AIWF_PROFILES_FILE')
    if not profiles_file:
        raise WorkerBootstrapError('AIWF_PROFILES_FILE environment variable not set')
    os.environ['AIWF_PROFILES_FILE'] = profiles_file

    factory = ConnectionFactory(db_path)
    migrate(factory)
    store = SQLiteStore(factory)

    with store.reader() as repo:
        workspace = repo.default_workspace()
    if workspace is None:
        raise WorkerBootstrapError('Default workspace not found')
    workspace_id = workspace.workspace_id

    config_resolver = _make_config_resolver(store, workspace_id)
    img_root = Path(image_root or 'artifacts/images').resolve()

    adapter_options = {}
    if provider_factory is not None:
        adapter_options['provider_factory'] = provider_factory
    if credential_resolver is not None:
        adapter_options['credential_resolver'] = credential_resolver

    adapter = FactoryAdapter(
        store=store,
        config_resolver=config_resolver,
        image_root=img_root,
        **adapter_options,
    )

    return Worker(
        store=store,
        executor=adapter,
        heartbeat_seconds=heartbeat_seconds,
        stale_seconds=stale_seconds,
    )


def run_worker(
    worker: Worker,
    poll_seconds: float = 1.0,
    run_once: bool = False,
) -> int:
    """
    Run the worker with signal handling.

    Returns:
        Exit code: 0 normal, 1 config error, 2 fatal runtime error.
    """
    stop = threading.Event()

    def _install_signal_handlers():
        try:
            signal.signal(signal.SIGINT, lambda *_: stop.set())
            signal.signal(signal.SIGTERM, lambda *_: stop.set())
        except (AttributeError, ValueError):
            pass

    _install_signal_handlers()

    if run_once:
        try:
            worker.run_once()
            return 0
        except WorkerBootstrapError:
            raise
        except Exception as e:
            raise WorkerBootstrapError(f'Worker execution failed: {e}') from e

    try:
        worker.run(stop, poll_seconds=poll_seconds)
        return 0
    except WorkerBootstrapError:
        raise
    except Exception as e:
        raise WorkerBootstrapError(f'Worker loop failed: {e}') from e
