"""D1 schema upgrade, non-secret contracts, and append-only invocation storage."""
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import uuid4, UUID
import hashlib
import shutil
import sqlite3

import pytest
from domain.contracts import Task, TaskRun, TaskEvent, ContentVersion
from domain.providers import (Workspace, WorkspaceStatus, AIProviderConnection, AIInvocation,
    ProviderMode, Capability, InvocationStatus, ProviderError, VerificationStatus, ContractError,
    DEFAULT_WORKSPACE_ID)
from persistence.codec import encode_record, decode_record, record_to_mapping, encode_snapshot
from persistence.connection import ConnectionFactory, ConstraintViolation, PersistenceError
from persistence.migration_runner import migrate
from persistence.repository import SQLiteStore, SQLiteRepository, JSON_FIELDS
from test_persistence_contracts import records

MIGRATIONS=Path(__file__).resolve().parents[1]/'persistence'/'migrations'
NOW='2026-09-18T00:00:00Z'
SECRET='test-secret-that-must-never-be-persisted'

@pytest.fixture
def store(tmp_path):
    factory=ConnectionFactory(tmp_path/'d1.sqlite3')
    migrate(factory)
    return SQLiteStore(factory)

def workspace(key='other'):
    return Workspace(workspace_id=str(uuid4()),workspace_key=key,name='繁體中文工作區',
        created_at=NOW,updated_at=NOW)

def connection(workspace_id=DEFAULT_WORKSPACE_ID):
    return AIProviderConnection(provider_connection_id=str(uuid4()),workspace_id=workspace_id,
        provider_type='OPENAI',provider_mode=ProviderMode.WORKSPACE_BYOK,
        capabilities=list(Capability),default_model='gpt-4',credential_reference='env:TEST_PROVIDER_KEY',
        configuration_version=1,created_at=NOW,updated_at=NOW,
        non_secret_configuration={'temperature':0.3,'options':{'max_tokens':2000,'response_format':'json_object'}})

def seed(store):
    items=records()
    conn=connection()
    with store.transaction() as repo:
        for item in (*items,conn): repo.add(item)
    call=AIInvocation(invocation_id=str(uuid4()),workspace_id=DEFAULT_WORKSPACE_ID,
        task_id=items[0].task_id,run_id=items[1].run_id,provider_connection_id=conn.provider_connection_id,
        capability=Capability.TEXT,provider_type='OPENAI',model='gpt-4',
        status=InvocationStatus.SUCCEEDED,created_at=NOW)
    return items,conn,call

def test_fresh_migration_and_idempotent_seed(store):
    migrate(store.factory)
    with store.factory.connection() as conn:
        assert [r[0] for r in conn.execute('SELECT version FROM schema_migrations ORDER BY version')]==[1,2]
        rows=conn.execute("SELECT * FROM workspaces WHERE workspace_key='default'").fetchall()
        assert len(rows)==1 and UUID(rows[0]['workspace_id']).version==4
        assert rows[0]['workspace_id']==DEFAULT_WORKSPACE_ID
        assert conn.execute('SELECT count(*) FROM ai_provider_connections').fetchone()[0]==1
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        assert conn.execute('PRAGMA foreign_keys').fetchone()[0]==1
    with store.reader() as repo:
        assert repo.default_workspace().workspace_key=='default'

def old_database(tmp_path):
    directory=tmp_path/'migration_copy'
    directory.mkdir()
    shutil.copyfile(MIGRATIONS/'0001_initial.sql',directory/'0001_initial.sql')
    factory=ConnectionFactory(tmp_path/'old.sqlite3')
    migrate(factory,directory)
    items=records()
    tables=('tasks','task_runs','task_events','content_versions')
    with factory.transaction() as conn:
        for item,table in zip(items,tables):
            data=record_to_mapping(item)
            columns={r[1] for r in conn.execute('PRAGMA table_info('+table+')')}
            data={k:v for k,v in data.items() if k in columns}
            values=[encode_snapshot(v) if k in JSON_FIELDS and v is not None else v for k,v in data.items()]
            conn.execute(f"INSERT INTO {table} ({','.join(data)}) VALUES ({','.join('?' for _ in data)})",values)
        conn.execute('UPDATE tasks SET current_run_id=?,latest_content_version_id=? WHERE task_id=?',
            (items[1].run_id,items[3].content_version_id,items[0].task_id))
    return factory,directory,items

def test_upgrade_preserves_all_records_and_backfills(tmp_path):
    factory,directory,items=old_database(tmp_path)
    before={}
    with factory.connection() as conn:
        for table in ('tasks','task_runs','task_events','content_versions'):
            before[table]=[dict(r) for r in conn.execute('SELECT * FROM '+table)]
    shutil.copyfile(MIGRATIONS/'0002_workspace_provider.sql',directory/'0002_workspace_provider.sql')
    migrate(factory,directory)
    migrate(factory,directory)
    with factory.connection() as conn:
        for table,rows in before.items():
            after=[dict(r) for r in conn.execute('SELECT * FROM '+table)]
            assert len(after)==len(rows)
            for old,new in zip(rows,after):
                assert {k:new[k] for k in old}==old
        assert conn.execute('SELECT workspace_id FROM tasks').fetchone()[0]==DEFAULT_WORKSPACE_ID
        assert all(conn.execute('SELECT '+name+' FROM task_runs').fetchone()[0] is None
            for name in ('provider_connection_id','provider_type','provider_mode','model','provider_configuration_version'))
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        assert conn.execute('PRAGMA foreign_keys').fetchone()[0]==1
    with SQLiteStore(factory).reader() as repo:
        assert repo.get(TaskEvent,items[2].event_id)==items[2]
        assert repo.get(ContentVersion,items[3].content_version_id)==items[3]

def test_failed_upgrade_rolls_back_rebuild(tmp_path):
    factory,directory,items=old_database(tmp_path)
    sql=(MIGRATIONS/'0002_workspace_provider.sql').read_text(encoding='utf-8')
    (directory/'0002_workspace_provider.sql').write_text(sql+'INSERT INTO absent VALUES (1);',encoding='utf-8')
    with pytest.raises(PersistenceError): migrate(factory,directory)
    with factory.connection() as conn:
        assert 'workspace_id' not in [r[1] for r in conn.execute('PRAGMA table_info(tasks)')]
        assert conn.execute('SELECT task_id FROM tasks').fetchone()[0]==items[0].task_id
        assert conn.execute('SELECT count(*) FROM content_versions').fetchone()[0]==1
        assert [r[0] for r in conn.execute('SELECT version FROM schema_migrations')]==[1]
        assert conn.execute('PRAGMA foreign_keys').fetchone()[0]==1

@pytest.mark.parametrize('bad_workspace',[None,'missing-workspace'])
def test_workspace_not_null_and_fk(store,bad_workspace):
    with pytest.raises(ConstraintViolation):
        with store.factory.transaction() as conn:
            # Exercise database constraints directly, not only typed contracts.
            data=record_to_mapping(records()[0])
            data['workspace_id']=bad_workspace
            values=[encode_snapshot(v) if k in JSON_FIELDS and v is not None else v for k,v in data.items()]
            conn.execute(f"INSERT INTO tasks ({','.join(data)}) VALUES ({','.join('?' for _ in data)})",values)

def test_submission_key_unique_within_workspace(store):
    other=workspace()
    task=records()[0]
    second=replace(task,task_id='second',workspace_id=other.workspace_id)
    with store.transaction() as repo:
        repo.add(other)
        repo.add(task)
        repo.add(second)
    with store.reader() as repo:
        assert repo.find_by_submission_key(task.submission_key)==task
        assert repo.find_by_submission_key(task.submission_key,workspace_id=other.workspace_id)==second
    with pytest.raises(ConstraintViolation):
        with store.transaction() as repo: repo.add(replace(second,task_id='third'))

def test_new_contracts_round_trip(store):
    other=workspace()
    provider=connection(other.workspace_id)
    with store.transaction() as repo:
        repo.add(other)
        repo.add(provider)
    with store.reader() as repo:
        assert repo.get(Workspace,other.workspace_id)==other
        assert repo.get(AIProviderConnection,provider.provider_connection_id)==provider
    for item in (other,provider): assert decode_record(type(item),encode_record(item))==item
    items,provider,call=seed(store)
    with store.transaction() as repo: repo.add(call)
    with store.reader() as repo:
        assert repo.get(AIInvocation,call.invocation_id)==call
        assert repo.invocations(call.run_id)==[call]
    assert decode_record(AIInvocation,encode_record(call))==call
    assert call.input_tokens is None and call.output_tokens is None and call.total_tokens is None
    assert call.latency_ms is None and call.estimated_cost_decimal is None

def test_task_run_snapshot_round_trip(store):
    task,run,_,_=records()
    provider=connection()
    run=replace(run,provider_connection_id=provider.provider_connection_id,provider_type='OPENAI',
        provider_mode='WORKSPACE_BYOK',model='gpt-4',provider_configuration_version=1)
    with store.transaction() as repo:
        for item in (task,provider,run): repo.add(item)
    with store.reader() as repo: assert repo.get(TaskRun,run.run_id)==run
    assert decode_record(TaskRun,encode_record(run))==run

@pytest.mark.parametrize('field',['input_tokens','output_tokens','total_tokens','latency_ms'])
@pytest.mark.parametrize('value',[-1,1.5,True])
def test_invalid_usage_rejected(store,field,value):
    _,_,call=seed(store)
    with pytest.raises(ContractError): replace(call,**{field:value})

@pytest.mark.parametrize('field',['input_tokens','output_tokens','total_tokens','latency_ms'])
def test_negative_usage_database_constraint(store,field):
    _,_,call=seed(store)
    data=record_to_mapping(call)
    data[field]=-1
    with pytest.raises(ConstraintViolation):
        with store.factory.transaction() as conn:
            conn.execute(f"INSERT INTO ai_invocations ({','.join(data)}) VALUES ({','.join('?' for _ in data)})",list(data.values()))

@pytest.mark.parametrize('cost',['0','0.000000000000000000001234567890','12345678901234567890.12345678901234567890'])
def test_decimal_exact_round_trip(store,cost):
    _,_,call=seed(store)
    call=replace(call,input_tokens=0,output_tokens=1,total_tokens=1,latency_ms=0,
        estimated_cost_decimal=cost,currency='USD',pricing_reference='catalog:openai/2026-09-18')
    with store.transaction() as repo: repo.add(call)
    with store.reader() as repo: saved=repo.get(AIInvocation,call.invocation_id)
    assert saved==call and saved.estimated_cost_decimal==cost
    assert Decimal(saved.estimated_cost_decimal)==Decimal(cost)
    assert decode_record(AIInvocation,encode_record(call))==call

@pytest.mark.parametrize('values',[
    {'estimated_cost_decimal':0.1,'currency':'USD','pricing_reference':'catalog:test'},
    {'estimated_cost_decimal':'-1','currency':'USD','pricing_reference':'catalog:test'},
    {'estimated_cost_decimal':'NaN','currency':'USD','pricing_reference':'catalog:test'},
    {'estimated_cost_decimal':'1.2','currency':None,'pricing_reference':'catalog:test'},
    {'estimated_cost_decimal':'1.2','currency':'USD','pricing_reference':None},
    {'estimated_cost_decimal':'1e-8','currency':'USD','pricing_reference':'catalog:test'},
])
def test_invalid_cost_rejected(store,values):
    _,_,call=seed(store)
    with pytest.raises(ContractError): replace(call,**values)

def test_invocations_append_only(store):
    _,_,call=seed(store)
    with store.transaction() as repo: repo.add(call)
    for sql in ('UPDATE ai_invocations SET model=\'other\'','DELETE FROM ai_invocations'):
        with pytest.raises(ConstraintViolation):
            with store.factory.transaction() as conn: conn.execute(sql)
    assert not hasattr(SQLiteRepository,'update_invocation')
    assert not hasattr(SQLiteRepository,'delete_invocation')
    assert not hasattr(SQLiteRepository,'update') and not hasattr(SQLiteRepository,'delete')

def test_transaction_rollback_new_records(store):
    other=workspace()
    provider=connection(other.workspace_id)
    task,run,_,_=records()
    task=replace(task,workspace_id=other.workspace_id)
    call=AIInvocation(invocation_id=str(uuid4()),workspace_id=other.workspace_id,task_id=task.task_id,
        run_id=run.run_id,provider_connection_id=provider.provider_connection_id,capability=Capability.TEXT,
        provider_type='OPENAI',model='gpt-4',status=InvocationStatus.SUCCEEDED,created_at=NOW)
    with pytest.raises(RuntimeError):
        with store.transaction() as repo:
            for item in (other,provider,task,run,call): repo.add(item)
            raise RuntimeError('rollback')
    with store.reader() as repo:
        assert repo.get(Workspace,other.workspace_id) is None
        assert repo.get(AIProviderConnection,provider.provider_connection_id) is None
        assert repo.get(AIInvocation,call.invocation_id) is None

@pytest.mark.parametrize('field,value',[
    ('non_secret_configuration',{'api_key':SECRET}),
    ('non_secret_configuration',{'options':{'Authorization':SECRET}}),
    ('non_secret_configuration',{'response_format':SECRET}),
    ('credential_reference',SECRET),
])
def test_secrets_rejected_before_database(store,field,value):
    with pytest.raises(ContractError) as caught:
        with store.transaction() as repo: repo.add(replace(connection(),**{field:value}))
    assert SECRET not in str(caught.value)
    with store.factory.connection() as conn:
        assert SECRET not in '\n'.join(conn.iterdump())

def test_classified_error_is_enum_not_external_message(store):
    _,_,call=seed(store)
    with pytest.raises(ContractError) as caught:
        replace(call,status=InvocationStatus.FAILED,classified_error=SECRET)
    assert SECRET not in str(caught.value)
    call=replace(call,status=InvocationStatus.FAILED,classified_error=ProviderError.RATE_LIMIT)
    with store.transaction() as repo: repo.add(call)
    with store.reader() as repo: assert repo.get(AIInvocation,call.invocation_id)==call
    with store.factory.connection() as conn: assert SECRET not in '\n'.join(conn.iterdump())

def test_configuration_crud_excludes_invocations(store):
    other=workspace()
    provider=connection(other.workspace_id)
    with store.transaction() as repo:
        repo.add(other)
        repo.add(provider)
        repo.update_workspace(replace(other,name='新名稱'))
        repo.update_provider_connection(replace(provider,default_model='gpt-4o',configuration_version=2))
    with store.reader() as repo:
        assert repo.get(Workspace,other.workspace_id).name=='新名稱'
        assert repo.get(AIProviderConnection,provider.provider_connection_id).configuration_version==2
    with store.transaction() as repo:
        repo.delete_provider_connection(provider.provider_connection_id)
        repo.delete_workspace(other.workspace_id)
    with store.reader() as repo: assert repo.get(Workspace,other.workspace_id) is None


@pytest.mark.parametrize('statement', ['PRAGMA quick_check', 'PRAGMA QuIcK_ChEcK(task_runs)'])
def test_authorizer_quick_check_read_only(store, statement):
    from persistence.migration_runner import _authorize
    with store.factory.connection() as conn:
        before = conn.total_changes
        conn.set_authorizer(_authorize)
        assert [row[0] for row in conn.execute(statement)] == ['ok']
        assert conn.total_changes == before


@pytest.mark.parametrize('name', ['quick_check', 'QUICK_CHECK', ' Quick_Check '])
def test_authorizer_normalizes_only_quick_check(name):
    from persistence.migration_runner import _authorize
    assert _authorize(sqlite3.SQLITE_PRAGMA, name, 'task_runs', 'main', None) == sqlite3.SQLITE_OK


@pytest.mark.parametrize('statement', [
    'PRAGMA writable_schema=ON', 'PRAGMA foreign_keys=OFF', 'PRAGMA journal_mode=DELETE',
    'PRAGMA user_version=42', 'PRAGMA quick_check_extra', 'COMMIT',
    'SAVEPOINT forbidden', "ATTACH DATABASE ':memory:' AS forbidden", 'DETACH DATABASE forbidden',
])
def test_authorizer_other_operations_still_rejected(store, statement):
    from persistence.migration_runner import _authorize
    with pytest.raises(PersistenceError):
        with store.factory.connection() as conn:
            conn.set_authorizer(_authorize)
            conn.execute(statement)


def test_authorizer_forbidden_action_codes_unchanged():
    from persistence.migration_runner import _authorize
    for action in (sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT,
                   sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
        assert _authorize(action, 'quick_check', None, None, None) == sqlite3.SQLITE_DENY
    assert _authorize(sqlite3.SQLITE_PRAGMA, 'writable_schema', 'ON', None, None) == sqlite3.SQLITE_DENY
