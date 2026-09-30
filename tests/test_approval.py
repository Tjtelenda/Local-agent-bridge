import copy
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bridge'))
from bridge import BridgeConfig, BridgeError, TaskExecutor, execute_task, local_approval


class HarmlessAdapter:
    def __init__(self):
        self.calls = []

    def execute(self, action, payload):
        self.calls.append((copy.deepcopy(action), copy.deepcopy(payload)))
        return {'synthetic': payload}


def setup_task():
    cfg = BridgeConfig('ws://127.0.0.1', 'unused',
                       agents={'demo': {}},
                       actions={('demo', 'write'): {'name': 'write', 'read_only': False},
                                ('demo', 'read'): {'name': 'read', 'read_only': True}})
    adapter = HarmlessAdapter()
    msg = {'id': 'test-1', 'agent': 'demo', 'action': 'write', 'input': {'value': 1}}
    return cfg, adapter, msg


@pytest.mark.parametrize('flags', [{}, {'approved': True}, {'confirmation': True},
                                  {'requires_confirmation': False}, {'read_only': True}])
def test_remote_flags_never_approve(flags):
    cfg, adapter, msg = setup_task()
    result = execute_task(cfg, {'demo': adapter}, {**msg, **flags})
    assert result['status'] == 'denied'
    assert adapter.calls == []


def test_review_before_execute_and_snapshot_bound():
    cfg, adapter, msg = setup_task()
    pending = []
    def approve(snapshot, deadline):
        assert adapter.calls == []
        assert pending[0]['status'] == 'pending_approval'
        assert snapshot['input'] == {'value': 1}
        assert snapshot['local_action']['read_only'] is False
        msg['input']['value'] = 99
        snapshot['input']['value'] = 88
        snapshot['local_action']['name'] = 'other'
        return True
    result = execute_task(cfg, {'demo': adapter}, msg, approve, pending.append)
    assert result['status'] == 'completed'
    assert adapter.calls == [({'name': 'write', 'read_only': False}, {'value': 1})]


@pytest.mark.parametrize('decision', [False, None, 'yes', 1])
def test_denial_is_terminal(decision):
    cfg, adapter, msg = setup_task()
    result = execute_task(cfg, {'demo': adapter}, msg, lambda *_: decision)
    assert result['status'] == 'denied'
    assert execute_task(cfg, {'demo': adapter}, msg, lambda *_: True) == result
    assert not adapter.calls


def test_replay_and_substitution():
    cfg, adapter, msg = setup_task()
    first = execute_task(cfg, {'demo': adapter}, msg, lambda *_: True)
    assert execute_task(cfg, {'demo': adapter}, msg, lambda *_: True) == first
    for altered in ({'input': {'value': 2}}, {'action': 'read'}, {'agent': 'other'}):
        assert execute_task(cfg, {'demo': adapter}, {**msg, **altered}, lambda *_: True)['status'] == 'rejected'
    assert len(adapter.calls) == 1


def test_expiry_and_exception_fail_closed():
    cfg, adapter, msg = setup_task()
    with patch('bridge.time.monotonic', side_effect=[0, 121]):
        assert execute_task(cfg, {'demo': adapter}, msg, lambda *_: True)['status'] == 'expired'
    msg['id'] = 'exception'
    def broken(*_):
        raise RuntimeError('no terminal')
    assert execute_task(cfg, {'demo': adapter}, msg, broken)['status'] == 'denied'
    assert not adapter.calls


def test_read_only_and_scoped_auto_approve():
    cfg, adapter, msg = setup_task()
    msg['action'] = 'read'
    assert execute_task(cfg, {'demo': adapter}, msg)['ok']
    msg.update(id='second', action='write')
    cfg.auto_approve = {'other/write'}
    assert execute_task(cfg, {'demo': adapter}, msg)['status'] == 'denied'
    msg['id'] = 'third'
    cfg.auto_approve = {'demo/write'}
    assert execute_task(cfg, {'demo': adapter}, msg)['ok']
    assert len(adapter.calls) == 2


@pytest.mark.parametrize('change', [{'id': ''}, {'id': []}, {'input': []},
                                   {'agent': []}, {'action': 'missing'}])
def test_invalid_requests(change):
    cfg, adapter, msg = setup_task()
    assert execute_task(cfg, {'demo': adapter}, {**msg, **change}, lambda *_: True)['status'] == 'rejected'
    assert not adapter.calls


def test_failure_never_retries():
    cfg, adapter, msg = setup_task()
    def fail(action, payload):
        adapter.calls.append('attempt')
        raise RuntimeError('synthetic')
    adapter.execute = fail
    assert execute_task(cfg, {'demo': adapter}, msg, lambda *_: True)['status'] == 'failed'
    execute_task(cfg, {'demo': adapter}, msg, lambda *_: True)
    assert adapter.calls == ['attempt']


def test_capacity_fails_closed():
    cfg, adapter, msg = setup_task()
    with patch('bridge.MAX_TASKS', 0):
        assert execute_task(cfg, {'demo': adapter}, msg, lambda *_: True)['status'] == 'rejected'
    assert not adapter.calls


def test_headless_denies():
    with patch('bridge.sys.stdin.isatty', return_value=False):
        assert local_approval({'id': 'x'}, 999999) is False


@pytest.mark.parametrize('text', [
    'relay_url: ws://localhost\nadapters: {}',
    'relay_url: ws://localhost\nauto_approve: [write]',
    'relay_url: ws://localhost\nagents: [{name: demo, type: command, actions: [{name: write, read_only: "false"}]}]',
])
def test_bad_config_rejected(tmp_path, text):
    file = tmp_path / 'config.yaml'
    file.write_text(text)
    with pytest.raises(BridgeError):
        BridgeConfig.load(str(file))


def test_example_config_loads():
    BridgeConfig.load(str(ROOT / 'bridge/config.example.yaml'))


def test_concurrent_duplicate_executes_once():
    import concurrent.futures
    import threading
    cfg, adapter, msg = setup_task()
    entered, release = threading.Event(), threading.Event()
    def approve(*_):
        entered.set()
        return release.wait(2)
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        first = pool.submit(execute_task, cfg, {'demo': adapter}, msg, approve)
        assert entered.wait(2)
        second = pool.submit(execute_task, cfg, {'demo': adapter}, msg, approve)
        release.set()
        assert first.result() == second.result()
    assert len(adapter.calls) == 1


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows console implementation')
@pytest.mark.parametrize('answer, expected', [('approve x\r', True), ('deny\r', False)])
def test_windows_terminal_decision(answer, expected):
    with patch('bridge.sys.stdin.isatty', return_value=True), \
         patch('bridge.time.monotonic', return_value=0), \
         patch('bridge.time.sleep'), \
         patch('msvcrt.kbhit', return_value=True), \
         patch('msvcrt.getwch', side_effect=list(answer)):
        assert local_approval({'id': 'x', 'input': {}}, 120) is expected
