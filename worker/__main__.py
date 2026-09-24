"""Worker runtime entry point: `python -m worker`."""
import argparse
import sys

# Test seams remain inert until main() has parsed CLI arguments. Production
# dependencies are imported lazily so module import and --help have no runtime
# configuration or credential side effects.
build_worker = None
run_worker = None
WorkerBootstrapError = None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog='python -m worker',
        description='AI WordPress Factory background worker',
    )
    parser.add_argument(
        '--once',
        action='store_true',
        help='Execute a single run_once() and exit (for smoke testing)',
    )
    parser.add_argument(
        '--poll-seconds',
        type=float,
        default=1.0,
        help='Poll interval between run attempts (default: 1.0)',
    )
    parser.add_argument(
        '--heartbeat-seconds',
        type=float,
        default=10.0,
        help='Heartbeat interval in seconds (default: 10.0)',
    )
    parser.add_argument(
        '--stale-seconds',
        type=float,
        default=60.0,
        help='Stale threshold in seconds (default: 60.0)',
    )
    parser.add_argument(
        '--database',
        type=str,
        default=None,
        help='SQLite database path (default: AIWF_DATABASE env or data/aiwf.sqlite3)',
    )
    parser.add_argument(
        '--profiles',
        type=str,
        default=None,
        help='Profiles JSON file (default: AIWF_PROFILES_FILE env)',
    )
    parser.add_argument(
        '--image-root',
        type=str,
        default=None,
        help='Local image storage root (default: artifacts/images)',
    )
    return parser.parse_args()


def main() -> int:
    global build_worker, run_worker, WorkerBootstrapError
    args = _parse_args()

    try:
        if build_worker is None or run_worker is None or WorkerBootstrapError is None:
            from service.worker_bootstrap import (
                WorkerBootstrapError as bootstrap_error,
                build_worker as production_build_worker,
                run_worker as production_run_worker,
            )
            if build_worker is None:
                build_worker = production_build_worker
            if run_worker is None:
                run_worker = production_run_worker
            if WorkerBootstrapError is None:
                WorkerBootstrapError = bootstrap_error

        worker = build_worker(
            database_path=args.database,
            profiles_path=args.profiles,
            poll_seconds=args.poll_seconds,
            heartbeat_seconds=args.heartbeat_seconds,
            stale_seconds=args.stale_seconds,
            run_once=args.once,
            image_root=args.image_root,
        )
    except Exception:
        print('Worker configuration failed.', file=sys.stderr)
        return 1

    try:
        return run_worker(worker, poll_seconds=args.poll_seconds, run_once=args.once)
    except Exception:
        print('Worker execution failed.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
