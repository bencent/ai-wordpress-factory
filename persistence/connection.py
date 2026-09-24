"""Explicit SQL transactions, each owning one connection for its entire lifetime."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class PersistenceError(RuntimeError):
    pass


class DatabaseBusy(PersistenceError):
    pass


class ConstraintViolation(PersistenceError):
    pass


def database_error(exc):
    if isinstance(exc, sqlite3.IntegrityError):
        return ConstraintViolation("Persistence constraint rejected the operation")
    code = getattr(exc, "sqlite_errorcode", 0) or 0
    if code & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return DatabaseBusy("Database lock wait expired")
    return PersistenceError("Database operation failed")


class ConnectionFactory:
    def __init__(self, path, *, busy_timeout_ms=5000):
        if type(busy_timeout_ms) is not int or busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be a nonnegative integer")
        if str(path) == ":memory:":
            raise ValueError("Use a file database for independent connections")
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms

    @contextmanager
    def connection(self):
        conn = None
        try:
            conn = sqlite3.connect(self.path, autocommit=True,
                                   timeout=self.busy_timeout_ms / 1000)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise PersistenceError("Foreign keys could not be enabled")
            yield conn
        except sqlite3.Error as exc:
            raise database_error(exc) from None
        finally:
            if conn is not None:
                conn.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            if conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() != "wal":
                raise PersistenceError("WAL could not be enabled")

    @contextmanager
    def transaction(self):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
