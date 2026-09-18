"""Transactional forward-only migrations. Historical SQL must not be edited."""
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
    # Migrations cannot escape the runner-owned transaction or change PRAGMAs.
    forbidden = (sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT,
                 sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_PRAGMA)
    return sqlite3.SQLITE_DENY if action in forbidden else sqlite3.SQLITE_OK


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
    with factory.transaction() as conn:
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
