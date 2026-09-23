import json
import sqlite3
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor

import pytest

from domain.providers import DEFAULT_WORKSPACE_ID
from domain.contracts import Task, TaskRun, TaskEvent, ContentVersion, ContentType, Status
from persistence.codec import CodecError, encode_record, decode_record, decode_snapshot, encode_snapshot
from persistence.connection import ConnectionFactory, PersistenceError, ConstraintViolation, DatabaseBusy
from persistence.migration_runner import migrate
from persistence.repository import SQLiteStore
from domain.ai_runtime import RuntimeOnly, ProviderBundle


@pytest.fixture
def store(tmp_path):
    factory = ConnectionFactory(tmp_path / 'app.sqlite3', busy_timeout_ms=50)
    migrate(factory)
    return SQLiteStore(factory)


def records(suffix='a', content_type=ContentType.POST):
    task = Task(task_id=suffix, workspace_id=DEFAULT_WORKSPACE_ID, submission_key='key-'+suffix, site_id='site-'+suffix,
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


def test_workflow_state_image_statuses_round_trip(store):
    for status in ('READY', 'FAILED', 'PENDING'):
        task = Task(task_id='t-'+status, workspace_id=DEFAULT_WORKSPACE_ID, submission_key='key-'+status,
                     site_id='site-'+status, content_type=ContentType.POST, topic='主題',
                     brief='需求', brand_profile_id='brand',
                     created_at='2026-09-17T00:00:00Z', updated_at='2026-09-17T00:00:00Z',
                     request_snapshot={}, approval_policy_snapshot={'mode': 'REQUIRE_HUMAN_REVIEW'})
        run = TaskRun(run_id='rt-'+status, task_id='t-'+status, attempt=1,
                       created_at='2026-09-17T00:00:00Z', updated_at='2026-09-17T00:00:00Z',
                       workflow_state={'draft': None, 'image': {'status': status, 'artifact_id': 'img-'+status},
                                       'items': [1, True, None], 'summary': '繁體中文測試'})
        with store.transaction() as repo:
            repo.add(task)
            repo.add(run)
        reopened = SQLiteStore(ConnectionFactory(store.factory.path))
        with reopened.reader() as repo:
            saved = repo.get(TaskRun, 'rt-'+status)
            assert saved.workflow_state['image']['status'] == status
            assert saved.workflow_state['items'] == [1, True, None]
            assert saved.workflow_state['summary'] == '繁體中文測試'
            assert decode_record(TaskRun, encode_record(saved)) == saved


def test_workflow_state_schema_version_preserved(store):
    task = Task(task_id='t-sv', workspace_id=DEFAULT_WORKSPACE_ID, submission_key='key-sv',
                 site_id='site-sv', content_type=ContentType.POST, topic='主題',
                 brief='需求', brand_profile_id='brand',
                 created_at='2026-09-17T00:00:00Z', updated_at='2026-09-17T00:00:00Z',
                 request_snapshot={}, approval_policy_snapshot={'mode': 'REQUIRE_HUMAN_REVIEW'})
    run = TaskRun(run_id='rt-sv', task_id='t-sv', attempt=1,
                   created_at='2026-09-17T00:00:00Z', updated_at='2026-09-17T00:00:00Z',
                   workflow_state={'draft': None, 'image': {'status': 'READY'}, 'items': [1]})
    with store.transaction() as repo:
        repo.add(task)
        repo.add(run)
    encoded = encode_snapshot(run.workflow_state)
    envelope = json.loads(encoded)
    assert envelope['schema_version'] == 1
    assert 'data' in envelope


def test_workflow_state_unsupported_type_fail_closed(store):
    runtime_obj = RuntimeOnly()
    with pytest.raises(CodecError):
        encode_snapshot({'provider': runtime_obj})
    with pytest.raises(CodecError):
        encode_snapshot({'client': object()})


def test_workflow_state_no_secrets_in_serialized(store):
    from domain.ai_runtime import ProviderBundle, TextProvider
    from unittest.mock import Mock
    provider = Mock(spec=TextProvider)
    provider.complete = Mock(return_value='result')
    bundle = ProviderBundle(provider)
    task = Task(task_id='t-secret', workspace_id=DEFAULT_WORKSPACE_ID, submission_key='key-secret',
                 site_id='site-secret', content_type=ContentType.POST, topic='主題',
                 brief='需求', brand_profile_id='brand',
                 created_at='2026-09-17T00:00:00Z', updated_at='2026-09-17T00:00:00Z',
                 request_snapshot={}, approval_policy_snapshot={'mode': 'REQUIRE_HUMAN_REVIEW'})
    run = TaskRun(run_id='rt-secret', task_id='t-secret', attempt=1,
                   created_at='2026-09-17T00:00:00Z', updated_at='2026-09-17T00:00:00Z',
                   workflow_state={'draft': None, 'image': {'status': 'READY'}, 'items': [1]})
    serialized = encode_snapshot(run.workflow_state)
    assert 'provider' not in serialized
    assert 'client' not in serialized
    assert 'api_key' not in serialized
    assert 'password' not in serialized
    assert 'Authorization' not in serialized


def test_workflow_state_chinese_and_complex_types_round_trip(store):
    task = Task(task_id='t-zh', workspace_id=DEFAULT_WORKSPACE_ID, submission_key='key-zh',
                 site_id='site-zh', content_type=ContentType.POST, topic='繁體中文主題',
                 brief='內容需求', brand_profile_id='brand',
                 created_at='2026-09-17T00:00:00Z', updated_at='2026-09-17T00:00:00Z',
                 request_snapshot={}, approval_policy_snapshot={'mode': 'REQUIRE_HUMAN_REVIEW'})
    run = TaskRun(run_id='rt-zh', task_id='t-zh', attempt=1,
                   created_at='2026-09-17T00:00:00Z', updated_at='2026-09-17T00:00:00Z',
                   workflow_state={'draft': '繁體中文內容', 'image': {'status': 'READY', 'metadata': {'描述': '測試'}},
                                   'items': [1, True, None, {'key': '值'}], 'nested': {'list': [1, 2, {'deep': '中文'}]}})
    with store.transaction() as repo:
        repo.add(task)
        repo.add(run)
    reopened = SQLiteStore(ConnectionFactory(store.factory.path))
    with reopened.reader() as repo:
        saved = repo.get(TaskRun, 'rt-zh')
        assert saved.workflow_state['draft'] == '繁體中文內容'
        assert saved.workflow_state['image']['metadata']['描述'] == '測試'
        assert saved.workflow_state['nested']['list'][2]['deep'] == '中文'
        assert decode_record(TaskRun, encode_record(saved)) == saved


def test_workflow_state_unknown_schema_version_fail_closed(store):
    with pytest.raises(CodecError):
        decode_snapshot('{"schema_version":99,"data":{}}')
    with pytest.raises(CodecError):
        decode_snapshot('{"schema_version":0,"data":{}}')
    with pytest.raises(CodecError):
        decode_snapshot('{"schema_version":"1","data":{}}')


def test_workflow_state_corrupt_json_fail_closed(store):
    for raw in ['{', '{"schema_version":1,"data":NaN}', '{"schema_version":1,"data":{}}extra']:
        with pytest.raises(CodecError):
            decode_snapshot(raw)


def test_wal_fk_timeout_and_idempotent_migration(store):
    migrate(store.factory)
    with store.factory.connection() as conn:
        assert conn.autocommit is True
        assert not conn.in_transaction
        assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert conn.execute('PRAGMA busy_timeout').fetchone()[0] == 50
        assert [r[0] for r in conn.execute(
            'SELECT version FROM schema_migrations ORDER BY version')] == [1, 2, 3, 4]
        assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {
            'schema_migrations', 'tasks', 'task_runs', 'task_events', 'content_versions',
            'workspaces', 'ai_provider_connections', 'ai_invocations', 'task_retry_requests'}


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
        assert [r[0] for r in conn.execute(
            'SELECT version FROM schema_migrations ORDER BY version')] == [1, 2, 3, 4]


def test_migration_0004_schema_migrations_version_list(store):
    """Fresh database after migrate() has schema_migrations versions [1, 2, 3, 4]."""
    with store.factory.connection() as conn:
        versions = [r[0] for r in conn.execute(
            'SELECT version FROM schema_migrations ORDER BY version')]
        assert versions == [1, 2, 3, 4]


def test_migration_0004_groq_provider_type_allowed_in_ai_provider_connections(store):
    """After migration 0004, ai_provider_connections allows GROQ provider_type."""
    from domain.providers import AIProviderConnection, Capability, Workspace, VerificationStatus, ProviderMode
    from service.execution import now

    ws = Workspace(workspace_id='test-ws', workspace_key='test', name='Test', created_at=now(), updated_at=now())
    conn = AIProviderConnection(
        provider_connection_id='conn-groq',
        workspace_id='test-ws',
        provider_type='GROQ',
        provider_mode=ProviderMode.PLATFORM_MANAGED,
        capabilities=[Capability.TEXT],
        default_model='qwen/qwen3.8-27b',
        verification_status=VerificationStatus.VERIFIED,
        non_secret_configuration={},
        credential_reference='env:GROQ_API_KEY',
        configuration_version=1,
        created_at=now(),
        updated_at=now()
    )
    with store.transaction() as repo:
        repo.add(ws)
        repo.add(conn)
    with store.reader() as repo:
        saved = repo.get(AIProviderConnection, 'conn-groq')
        assert saved.provider_type == 'GROQ'


def test_migration_0004_groq_provider_type_allowed_in_task_runs(store):
    """After migration 0004, task_runs allows GROQ provider_type."""
    from domain.contracts import Task, TaskRun, Status
    from domain.providers import AIProviderConnection, Capability, Workspace, VerificationStatus, ProviderMode
    from service.execution import now

    ws = Workspace(workspace_id='test-ws', workspace_key='test', name='Test', created_at=now(), updated_at=now())
    conn = AIProviderConnection(
        provider_connection_id='conn-groq',
        workspace_id='test-ws',
        provider_type='GROQ',
        provider_mode=ProviderMode.PLATFORM_MANAGED,
        capabilities=[Capability.TEXT],
        default_model='qwen/qwen3.8-27b',
        verification_status=VerificationStatus.VERIFIED,
        non_secret_configuration={},
        credential_reference='env:GROQ_API_KEY',
        configuration_version=1,
        created_at=now(),
        updated_at=now()
    )
    task = Task(task_id='task-groq', workspace_id='test-ws', submission_key='key-groq',
                site_id='site-groq', content_type=ContentType.POST, topic='主題',
                brief='需求', brand_profile_id='brand',
                created_at=now(), updated_at=now(),
                request_snapshot={}, approval_policy_snapshot={'mode': 'REQUIRE_HUMAN_REVIEW'})
    run = TaskRun(run_id='run-groq', task_id='task-groq', attempt=1,
                  created_at=now(), updated_at=now(),
                  run_mode='INITIAL', status=Status.QUEUED,
                  owner_id=None, fencing_token=0, workflow_state=None,
                  claimed_at=None, started_at=None, heartbeat_at=None, finished_at=None, error=None,
                  resumed_from_run_id=None, resumed_from_checkpoint_id=None,
                  provider_connection_id='conn-groq', provider_type='GROQ',
                  provider_mode=ProviderMode.PLATFORM_MANAGED.value,
                  model='qwen/qwen3.8-27b', provider_configuration_version=1)

    with store.transaction() as repo:
        repo.add(ws)
        repo.add(conn)
        repo.add(task)
        repo.add(run)
    with store.reader() as repo:
        saved = repo.get(TaskRun, 'run-groq')
        assert saved.provider_type == 'GROQ'


def test_migration_0004_groq_provider_type_allowed_in_ai_invocations(store):
    """After migration 0004, ai_invocations allows GROQ provider_type."""
    from domain.providers import AIProviderConnection, Capability, Workspace, VerificationStatus, ProviderMode, AIInvocation, InvocationStatus
    from domain.contracts import Task, TaskRun, Status
    from service.execution import now

    ws = Workspace(workspace_id='test-ws', workspace_key='test', name='Test', created_at=now(), updated_at=now())
    conn = AIProviderConnection(
        provider_connection_id='conn-groq',
        workspace_id='test-ws',
        provider_type='GROQ',
        provider_mode=ProviderMode.PLATFORM_MANAGED,
        capabilities=[Capability.TEXT],
        default_model='qwen/qwen3.8-27b',
        verification_status=VerificationStatus.VERIFIED,
        non_secret_configuration={},
        credential_reference='env:GROQ_API_KEY',
        configuration_version=1,
        created_at=now(),
        updated_at=now()
    )
    task = Task(task_id='task-groq', workspace_id='test-ws', submission_key='key-groq',
                site_id='site-groq', content_type=ContentType.POST, topic='主題',
                brief='需求', brand_profile_id='brand',
                created_at=now(), updated_at=now(),
                request_snapshot={}, approval_policy_snapshot={'mode': 'REQUIRE_HUMAN_REVIEW'})
    run = TaskRun(run_id='run-groq', task_id='task-groq', attempt=1,
                  created_at=now(), updated_at=now(),
                  run_mode='INITIAL', status=Status.QUEUED,
                  owner_id=None, fencing_token=0, workflow_state=None,
                  claimed_at=None, started_at=None, heartbeat_at=None, finished_at=None, error=None,
                  resumed_from_run_id=None, resumed_from_checkpoint_id=None,
                  provider_connection_id='conn-groq', provider_type='GROQ',
                  provider_mode=ProviderMode.PLATFORM_MANAGED.value,
                  model='qwen/qwen3.8-27b', provider_configuration_version=1)
    inv = AIInvocation(
        invocation_id='inv-groq',
        workspace_id='test-ws',
        task_id='task-groq',
        run_id='run-groq',
        provider_connection_id='conn-groq',
        capability=Capability.TEXT,
        provider_type='GROQ',
        model='qwen/qwen3.8-27b',
        input_tokens=10,
        output_tokens=20,
        total_tokens=30,
        latency_ms=100,
        estimated_cost_decimal='0.0001',
        currency='USD',
        pricing_reference='catalog:groq/text/qwen',
        status=InvocationStatus.SUCCEEDED,
        classified_error=None,
        created_at=now()
    )

    with store.transaction() as repo:
        repo.add(ws)
        repo.add(conn)
        repo.add(task)
        repo.add(run)
        repo.add(inv)
    with store.reader() as repo:
        saved = repo.invocations('run-groq')
        assert len(saved) == 1
        assert saved[0].provider_type == 'GROQ'


def test_migration_0004_unknown_provider_type_rejected_in_ai_provider_connections(store):
    """After migration 0004, ai_provider_connections rejects unknown provider_type via DB CHECK."""
    from domain.providers import AIProviderConnection, Capability, Workspace, VerificationStatus, ProviderMode
    from service.execution import now

    ws = Workspace(workspace_id='test-ws', workspace_key='test', name='Test', created_at=now(), updated_at=now())
    conn = AIProviderConnection(
        provider_connection_id='conn-groq',
        workspace_id='test-ws',
        provider_type='GROQ',
        provider_mode=ProviderMode.PLATFORM_MANAGED,
        capabilities=[Capability.TEXT],
        default_model='qwen/qwen3.8-27b',
        verification_status=VerificationStatus.VERIFIED,
        non_secret_configuration={},
        credential_reference='env:GROQ_API_KEY',
        configuration_version=1,
        created_at=now(),
        updated_at=now()
    )
    with store.transaction() as repo:
        repo.add(ws)
        repo.add(conn)

    # Direct SQL INSERT with UNKNOWN provider_type to test DB CHECK constraint
    with pytest.raises(ConstraintViolation):
        with store.transaction() as repo:
            repo._conn.execute("""
                INSERT INTO ai_provider_connections (
                    provider_connection_id, workspace_id, provider_type, provider_mode,
                    capabilities, default_model, verification_status, non_secret_configuration,
                    credential_reference, configuration_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                'conn-unknown', 'test-ws', 'UNKNOWN', 'PLATFORM_MANAGED',
                '["TEXT"]', 'some-model', 'VERIFIED', '{}',
                'env:UNKNOWN_KEY', 1, now(), now()
            ))


def test_migration_0004_unknown_provider_type_rejected_in_task_runs(store):
    """After migration 0004, task_runs rejects unknown provider_type."""
    from domain.contracts import Task, TaskRun, Status
    from domain.providers import AIProviderConnection, Capability, Workspace, VerificationStatus, ProviderMode
    from service.execution import now

    ws = Workspace(workspace_id='test-ws', workspace_key='test', name='Test', created_at=now(), updated_at=now())
    conn = AIProviderConnection(
        provider_connection_id='conn-groq',
        workspace_id='test-ws',
        provider_type='GROQ',
        provider_mode=ProviderMode.PLATFORM_MANAGED,
        capabilities=[Capability.TEXT],
        default_model='qwen/qwen3.8-27b',
        verification_status=VerificationStatus.VERIFIED,
        non_secret_configuration={},
        credential_reference='env:GROQ_API_KEY',
        configuration_version=1,
        created_at=now(),
        updated_at=now()
    )
    task = Task(task_id='task-groq', workspace_id='test-ws', submission_key='key-groq',
                site_id='site-groq', content_type=ContentType.POST, topic='主題',
                brief='需求', brand_profile_id='brand',
                created_at=now(), updated_at=now(),
                request_snapshot={}, approval_policy_snapshot={'mode': 'REQUIRE_HUMAN_REVIEW'})
    run = TaskRun(run_id='run-unknown', task_id='task-groq', attempt=1,
                  created_at=now(), updated_at=now(),
                  run_mode='INITIAL', status=Status.QUEUED,
                  owner_id=None, fencing_token=0, workflow_state=None,
                  claimed_at=None, started_at=None, heartbeat_at=None, finished_at=None, error=None,
                  resumed_from_run_id=None, resumed_from_checkpoint_id=None,
                  provider_connection_id='conn-groq', provider_type='UNKNOWN',  # Not allowed
                  provider_mode=ProviderMode.PLATFORM_MANAGED.value,
                  model='some-model', provider_configuration_version=1)

    with pytest.raises(ConstraintViolation):
        with store.transaction() as repo:
            repo.add(ws)
            repo.add(conn)
            repo.add(task)
            repo.add(run)


def test_migration_0004_unknown_provider_type_rejected_in_ai_invocations(store):
    """After migration 0004, ai_invocations rejects unknown provider_type via DB CHECK."""
    from domain.providers import AIProviderConnection, Capability, Workspace, VerificationStatus, ProviderMode, AIInvocation, InvocationStatus
    from domain.contracts import Task, TaskRun, Status
    from service.execution import now

    ws = Workspace(workspace_id='test-ws', workspace_key='test', name='Test', created_at=now(), updated_at=now())
    conn = AIProviderConnection(
        provider_connection_id='conn-groq',
        workspace_id='test-ws',
        provider_type='GROQ',
        provider_mode=ProviderMode.PLATFORM_MANAGED,
        capabilities=[Capability.TEXT],
        default_model='qwen/qwen3.8-27b',
        verification_status=VerificationStatus.VERIFIED,
        non_secret_configuration={},
        credential_reference='env:GROQ_API_KEY',
        configuration_version=1,
        created_at=now(),
        updated_at=now()
    )
    task = Task(task_id='task-groq', workspace_id='test-ws', submission_key='key-groq',
                site_id='site-groq', content_type=ContentType.POST, topic='主題',
                brief='需求', brand_profile_id='brand',
                created_at=now(), updated_at=now(),
                request_snapshot={}, approval_policy_snapshot={'mode': 'REQUIRE_HUMAN_REVIEW'})
    run = TaskRun(run_id='run-groq', task_id='task-groq', attempt=1,
                  created_at=now(), updated_at=now(),
                  run_mode='INITIAL', status=Status.QUEUED,
                  owner_id=None, fencing_token=0, workflow_state=None,
                  claimed_at=None, started_at=None, heartbeat_at=None, finished_at=None, error=None,
                  resumed_from_run_id=None, resumed_from_checkpoint_id=None,
                  provider_connection_id='conn-groq', provider_type='GROQ',
                  provider_mode=ProviderMode.PLATFORM_MANAGED.value,
                  model='qwen/qwen3.8-27b', provider_configuration_version=1)

    with store.transaction() as repo:
        repo.add(ws)
        repo.add(conn)
        repo.add(task)
        repo.add(run)

    # Direct SQL INSERT with UNKNOWN provider_type to test DB CHECK constraint
    with pytest.raises(ConstraintViolation):
        with store.transaction() as repo:
            repo._conn.execute("""
                INSERT INTO ai_invocations (
                    invocation_id, workspace_id, task_id, run_id, provider_connection_id,
                    capability, provider_type, model, input_tokens, output_tokens, total_tokens,
                    latency_ms, estimated_cost_decimal, currency, pricing_reference,
                    status, classified_error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                'inv-unknown', 'test-ws', 'task-groq', 'run-groq', 'conn-groq',
                'TEXT', 'UNKNOWN', 'some-model', 10, 20, 30,
                100, '0.0001', 'USD', 'catalog:groq/text/qwen',
                'SUCCEEDED', None, now()
            ))


def test_migration_0004_upgrades_v3_preserves_openai_rows_and_is_idempotent(tmp_path):
    """Migration 0004 upgrades v3->v4 preserving all OPENAI data and is idempotent."""
    import shutil
    from domain.providers import (
        AIProviderConnection, Capability, Workspace, VerificationStatus, ProviderMode,
        AIInvocation, InvocationStatus
    )
    from domain.contracts import Task, TaskRun, Status, ContentType
    from service.execution import now
    from persistence.connection import ConnectionFactory
    from persistence.migration_runner import migrate
    from persistence.repository import SQLiteStore

    # 1. Create tmp migrations dir with only 0001-0003
    migrations_src = tmp_path / 'migrations_src'
    migrations_src.mkdir()
    for name in ('0001_initial.sql', '0002_workspace_provider.sql', '0003_retry_idempotency.sql'):
        shutil.copy(f'persistence/migrations/{name}', migrations_src / name)

    # 2. Create DB and migrate to v3
    db_path = tmp_path / 'app.sqlite3'
    factory = ConnectionFactory(db_path, busy_timeout_ms=50)
    migrate(factory, migrations_src)

    with factory.connection() as conn:
        v3_versions = [r[0] for r in conn.execute(
            'SELECT version FROM schema_migrations ORDER BY version')]
        assert v3_versions == [1, 2, 3]

    # 3. Create OPENAI data at v3 using domain objects
    store = SQLiteStore(factory)
    now_ts = now()

    ws = Workspace(
        workspace_id='test-ws', workspace_key='test', name='Test',
        created_at=now_ts, updated_at=now_ts
    )
    conn = AIProviderConnection(
        provider_connection_id='conn-openai',
        workspace_id='test-ws',
        provider_type='OPENAI',
        provider_mode=ProviderMode.PLATFORM_MANAGED,
        capabilities=[Capability.TEXT, Capability.IMAGE, Capability.VISUAL_QUALITY],
        default_model='gpt-4',
        verification_status=VerificationStatus.VERIFIED,
        non_secret_configuration={},
        credential_reference='env:OPENAI_API_KEY',
        configuration_version=1,
        created_at=now_ts,
        updated_at=now_ts
    )
    task = Task(
        task_id='task-openai', workspace_id='test-ws', submission_key='key-openai',
        site_id='site-openai', content_type=ContentType.POST, topic='主題',
        brief='需求', brand_profile_id='brand',
        created_at=now_ts, updated_at=now_ts,
        request_snapshot={}, approval_policy_snapshot={'mode': 'REQUIRE_HUMAN_REVIEW'}
    )
    run = TaskRun(
        run_id='run-openai', task_id='task-openai', attempt=1,
        created_at=now_ts, updated_at=now_ts,
        run_mode='INITIAL', status=Status.QUEUED,
        owner_id=None, fencing_token=0, workflow_state=None,
        claimed_at=None, started_at=None, heartbeat_at=None, finished_at=None, error=None,
        resumed_from_run_id=None, resumed_from_checkpoint_id=None,
        provider_connection_id='conn-openai', provider_type='OPENAI',
        provider_mode=ProviderMode.PLATFORM_MANAGED.value,
        model='gpt-4', provider_configuration_version=1
    )
    inv = AIInvocation(
        invocation_id='inv-openai',
        workspace_id='test-ws',
        task_id='task-openai',
        run_id='run-openai',
        provider_connection_id='conn-openai',
        capability=Capability.TEXT,
        provider_type='OPENAI',
        model='gpt-4',
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        latency_ms=500,
        estimated_cost_decimal='0.002',
        currency='USD',
        pricing_reference='catalog:openai/text/gpt4',
        status=InvocationStatus.SUCCEEDED,
        classified_error=None,
        created_at=now_ts
    )

    with store.transaction() as repo:
        repo.add(ws)
        repo.add(conn)
        repo.add(task)
        repo.add(run)
        repo.add(inv)

    # 4. Record pre-migration state
    with store.factory.connection() as conn:
        pre_conn = conn.execute(
            'SELECT provider_connection_id, workspace_id, provider_type, provider_mode, '
            'capabilities, default_model, verification_status, non_secret_configuration, '
            'credential_reference, configuration_version, created_at, updated_at '
            'FROM ai_provider_connections WHERE provider_connection_id=?', ('conn-openai',)
        ).fetchone()
        pre_run = conn.execute(
            'SELECT run_id, task_id, provider_connection_id, provider_type, provider_mode, '
            'model, provider_configuration_version '
            'FROM task_runs WHERE run_id=?', ('run-openai',)
        ).fetchone()
        pre_inv = conn.execute(
            'SELECT invocation_id, workspace_id, task_id, run_id, provider_connection_id, '
            'capability, provider_type, model, input_tokens, output_tokens, total_tokens, '
            'latency_ms, estimated_cost_decimal, currency, pricing_reference, '
            'status, classified_error, created_at '
            'FROM ai_invocations WHERE invocation_id=?', ('inv-openai',)
        ).fetchone()
        pre_counts = {
            'connections': conn.execute('SELECT COUNT(*) FROM ai_provider_connections').fetchone()[0],
            'runs': conn.execute('SELECT COUNT(*) FROM task_runs').fetchone()[0],
            'invocations': conn.execute('SELECT COUNT(*) FROM ai_invocations').fetchone()[0],
        }

    # 5. Add 0004 migration and re-run migrate
    shutil.copy('persistence/migrations/0004_groq_provider.sql', migrations_src / '0004_groq_provider.sql')
    migrate(factory, migrations_src)

    # 6. Verify post-migration state
    with store.factory.connection() as conn:
        v4_versions = [r[0] for r in conn.execute(
            'SELECT version FROM schema_migrations ORDER BY version')]
        assert v4_versions == [1, 2, 3, 4]

        post_conn = conn.execute(
            'SELECT provider_connection_id, workspace_id, provider_type, provider_mode, '
            'capabilities, default_model, verification_status, non_secret_configuration, '
            'credential_reference, configuration_version, created_at, updated_at '
            'FROM ai_provider_connections WHERE provider_connection_id=?', ('conn-openai',)
        ).fetchone()
        post_run = conn.execute(
            'SELECT run_id, task_id, provider_connection_id, provider_type, provider_mode, '
            'model, provider_configuration_version '
            'FROM task_runs WHERE run_id=?', ('run-openai',)
        ).fetchone()
        post_inv = conn.execute(
            'SELECT invocation_id, workspace_id, task_id, run_id, provider_connection_id, '
            'capability, provider_type, model, input_tokens, output_tokens, total_tokens, '
            'latency_ms, estimated_cost_decimal, currency, pricing_reference, '
            'status, classified_error, created_at '
            'FROM ai_invocations WHERE invocation_id=?', ('inv-openai',)
        ).fetchone()
        post_counts = {
            'connections': conn.execute('SELECT COUNT(*) FROM ai_provider_connections').fetchone()[0],
            'runs': conn.execute('SELECT COUNT(*) FROM task_runs').fetchone()[0],
            'invocations': conn.execute('SELECT COUNT(*) FROM ai_invocations').fetchone()[0],
        }

    assert pre_conn == post_conn, 'ai_provider_connections row changed after migration'
    assert pre_run == post_run, 'task_runs row changed after migration'
    assert pre_inv == post_inv, 'ai_invocations row changed after migration'
    assert pre_counts == post_counts, 'Row counts changed after migration'

    # 7. Run migrate again (idempotency)
    migrate(factory, migrations_src)

    with store.factory.connection() as conn:
        v4_versions_again = [r[0] for r in conn.execute(
            'SELECT version FROM schema_migrations ORDER BY version')]
        assert v4_versions_again == [1, 2, 3, 4], 'Second migrate changed schema versions'

        counts_again = {
            'connections': conn.execute('SELECT COUNT(*) FROM ai_provider_connections').fetchone()[0],
            'runs': conn.execute('SELECT COUNT(*) FROM task_runs').fetchone()[0],
            'invocations': conn.execute('SELECT COUNT(*) FROM ai_invocations').fetchone()[0],
        }
        assert counts_again == post_counts, 'Row counts changed on second migrate'

        conn_again = conn.execute(
            'SELECT provider_connection_id, workspace_id, provider_type, provider_mode, '
            'capabilities, default_model, verification_status, non_secret_configuration, '
            'credential_reference, configuration_version, created_at, updated_at '
            'FROM ai_provider_connections WHERE provider_connection_id=?', ('conn-openai',)
        ).fetchone()
        assert conn_again == post_conn, 'Data changed on second migrate'


def test_migration_0004_preserves_indexes_triggers_and_foreign_keys(tmp_path):
    """Migration 0004 preserves indexes, triggers, and FK integrity for rebuilt tables."""
    import shutil
    from persistence.connection import ConnectionFactory
    from persistence.migration_runner import migrate

    # 1. Create tmp migrations dir with only 0001-0003
    migrations_src = tmp_path / 'migrations_src'
    migrations_src.mkdir()
    for name in ('0001_initial.sql', '0002_workspace_provider.sql', '0003_retry_idempotency.sql'):
        shutil.copy(f'persistence/migrations/{name}', migrations_src / name)

    # 2. Create DB and migrate to v3
    db_path = tmp_path / 'app.sqlite3'
    factory = ConnectionFactory(db_path, busy_timeout_ms=50)
    migrate(factory, migrations_src)

    # 3. Capture v3 indexes and triggers for the three rebuilt tables
    with factory.connection() as conn:
        v3_objects = conn.execute("""
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE tbl_name IN ('ai_provider_connections', 'task_runs', 'ai_invocations')
              AND type IN ('index', 'trigger')
              AND name NOT LIKE 'sqlite_autoindex%'
            ORDER BY type, tbl_name, name
        """).fetchall()
        v3_indexes = {(r['type'], r['name'], r['tbl_name'], r['sql']) for r in v3_objects}

        # Also capture FK list for reference
        v3_fks = {}
        for table in ('ai_provider_connections', 'task_runs', 'ai_invocations'):
            fks = conn.execute(f'PRAGMA foreign_key_list({table})').fetchall()
            v3_fks[table] = [(r['id'], r['seq'], r['table'], r['from'], r['to'], r['on_update'], r['on_delete'], r['match']) for r in fks]

    # 4. Add 0004 and migrate to v4
    shutil.copy('persistence/migrations/0004_groq_provider.sql', migrations_src / '0004_groq_provider.sql')
    migrate(factory, migrations_src)

    # 5. Verify v4
    with factory.connection() as conn:
        # Schema versions
        v4_versions = [r[0] for r in conn.execute(
            'SELECT version FROM schema_migrations ORDER BY version')]
        assert v4_versions == [1, 2, 3, 4]

        # PRAGMA foreign_keys = 1
        fk_status = conn.execute('PRAGMA foreign_keys').fetchone()[0]
        assert fk_status == 1, 'PRAGMA foreign_keys should be 1'

        # PRAGMA foreign_key_check returns no rows
        fk_check = conn.execute('PRAGMA foreign_key_check').fetchall()
        assert len(fk_check) == 0, f'Foreign key violations found: {fk_check}'

        # Capture v4 indexes and triggers for the three rebuilt tables
        v4_objects = conn.execute("""
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE tbl_name IN ('ai_provider_connections', 'task_runs', 'ai_invocations')
              AND type IN ('index', 'trigger')
              AND name NOT LIKE 'sqlite_autoindex%'
            ORDER BY type, tbl_name, name
        """).fetchall()
        v4_indexes = {(r['type'], r['name'], r['tbl_name'], r['sql']) for r in v4_objects}

        # Verify all v3 named indexes/triggers for these tables still exist in v4
        missing = v3_indexes - v4_indexes
        assert not missing, f'Missing indexes/triggers after migration: {missing}'

        # Verify FK constraints still work - try to insert invalid FK
        # ai_provider_connections has no FKs in v3/v4
        # task_runs has FK to ai_provider_connections
        # ai_invocations has FKs to workspace, task, run, provider_connection

        # Try to insert task_run with non-existent provider_connection_id
        try:
            conn.execute("""
                INSERT INTO task_runs (
                    run_id, task_id, attempt, created_at, updated_at, run_mode, status,
                    owner_id, fencing_token, workflow_state, claimed_at, started_at,
                    heartbeat_at, finished_at, error, resumed_from_run_id,
                    resumed_from_checkpoint_id, provider_connection_id, provider_type,
                    provider_mode, model, provider_configuration_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                'bad-fk-run', 'bad-fk-task', 1, '2026-09-17T00:00:00Z', '2026-09-17T00:00:00Z',
                'INITIAL', 'QUEUED', None, 0, None, None, None, None, None, None,
                None, None, 'non-existent-conn', 'OPENAI', 'PLATFORM_MANAGED', 'gpt-4', 1
            ))
            assert False, 'Should have raised ConstraintViolation for invalid FK'
        except Exception as e:
            # SQLite raises IntegrityError/ConstraintViolation for FK violation
            pass

        # Try to insert ai_invocation with non-existent provider_connection_id
        try:
            conn.execute("""
                INSERT INTO ai_invocations (
                    invocation_id, workspace_id, task_id, run_id, provider_connection_id,
                    capability, provider_type, model, input_tokens, output_tokens, total_tokens,
                    latency_ms, estimated_cost_decimal, currency, pricing_reference,
                    status, classified_error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                'bad-fk-inv', 'test-ws', 'bad-task', 'bad-run', 'non-existent-conn',
                'TEXT', 'OPENAI', 'gpt-4', 10, 20, 30,
                100, '0.001', 'USD', 'catalog:openai:text:gpt4',
                'SUCCEEDED', None, '2026-09-17T00:00:00Z'
            ))
            assert False, 'Should have raised ConstraintViolation for invalid FK'
        except Exception as e:
            pass


def test_migration_0004_failure_rolls_back_atomically_and_can_retry(tmp_path):
    """Migration 0004 failure rolls back atomically and retry with real 0004 succeeds."""
    import shutil
    from pathlib import Path
    from domain.providers import (
        AIProviderConnection, Capability, Workspace, VerificationStatus, ProviderMode,
        AIInvocation, InvocationStatus
    )
    from domain.contracts import Task, TaskRun, Status, ContentType
    from service.execution import now
    from persistence.connection import ConnectionFactory, PersistenceError
    from persistence.migration_runner import migrate
    from persistence.repository import SQLiteStore

    # 1. Create tmp migrations dir with only 0001-0003
    migrations_src = tmp_path / 'migrations_src'
    migrations_src.mkdir()
    for name in ('0001_initial.sql', '0002_workspace_provider.sql', '0003_retry_idempotency.sql'):
        shutil.copy(f'persistence/migrations/{name}', migrations_src / name)

    # 2. Create DB and migrate to v3
    db_path = tmp_path / 'app.sqlite3'
    factory = ConnectionFactory(db_path, busy_timeout_ms=50)
    migrate(factory, migrations_src)

    # 3. Create OPENAI data at v3
    store = SQLiteStore(factory)
    now_ts = now()

    ws = Workspace(
        workspace_id='test-ws', workspace_key='test', name='Test',
        created_at=now_ts, updated_at=now_ts
    )
    conn = AIProviderConnection(
        provider_connection_id='conn-openai',
        workspace_id='test-ws',
        provider_type='OPENAI',
        provider_mode=ProviderMode.PLATFORM_MANAGED,
        capabilities=[Capability.TEXT, Capability.IMAGE, Capability.VISUAL_QUALITY],
        default_model='gpt-4',
        verification_status=VerificationStatus.VERIFIED,
        non_secret_configuration={},
        credential_reference='env:OPENAI_API_KEY',
        configuration_version=1,
        created_at=now_ts,
        updated_at=now_ts
    )
    task = Task(
        task_id='task-openai', workspace_id='test-ws', submission_key='key-openai',
        site_id='site-openai', content_type=ContentType.POST, topic='主題',
        brief='需求', brand_profile_id='brand',
        created_at=now_ts, updated_at=now_ts,
        request_snapshot={}, approval_policy_snapshot={'mode': 'REQUIRE_HUMAN_REVIEW'}
    )
    run = TaskRun(
        run_id='run-openai', task_id='task-openai', attempt=1,
        created_at=now_ts, updated_at=now_ts,
        run_mode='INITIAL', status=Status.QUEUED,
        owner_id=None, fencing_token=0, workflow_state=None,
        claimed_at=None, started_at=None, heartbeat_at=None, finished_at=None, error=None,
        resumed_from_run_id=None, resumed_from_checkpoint_id=None,
        provider_connection_id='conn-openai', provider_type='OPENAI',
        provider_mode=ProviderMode.PLATFORM_MANAGED.value,
        model='gpt-4', provider_configuration_version=1
    )
    inv = AIInvocation(
        invocation_id='inv-openai',
        workspace_id='test-ws',
        task_id='task-openai',
        run_id='run-openai',
        provider_connection_id='conn-openai',
        capability=Capability.TEXT,
        provider_type='OPENAI',
        model='gpt-4',
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        latency_ms=500,
        estimated_cost_decimal='0.002',
        currency='USD',
        pricing_reference='catalog:openai/text/gpt4',
        status=InvocationStatus.SUCCEEDED,
        classified_error=None,
        created_at=now_ts
    )

    with store.transaction() as repo:
        repo.add(ws)
        repo.add(conn)
        repo.add(task)
        repo.add(run)
        repo.add(inv)

    # 4. Record pre-migration state
    with factory.connection() as conn:
        pre_versions = [r[0] for r in conn.execute(
            'SELECT version FROM schema_migrations ORDER BY version')]
        assert pre_versions == [1, 2, 3]

        pre_conn = conn.execute(
            'SELECT * FROM ai_provider_connections WHERE provider_connection_id=?', ('conn-openai',)
        ).fetchone()
        pre_run = conn.execute(
            'SELECT * FROM task_runs WHERE run_id=?', ('run-openai',)
        ).fetchone()
        pre_inv = conn.execute(
            'SELECT * FROM ai_invocations WHERE invocation_id=?', ('inv-openai',)
        ).fetchone()

        pre_counts = {
            'connections': conn.execute('SELECT COUNT(*) FROM ai_provider_connections').fetchone()[0],
            'runs': conn.execute('SELECT COUNT(*) FROM task_runs').fetchone()[0],
            'invocations': conn.execute('SELECT COUNT(*) FROM ai_invocations').fetchone()[0],
        }

        # Capture named indexes/triggers
        pre_objects = conn.execute("""
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE tbl_name IN ('ai_provider_connections', 'task_runs', 'ai_invocations')
              AND type IN ('index', 'trigger')
              AND name NOT LIKE 'sqlite_autoindex%'
            ORDER BY type, tbl_name, name
        """).fetchall()
        pre_indexes = {(r['type'], r['name'], r['tbl_name'], r['sql']) for r in pre_objects}

        # Verify v3 schema constraint (no GROQ)
        ai_conn_sql = conn.execute("""
            SELECT sql FROM sqlite_master WHERE type='table' AND name='ai_provider_connections'
        """).fetchone()['sql']
        assert 'GROQ' not in ai_conn_sql, 'v3 should not allow GROQ'

    # 5. Create failing 0004 - real 0004 + guaranteed failure statement
    real_0004 = Path('persistence/migrations/0004_groq_provider.sql').read_text()
    failing_0004 = real_0004 + '\nINSERT INTO absent_table_should_not_exist VALUES (1);\n'
    (migrations_src / '0004_groq_provider.sql').write_text(failing_0004)

    # 6. Execute migrate - should fail with PersistenceError
    with pytest.raises(PersistenceError):
        migrate(factory, migrations_src)

    # 7. Verify complete rollback
    with factory.connection() as conn:
        # Schema versions still [1,2,3]
        post_versions = [r[0] for r in conn.execute(
            'SELECT version FROM schema_migrations ORDER BY version')]
        assert post_versions == [1, 2, 3], f'Schema versions changed: {post_versions}'

        # Tables still exist
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('ai_provider_connections', 'task_runs', 'ai_invocations')")]
        assert set(tables) == {'ai_provider_connections', 'task_runs', 'ai_invocations'}

        # No intermediate tables left
        intermediate = conn.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND (name LIKE '%_new' OR name LIKE '%_old' OR name LIKE '%_d1')
        """).fetchall()
        assert len(intermediate) == 0, f'Intermediate tables found: {[r[0] for r in intermediate]}'

        # Data unchanged
        post_conn = conn.execute(
            'SELECT * FROM ai_provider_connections WHERE provider_connection_id=?', ('conn-openai',)
        ).fetchone()
        post_run = conn.execute(
            'SELECT * FROM task_runs WHERE run_id=?', ('run-openai',)
        ).fetchone()
        post_inv = conn.execute(
            'SELECT * FROM ai_invocations WHERE invocation_id=?', ('inv-openai',)
        ).fetchone()

        assert pre_conn == post_conn, 'ai_provider_connections data changed after failed migration'
        assert pre_run == post_run, 'task_runs data changed after failed migration'
        assert pre_inv == post_inv, 'ai_invocations data changed after failed migration'

        post_counts = {
            'connections': conn.execute('SELECT COUNT(*) FROM ai_provider_connections').fetchone()[0],
            'runs': conn.execute('SELECT COUNT(*) FROM task_runs').fetchone()[0],
            'invocations': conn.execute('SELECT COUNT(*) FROM ai_invocations').fetchone()[0],
        }
        assert pre_counts == post_counts, 'Row counts changed after failed migration'

        # Named indexes/triggers preserved
        post_objects = conn.execute("""
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE tbl_name IN ('ai_provider_connections', 'task_runs', 'ai_invocations')
              AND type IN ('index', 'trigger')
              AND name NOT LIKE 'sqlite_autoindex%'
            ORDER BY type, tbl_name, name
        """).fetchall()
        post_indexes = {(r['type'], r['name'], r['tbl_name'], r['sql']) for r in post_objects}
        missing = pre_indexes - post_indexes
        assert not missing, f'Missing indexes/triggers after rollback: {missing}'

        # FK integrity
        fk_check = conn.execute('PRAGMA foreign_key_check').fetchall()
        assert len(fk_check) == 0, f'FK violations: {fk_check}'

        # Schema still v3 (no GROQ)
        ai_conn_sql = conn.execute("""
            SELECT sql FROM sqlite_master WHERE type='table' AND name='ai_provider_connections'
        """).fetchone()['sql']
        assert 'GROQ' not in ai_conn_sql, 'GROQ should not be in v3 schema after rollback'

    # 8. Replace with real 0004 and retry
    shutil.copy('persistence/migrations/0004_groq_provider.sql', migrations_src / '0004_groq_provider.sql')
    migrate(factory, migrations_src)

    # 9. Verify retry success
    with factory.connection() as conn:
        retry_versions = [r[0] for r in conn.execute(
            'SELECT version FROM schema_migrations ORDER BY version')]
        assert retry_versions == [1, 2, 3, 4], f'Retry schema versions: {retry_versions}'

        # Data still intact
        retry_conn = conn.execute(
            'SELECT * FROM ai_provider_connections WHERE provider_connection_id=?', ('conn-openai',)
        ).fetchone()
        retry_run = conn.execute(
            'SELECT * FROM task_runs WHERE run_id=?', ('run-openai',)
        ).fetchone()
        retry_inv = conn.execute(
            'SELECT * FROM ai_invocations WHERE invocation_id=?', ('inv-openai',)
        ).fetchone()

        assert pre_conn == retry_conn, 'Data changed after retry'
        assert pre_run == retry_run, 'Data changed after retry'
        assert pre_inv == retry_inv, 'Data changed after retry'

        # GROQ now allowed
        ai_conn_sql = conn.execute("""
            SELECT sql FROM sqlite_master WHERE type='table' AND name='ai_provider_connections'
        """).fetchone()['sql']
        assert 'GROQ' in ai_conn_sql, 'GROQ should be allowed after retry'

        # Can now insert GROQ
        conn.execute("""
            INSERT INTO ai_provider_connections (
                provider_connection_id, workspace_id, provider_type, provider_mode,
                capabilities, default_model, verification_status, non_secret_configuration,
                credential_reference, configuration_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            'conn-groq', 'test-ws', 'GROQ', 'PLATFORM_MANAGED',
            '["TEXT"]', 'qwen/qwen3.8-27b', 'VERIFIED', '{}',
            'env:GROQ_API_KEY', 1, now_ts, now_ts
        ))
