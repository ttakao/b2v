import json
from unittest.mock import Mock
import pytest
from b2v import services


def test_pid_reuse_never_signals(tmp_path,monkeypatch):
    monkeypatch.setattr(services,'RUN',tmp_path)
    (tmp_path/'b2v.json').write_text(json.dumps({'pid':123,'identity':'old','url':'http://127.0.0.1:8600'}))
    monkeypatch.setattr(services,'identity',lambda pid:'different')
    kill=Mock();monkeypatch.setattr(services.os,'kill',kill)
    services.stop_one('b2v')
    kill.assert_not_called()
    assert not (tmp_path/'b2v.json').exists()


def test_stop_order_and_wait(tmp_path,monkeypatch):
    monkeypatch.setattr(services,'RUN',tmp_path/'run');monkeypatch.setattr(services,'LOG',tmp_path/'logs')
    order=[]
    monkeypatch.setattr(services,'stop_one',lambda name:order.append(name))
    monkeypatch.setattr(services,'listening',lambda url:False)
    services.main('stop')
    assert order==['b2v','llm','stylebert']


def test_external_b2v_preserves_dependencies(tmp_path,monkeypatch):
    monkeypatch.setattr(services,'RUN',tmp_path/'run');monkeypatch.setattr(services,'LOG',tmp_path/'logs')
    order=[];monkeypatch.setattr(services,'stop_one',lambda name:order.append(name))
    monkeypatch.setattr(services,'listening',lambda url:True)
    with pytest.raises(RuntimeError,match='管理対象外'):services.main('stop')
    assert order==['b2v']


def test_external_service_not_adopted(tmp_path,monkeypatch):
    monkeypatch.setattr(services,'RUN',tmp_path)
    monkeypatch.setattr(services,'listening',lambda url:True)
    monkeypatch.setattr(services,'ready',lambda *args:True)
    popen=Mock();monkeypatch.setattr(services.subprocess,'Popen',popen)
    services.start_one('api','b2v')
    popen.assert_not_called()
    assert not (tmp_path/'b2v.json').exists()


def test_real_process_start_and_stop(tmp_path,monkeypatch):
    import sys
    monkeypatch.setattr(services,'RUN',tmp_path)
    monkeypatch.setattr(services,'LOG',tmp_path)
    monkeypatch.setattr(services,'listening',lambda url:False)
    monkeypatch.setattr(services,'command',lambda service:[sys.executable,'-c','import time; time.sleep(60)'])
    monkeypatch.setattr(services,'ready',lambda *args:True)
    services.start_one('api','b2v')
    record=services.read_record('b2v')
    assert services.owned(record)
    services.stop_one('b2v')
    assert not services.owned(record)


def test_failed_start_not_reported_ready(tmp_path,monkeypatch):
    import sys
    monkeypatch.setattr(services,'RUN',tmp_path);monkeypatch.setattr(services,'LOG',tmp_path)
    monkeypatch.setattr(services,'listening',lambda url:False)
    monkeypatch.setattr(services,'command',lambda service:[sys.executable,'-c','raise SystemExit(1)'])
    monkeypatch.setattr(services,'ready',lambda *args:False)
    with pytest.raises(RuntimeError,match='起動失敗'):services.start_one('api','b2v')
