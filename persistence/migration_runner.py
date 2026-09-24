"""Transactional forward-only migrations. Historical SQL must not be edited."""
from contextlib import contextmanager
import hashlib
import re
import sqlite3
from pathlib import Path
from .connection import PersistenceError


def statements(script):
    # complete_statement understands quoted semicolons and trigger bodies.
    pending = ""
    for char in script:
        pending += char
        if char == ";" and sqlite3.complete_statement(pending):
            yield pending
            pending = ""
    if pending.strip():
        raise PersistenceError("Migration must end with a complete statement")


def _authorize(action, arg1, arg2, database, source):
    # SQLite ALTER TABLE may invoke read-only quick_check(table) internally.
    # Its argument selects a table/check limit; it does not change a setting.
    if action == sqlite3.SQLITE_PRAGMA and (arg1 or '').strip().casefold() == 'quick_check':
        return sqlite3.SQLITE_OK
    # Every other PRAGMA and transaction-control action remains forbidden.
    forbidden = (sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT,
                 sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_PRAGMA)
    return sqlite3.SQLITE_DENY if action in forbidden else sqlite3.SQLITE_OK


@contextmanager
def migration_transaction(factory):
    # Rebuilding a referenced table requires FK enforcement off BEFORE BEGIN.
    # Only this dedicated migration connection changes it; application connections stay ON.
    with factory.connection() as conn:
        conn.execute('PRAGMA foreign_keys=OFF')
        try:
            conn.execute('BEGIN IMMEDIATE')
            try:
                yield conn
                if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
                    raise PersistenceError('Migration foreign key validation failed')
                conn.execute('COMMIT')
            except BaseException:
                if conn.in_transaction:
                    conn.execute('ROLLBACK')
                raise
        finally:
            conn.execute('PRAGMA foreign_keys=ON')
            if conn.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
                raise PersistenceError('Migration foreign keys could not be restored')


def migrate(factory, directory=None):
    directory = Path(directory) if directory else Path(__file__).with_name("migrations")
    migrations = []
    for path in sorted(directory.glob("*.sql")):
        if not re.fullmatch(r"[0-9]{4}_.+\.sql", path.name):
            raise PersistenceError("Invalid migration name")
        sql = path.read_text(encoding="utf-8")
        migrations.append((int(path.name[:4]), hashlib.sha256(sql.encode()).hexdigest(), sql))
    versions = [v for v, _, _ in migrations]
    if not versions or len(versions) != len(set(versions)):
        raise PersistenceError("Missing or duplicate migrations")
    factory.initialize()
    with migration_transaction(factory) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                     "(version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)")
        applied = dict(conn.execute("SELECT version, checksum FROM schema_migrations"))
        expected = {v: digest for v, digest, _ in migrations}
        if any(expected.get(v) != digest for v, digest in applied.items()):
            raise PersistenceError("Unknown or modified migration")
        if set(applied) != set(versions[:len(applied)]):
            raise PersistenceError("Migration history is not a prefix")
        for version, digest, sql in migrations:
            if version in applied:
                continue
            conn.set_authorizer(_authorize)
            try:
                for statement in statements(sql):
                    conn.execute(statement)
            finally:
                conn.set_authorizer(None)
            conn.execute("INSERT INTO schema_migrations VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                         (version, digest))
