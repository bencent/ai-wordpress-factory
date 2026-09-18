import json
import sqlite3
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor

import pytest

from domain.contracts import Task, TaskRun, TaskEvent, ContentVersion, ContentType, Status
from persistence.codec import CodecError, encode_record, decode_record, decode_snapshot
from persistence.connection import ConnectionFactory, PersistenceError, ConstraintViolation, DatabaseBusy
from persistence.migration_runner import migrate
from persistence.repository import SQLiteStore


@pytest.fixture
def store(tmp_path):
    factory = ConnectionFactory(tmp_path / 'app.sqlite3', busy_timeout_ms=50)
    migrate(factory)
    return SQLiteStore(factory)


def records(suffix='a', content_type=ContentType.POST):
    task = Task(task_id=suffix, submission_key='key-'+suffix, site_id='site-'+suffix,
                content_type=content_type, topic='繁體中文主題', brief='內容需求',
                brand_profile_id='brand', created_at='2026-09-17T00:00:00Z',
                updated_at='2026-09-17T00:00:00Z',
                request_snapshot={'nested': [None, {'type': content_type, 'text': '中文'}]},
                approval_policy_snapshot={'mode': 'REQUIRE_HUMAN_REVIEW'})
    run = TaskRun(run_id='run-'+suffix, task_id=suffix, attempt=1,
                  created_at=task.created_at, updated_at=task.updated_at,
                  workflow_state={'draft': None, 'image': {'status': 'READY'}, 'items': [1, True]})
    event = TaskEvent(event_id='event-'+suffix, event_key='event-key-'+suffix,
                      task_id=suffix, run_id=run.run_id, attempt=1, sequence_number=1,
                      type='created', actor='system', summary='已建立', created_at=task.created_at)
    version = ContentVersion(content_version_id='version-'+suffix, task_id=suffix,
                             run_id=run.run_id, version_number=1, content_type=content_type,
                             title='完整標題', content='<p>繁體中文</p>', validation_result={'passed': True},
                             created_at=task.created_at, updated_at=task.updated_at)
    return task, run, event, version


@pytest.mark.parametrize('content_type', list(ContentType))
def test_round_trip_and_reopen(store, content_type):
    items = records(content_type=content_type)
    with store.transaction() as repo:
        for item in items:
            repo.add(item)
    reopened = SQLiteStore(ConnectionFactory(store.factory.path))
    with reopened.reader() as repo:
        for item in items:
            identifier = next(iter(vars(item).values()))
            assert repo.get(type(item), identifier) == item
            assert decode_record(type(item), encode_record(item)) == item
        assert repo.events('a') == [items[2]]
        assert repo.events('a', after_sequence=1) == []
        assert repo.get(Task, 'missing') is None


def test_wal_fk_timeout_and_idempotent_migration(store):
    migrate(store.factory)
    with store.factory.connection() as conn:
        assert conn.autocommit is True
        assert not conn.in_transaction
        assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert conn.execute('PRAGMA busy_timeout').fetchone()[0] == 50
        assert conn.execute('SELECT count(*) FROM schema_migrations').fetchone()[0] == 1
        assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {
            'schema_migrations', 'tasks', 'task_runs', 'task_events', 'content_versions'}


def test_transaction_rollback_all_records(store):
    with pytest.raises(RuntimeError):
        with store.transaction() as repo:
            for item in records():
                repo.add(item)
            raise RuntimeError('injected')
    with store.reader() as repo:
        assert repo.get(Task, 'a') is None
        assert repo.get(TaskRun, 'run-a') is None
        assert repo.events('a') == []
        assert repo.get(ContentVersion, 'version-a') is None


def test_atomic_pointer_and_status_update(store):
    task, run, event, version = records()
    with store.transaction() as repo:
        for item in (task, run, event, version):
            repo.add(item)
        assert repo.update_task('a', expected_status=Status.QUEUED, status=Status.AWAITING_APPROVAL,
                                updated_at=task.updated_at, current_run_id=run.run_id,
                                latest_content_version_id=version.content_version_id)
        assert not repo.update_task('a', expected_status=Status.QUEUED, status=Status.FAILED,
                                    updated_at=task.updated_at)
    with store.reader() as repo:
        saved = repo.get(Task, 'a')
        assert saved.latest_content_version_id == version.content_version_id
        assert saved.current_run_id == run.run_id


@pytest.mark.parametrize('kind', ['key','attempt','event_key','sequence','version_number'])
def test_unique_constraints(store, kind):
    task, run, event, version = records()
    with store.transaction() as repo:
        for item in (task, run, event, version):
            repo.add(item)
    duplicate = {
        'key': replace(task, task_id='other'),
        'attempt': replace(run, run_id='other'),
        'event_key': replace(event, event_id='other', sequence_number=2),
        'sequence': replace(event, event_id='other', event_key='other'),
        'version_number': replace(version, content_version_id='other'),
    }[kind]
    with pytest.raises(ConstraintViolation):
        with store.transaction() as repo:
            repo.add(duplicate)


@pytest.mark.parametrize('kind', ['run','event','event_attempt','version_run','version_type','pointer_run','pointer_version'])
def test_cross_task_and_type_constraints(store, kind):
    a, b = records('a'), records('b', ContentType.PAGE)
    with store.transaction() as repo:
        for item in (*a, *b):
            repo.add(item)
    with pytest.raises(ConstraintViolation):
        with store.transaction() as repo:
            if kind == 'pointer_run':
                repo.update_task('a', expected_status=Status.QUEUED, status=Status.RUNNING,
                                 updated_at=a[0].updated_at, current_run_id=b[1].run_id)
            elif kind == 'pointer_version':
                repo.update_task('a', expected_status=Status.QUEUED, status=Status.RUNNING,
                                 updated_at=a[0].updated_at, latest_content_version_id=b[3].content_version_id)
            else:
                invalid = {
                    'run': replace(a[1], run_id='bad', task_id='missing'),
                    'event': replace(a[2], event_id='bad', event_key='bad', sequence_number=2, run_id=b[1].run_id),
                    'event_attempt': replace(a[2], event_id='bad', event_key='bad', sequence_number=2, attempt=2),
                    'version_run': replace(a[3], content_version_id='bad', version_number=2, run_id=b[1].run_id),
                    'version_type': replace(a[3], content_version_id='bad', version_number=2, content_type=ContentType.PAGE),
                }[kind]
                repo.add(invalid)


@pytest.mark.parametrize('sql', [
    "UPDATE tasks SET brief='changed'",
    "UPDATE content_versions SET content='changed'",
    'DELETE FROM content_versions',
    "UPDATE task_events SET summary='changed'",
    'DELETE FROM task_events',
])
def test_immutable_history(store, sql):
    with store.transaction() as repo:
        for item in records():
            repo.add(item)
    with pytest.raises(ConstraintViolation):
        with store.factory.transaction() as conn:
            conn.execute(sql)


@pytest.mark.parametrize('raw', ['{', '{"schema_version":2,"data":{}}',
    '{"schema_version":true,"data":{}}', '{"schema_version":1,"data":NaN}',
    '{"schema_version":1,"data":{},"extra":0}',
    '{"schema_version":1,"data":{"x":1,"x":2}}'])
def test_bad_snapshot_is_controlled(raw):
    with pytest.raises(CodecError):
        decode_snapshot(raw)


@pytest.mark.parametrize('change', ['unknown','missing','enum','type'])
def test_strict_record_fields(change):
    envelope = json.loads(encode_record(records()[0]))
    data = envelope['data']
    if change == 'unknown':
        data['unexpected'] = 1
    elif change == 'missing':
        del data['topic']
    elif change == 'enum':
        data['content_type'] = 'PRODUCT'
    else:
        data['topic'] = 123
    with pytest.raises(CodecError):
        decode_record(Task, json.dumps(envelope))


def test_corrupt_stored_snapshot_is_controlled(store):
    with store.transaction() as repo:
        repo.add(records()[0])
        repo.add(records()[1])
    with store.factory.transaction() as conn:
        conn.execute("UPDATE task_runs SET workflow_state=?", ('{"schema_version":99,"data":{}}',))
    with pytest.raises(CodecError):
        with store.reader() as repo:
            repo.get(TaskRun, 'run-a')


@pytest.mark.parametrize('bad_sql', ['CREATE TABLE broken(', 'COMMIT;', 'PRAGMA foreign_keys=OFF;'])
def test_failed_migration_rolls_back_schema_and_history(tmp_path, bad_sql):
    directory = tmp_path / 'migrations'
    directory.mkdir()
    (directory / '0001_good.sql').write_text('CREATE TABLE sample(value TEXT);', encoding='utf-8')
    (directory / '0002_bad.sql').write_text(bad_sql, encoding='utf-8')
    factory = ConnectionFactory(tmp_path / 'test.sqlite3')
    with pytest.raises(PersistenceError):
        migrate(factory, directory)
    with factory.connection() as conn:
        assert list(conn.execute("SELECT name FROM sqlite_master WHERE type='table'")) == []


def test_migration_trigger_semicolon_and_checksum(tmp_path):
    directory = tmp_path / 'migrations'
    directory.mkdir()
    path = directory / '0001_initial.sql'
    path.write_text("CREATE TABLE t(x TEXT); INSERT INTO t VALUES ('a;b'); "
                    "CREATE TRIGGER no_delete BEFORE DELETE ON t BEGIN SELECT RAISE(ABORT,'no;delete'); END;", encoding='utf-8')
    factory = ConnectionFactory(tmp_path / 'test.sqlite3')
    migrate(factory, directory)
    with factory.connection() as conn:
        assert conn.execute('SELECT x FROM t').fetchone()[0] == 'a;b'
    path.write_text('CREATE TABLE changed(x);', encoding='utf-8')
    with pytest.raises(PersistenceError, match='modified'):
        migrate(factory, directory)


def test_competing_writer_times_out_but_reader_works(store):
    with store.transaction() as repo:
        repo.add(records()[0])
    def compete():
        with store.transaction() as other:
            other.add(records('b')[0])
    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.transaction():
            with store.reader() as reader:
                assert reader.get(Task, 'a') is not None
            with pytest.raises(DatabaseBusy):
                pool.submit(compete).result(timeout=3)
    with store.transaction() as repo:
        repo.add(records('b')[0])


def test_reader_cannot_write(store):
    with pytest.raises(PersistenceError):
        with store.reader() as repo:
            repo.add(records()[0])


def test_future_optimization_remains_null(store):
    with pytest.raises(ConstraintViolation):
        with store.transaction() as repo:
            task, run, _, version = records()
            repo.add(task)
            repo.add(run)
            repo.add(replace(version, aeo_data={'fake': 'PASS'}))


@pytest.mark.parametrize('owner,token,expected,accepted', [
    ('worker', 3, Status.RUNNING, True),
    ('other', 3, Status.RUNNING, False),
    ('worker', 2, Status.RUNNING, False),
    ('worker', 3, Status.QUEUED, False),
])
def test_run_snapshot_conditional_update(store, owner, token, expected, accepted):
    task, run, _, _ = records()
    run = replace(run, owner_id='worker', fencing_token=3, status=Status.RUNNING)
    with store.transaction() as repo:
        repo.add(task)
        repo.add(run)
        assert repo.update_run_snapshot(run.run_id, owner_id=owner, fencing_token=token,
            expected_status=expected, status=Status.AWAITING_APPROVAL,
            updated_at=run.updated_at, workflow_state={'draft': '完成'}) is accepted
    with store.reader() as repo:
        result = repo.get(TaskRun, run.run_id)
        assert result.workflow_state == ({'draft': '完成'} if accepted else run.workflow_state)


def test_failed_upgrade_preserves_previous_schema(tmp_path):
    directory = tmp_path / 'migrations'
    directory.mkdir()
    (directory / '0001_good.sql').write_text('CREATE TABLE t(x TEXT);', encoding='utf-8')
    factory = ConnectionFactory(tmp_path / 'app.sqlite3')
    migrate(factory, directory)
    with factory.transaction() as conn:
        conn.execute("INSERT INTO t VALUES ('保留')")
    (directory / '0002_bad.sql').write_text('ALTER TABLE t ADD COLUMN y TEXT; INSERT INTO absent VALUES (1);', encoding='utf-8')
    with pytest.raises(PersistenceError):
        migrate(factory, directory)
    with factory.connection() as conn:
        assert [r[1] for r in conn.execute('PRAGMA table_info(t)')] == ['x']
        assert conn.execute('SELECT x FROM t').fetchone()[0] == '保留'
        assert [r[0] for r in conn.execute('SELECT version FROM schema_migrations')] == [1]


def test_concurrent_migrations_share_one_history(store):
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(migrate, ConnectionFactory(store.factory.path, busy_timeout_ms=2000))
                   for _ in range(2)]
        for future in futures:
            future.result(timeout=5)
    with store.factory.connection() as conn:
        assert conn.execute('SELECT count(*) FROM schema_migrations').fetchone()[0] == 1
