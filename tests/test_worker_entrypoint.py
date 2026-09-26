"""Phase 8.1-8A Worker CLI and production-bootstrap contracts."""
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

from service.worker_bootstrap import WorkerBootstrapError, build_worker, run_worker


ROOT = Path(__file__).resolve().parents[1]


def test_help_is_side_effect_free(tmp_path):
    database = tmp_path / "help-must-not-create.sqlite3"
    profiles = tmp_path / "missing-profiles.json"
    environment = os.environ.copy()
    environment.update({"AIWF_DATABASE": str(database), "AIWF_PROFILES_FILE": str(profiles)})

    result = subprocess.run(
        [sys.executable, "-m", "worker", "--help"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0
    assert "--once" in result.stdout
    assert not database.exists()


def test_cli_is_a_thin_argument_and_exit_code_adapter(monkeypatch):
    from worker import __main__ as cli

    worker = object()
    build = Mock(return_value=worker)
    run = Mock(return_value=0)
    monkeypatch.setattr(cli, "build_worker", build)
    monkeypatch.setattr(cli, "run_worker", run)
    monkeypatch.setattr(sys, "argv", ["python -m worker", "--once", "--poll-seconds", "2"])

    assert cli.main() == 0
    assert build.call_args.kwargs["run_once"] is True
    run.assert_called_once_with(worker, poll_seconds=2.0, run_once=True)


@pytest.mark.parametrize("stage, expected", [("build", 1), ("run", 2)])
def test_fatal_startup_and_runtime_errors_return_nonzero_without_leaking_secrets(
    monkeypatch, capsys, stage, expected
):
    from worker import __main__ as cli

    secret = "credential-must-not-leak"
    monkeypatch.setattr(sys, "argv", ["python -m worker", "--once"])
    if stage == "build":
        monkeypatch.setattr(cli, "build_worker", Mock(side_effect=WorkerBootstrapError(secret)))
    else:
        monkeypatch.setattr(cli, "build_worker", Mock(return_value=object()))
        monkeypatch.setattr(cli, "run_worker", Mock(side_effect=WorkerBootstrapError(secret)))

    assert cli.main() == expected
    assert secret not in capsys.readouterr().err


def test_once_executes_exactly_one_worker_iteration():
    worker = Mock()

    assert run_worker(worker, run_once=True) == 0
    worker.run_once.assert_called_once_with()
    worker.run.assert_not_called()


def test_signal_handlers_only_set_the_stop_event():
    stop = Mock()
    worker = Mock()
    handlers = {}

    def capture(sig, handler):
        handlers[sig] = handler

    def run(captured_stop, *, poll_seconds):
        assert captured_stop is stop
        handlers[signal.SIGINT](signal.SIGINT, None)

    worker.run.side_effect = run
    with patch("service.worker_bootstrap.threading.Event", return_value=stop), patch(
        "service.worker_bootstrap.signal.signal", side_effect=capture
    ):
        assert run_worker(worker, poll_seconds=0.25) == 0

    stop.set.assert_called_once_with()


def test_bootstrap_uses_adapter_production_provider_defaults(tmp_path):
    """Provider and credential assembly belongs to FactoryAdapter/ProviderSession."""
    profiles = tmp_path / "profiles.json"
    profiles.write_text(
        '[{"workspace_id":"workspace","site_id":"site",'
        '"brand_profile_id":"brand","snapshot":{}}]',
        encoding="utf-8",
    )
    store = MagicMock()
    repository = MagicMock()
    store.reader.return_value.__enter__.return_value = repository
    repository.default_workspace.return_value = SimpleNamespace(workspace_id="workspace")

    with patch("service.worker_bootstrap.ConnectionFactory"), patch(
        "service.worker_bootstrap.migrate"
    ), patch("service.worker_bootstrap.SQLiteStore", return_value=store), patch(
        "service.worker_bootstrap.FactoryAdapter", return_value=object()
    ) as adapter, patch("service.worker_bootstrap.Worker", return_value=object()):
        build_worker(database_path=str(tmp_path / "worker.sqlite3"), profiles_path=str(profiles))

    assert "provider_factory" not in adapter.call_args.kwargs
    assert "credential_resolver" not in adapter.call_args.kwargs


def test_entrypoint_does_not_compose_publication_or_learning():
    source = (ROOT / "service" / "worker_bootstrap.py").read_text(encoding="utf-8")
    cli_source = (ROOT / "worker" / "__main__.py").read_text(encoding="utf-8")
    combined = source + cli_source

    assert "WordPressPublisher" not in combined
    assert "LearnerAgent" not in combined
    assert "AUTO_PUBLISH" not in combined


def _isolated_process(arguments, tmp_path):
    """Foreground child, bounded wait; subprocess.run kills/reaps on timeout."""
    environment=os.environ.copy()
    for key in list(environment):
        if any(part in key.upper() for part in ('API_KEY','PASSWORD','TOKEN','SECRET','WORDPRESS')):
            environment.pop(key)
    environment['AIWF_DATABASE']=str(tmp_path/'isolated.sqlite3')
    environment['AIWF_PROFILES_FILE']=str(tmp_path/'missing-profiles.json')
    environment['OPENAI_API_KEY']='entrypoint-canary-only'
    return subprocess.run([sys.executable,*arguments],cwd=ROOT,env=environment,
        capture_output=True,text=True,timeout=30)


@pytest.mark.parametrize('target',['import','help'])
def test_import_and_help_do_not_touch_runtime_resources(tmp_path,target):
    script = """
import os,sqlite3,threading,subprocess,runpy,sys
from pathlib import Path
from unittest.mock import patch
original=os.getenv
def getenv(name,*args):
    if any(part in name.upper() for part in ('API_KEY','PASSWORD','TOKEN','SECRET','WORDPRESS')):
        raise AssertionError('Credential access during import/help')
    return original(name,*args)
def forbidden(*args,**kwargs): raise AssertionError('Runtime resource access during import/help')
with patch('os.getenv',side_effect=getenv), patch.object(sqlite3,'connect',side_effect=forbidden), patch.object(Path,'read_text',side_effect=forbidden), patch.object(threading.Thread,'start',side_effect=forbidden), patch.object(subprocess,'Popen',side_effect=forbidden):
    if sys.argv[1]=='help':
        sys.argv=['worker','--help']
        runpy.run_module('worker',run_name='__main__')
    else:
        import worker.__main__
"""
    result=_isolated_process(['-c',script,target],tmp_path)
    assert result.returncode==0,result.stderr
    assert not (tmp_path/'isolated.sqlite3').exists()
    assert 'entrypoint-canary-only' not in result.stdout+result.stderr


def _runtime_paths(tmp_path):
    import json
    from persistence.connection import ConnectionFactory
    from persistence.migration_runner import migrate
    from persistence.repository import SQLiteStore
    factory=ConnectionFactory(tmp_path/'runtime.sqlite3');migrate(factory)
    store=SQLiteStore(factory)
    with store.reader() as repo: workspace=repo.default_workspace()
    profiles=tmp_path/'profiles.json'
    profiles.write_text(json.dumps([{'workspace_id':workspace.workspace_id,'site_id':'site',
        'brand_profile_id':'brand','snapshot':{}}]),encoding='utf-8')
    return store,factory.path,profiles


def test_empty_queue_once_subprocess_exits_without_background_process(tmp_path):
    _,database,profiles=_runtime_paths(tmp_path)
    result=_isolated_process(['-m','worker','--once','--database',str(database),'--profiles',str(profiles),
        '--image-root',str(tmp_path/'images')],tmp_path)
    assert result.returncode==0,result.stderr
    assert 'entrypoint-canary-only' not in result.stdout+result.stderr


def test_unique_owner_ids(tmp_path,monkeypatch):
    _,database,profiles=_runtime_paths(tmp_path)
    monkeypatch.setenv('AIWF_PROFILES_FILE',str(profiles))
    first=build_worker(database_path=str(database),profiles_path=str(profiles))
    second=build_worker(database_path=str(database),profiles_path=str(profiles))
    from uuid import UUID
    assert UUID(first.owner_id)!=UUID(second.owner_id)


def test_queued_task_claim_and_fake_provider_to_awaiting_approval(tmp_path,monkeypatch):
    from test_factory_integration import Harness,PNG,submit,read
    from worker.adapter import FactoryAdapter
    from domain.ai_runtime import TextRequest,TextResult
    from domain.contracts import Status
    store,database,profiles=_runtime_paths(tmp_path)
    monkeypatch.setenv('AIWF_PROFILES_FILE',str(profiles))
    task=submit(store)
    calls=[]
    class Fake:
        def complete(self,request):
            calls.append(request)
            with store.reader() as repo:
                from domain.contracts import TaskRun
                run=repo.get(TaskRun,task.current_run_id)
                assert run.status==Status.RUNNING and run.owner_id
            return TextResult('fake content','OPENAI','fake-model')
    def before(factory,legacy,mocks):
        factory.run_context.providers.text.complete(TextRequest('fake-only'))
    monkeypatch.setattr(Harness,'options',{'before':before})
    monkeypatch.setattr(Harness,'seen',[])
    def adapter(**kwargs):
        kwargs.update(factory_class=Harness,image_downloader=lambda _:PNG,
            provider_factory=lambda *args:Fake(),credential_resolver=Mock(resolve=Mock(return_value='fake-only')))
        return FactoryAdapter(**kwargs)
    with patch('service.worker_bootstrap.FactoryAdapter',side_effect=adapter), \
         patch('service.worker_bootstrap.signal.signal'), \
         patch('openai.OpenAI',side_effect=AssertionError('No external AI')), \
         patch('socket.socket.connect',side_effect=AssertionError('No network')), \
         patch('tools.wordpress.WordPressPublisher',side_effect=AssertionError('No publishing')):
        worker=build_worker(database_path=str(database),profiles_path=str(profiles),image_root=str(tmp_path/'images'))
        assert run_worker(worker,run_once=True)==0
    saved,run,version,events=read(store,task)
    assert saved.status==run.status==Status.AWAITING_APPROVAL
    assert run.owner_id==worker.owner_id and version is not None
    assert len(calls)==1
    with store.reader() as repo: assert len(repo.invocations(run.run_id))==1


@pytest.mark.parametrize('invalid',['database','profiles'])
def test_invalid_runtime_paths_fail_safely_in_subprocess(tmp_path,invalid):
    _,database,profiles=_runtime_paths(tmp_path)
    if invalid=='database':
        database=tmp_path/'private-path-canary';database.mkdir()
    else:
        profiles=tmp_path/'private-path-canary.json'
        profiles.write_text('{invalid',encoding='utf-8')
    result=_isolated_process(['-m','worker','--once','--database',str(database),'--profiles',str(profiles)],tmp_path)
    assert result.returncode!=0
    output=result.stdout+result.stderr
    assert 'Traceback' not in output
    assert 'private-path-canary' not in output and 'entrypoint-canary-only' not in output


@pytest.mark.parametrize('sig',[signal.SIGINT,signal.SIGTERM])
def test_both_shutdown_signals_set_stop_without_executing_extra_work(sig):
    import threading
    stop=threading.Event();handlers={}
    worker=Mock()
    def run(actual_stop,**kwargs):
        assert actual_stop is stop and not stop.is_set()
        handlers[sig](sig,None)
        assert stop.is_set()
    worker.run.side_effect=run
    with patch('service.worker_bootstrap.threading.Event',return_value=stop), \
         patch('service.worker_bootstrap.signal.signal',side_effect=lambda s,h:handlers.update({s:h})):
        assert run_worker(worker)==0
    worker.run_once.assert_not_called()


def test_http_application_never_starts_worker():
    from api.app import create_app
    from worker.loop import Worker
    with patch.object(Worker,'run',side_effect=AssertionError('Implicit Worker')), \
         patch.object(Worker,'run_once',side_effect=AssertionError('Implicit Worker')), \
         patch('threading.Thread.start',side_effect=AssertionError('Background thread')):
        assert create_app(Mock()) is not None
