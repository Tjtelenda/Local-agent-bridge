import concurrent.futures
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'relay'))
import app as relay


def pair(client):
    with client.websocket_connect('/v1/bridge') as ws:
        ws.send_json({'type': 'request_code'})
        code = ws.receive_json()['code']
        response = client.post('/v1/pair', json={'code': code})
        credentials = response.json()
        assert ws.receive_json()['pairing_token'] == credentials['pairing_token']
        assert client.post('/v1/pair', json={'code': code}).status_code == 410
        return credentials


def test_pair_dispatch_owner_binding_and_terminal_reply():
    with TestClient(relay.app) as client:
        owner, other = pair(client), pair(client)
        auth = {'Authorization': 'Bearer ' + owner['api_key']}
        other_auth = {'Authorization': 'Bearer ' + other['api_key']}
        with client.websocket_connect('/v1/bridge', subprotocols=[owner['pairing_token']]) as ws:
            ws.send_json({'type': 'register_agents', 'agents': [{'name': 'demo', 'config': 'SECRET'}]})
            assert ws.receive_json()['count'] == 1
            assert 'SECRET' not in client.get('/v1/agents', headers=auth).text
            with concurrent.futures.ThreadPoolExecutor() as pool:
                task = pool.submit(client.post, '/v1/tasks', headers=auth,
                                   json={'agent': 'demo', 'action': 'write', 'input': {}})
                incoming = ws.receive_json()
                tid = incoming['id']
                forged = {'type': 'task_reply', 'id': tid, 'ok': True, 'result': 'forged'}
                for subprotocols in ([], [other['pairing_token']]):
                    with client.websocket_connect('/v1/bridge', subprotocols=subprotocols) as rogue:
                        rogue.send_json(forged)
                        rogue.send_json({'type': 'unknown'})
                        assert rogue.receive_json()['type'] == 'error'  # barrier
                        assert relay.task_results[tid]['status'] == 'dispatched'
                ws.send_json({'type': 'task_reply', 'id': tid, 'ok': False,
                              'status': 'pending_approval', 'requires_confirmation': True})
                assert task.result(timeout=5).json()['status'] == 'pending_approval'
            assert client.get('/v1/tasks/' + tid, headers=other_auth).status_code == 404
            ws.send_json({'type': 'task_reply', 'id': tid, 'ok': True,
                          'status': 'completed', 'result': {'synthetic': True}})
            ws.send_json({'type': 'unknown'})
            ws.receive_json()
            final = client.get('/v1/tasks/' + tid, headers=auth).json()
            assert final['result'] == {'synthetic': True}
            ws.send_json(forged)
            ws.send_json({'type': 'unknown'})
            ws.receive_json()
            assert client.get('/v1/tasks/' + tid, headers=auth).json() == final
            relay.task_results[tid]['expires'] = 0
            assert client.get('/v1/tasks/' + tid, headers=auth).status_code == 404
        assert client.post('/v1/tasks', headers=auth,
                           json={'agent': 'demo', 'action': 'read'}).status_code == 503
