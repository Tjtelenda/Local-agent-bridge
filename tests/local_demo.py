"""Loopback-only smoke: real relay, real bridge protocol, synthetic adapter.

Runs the existing relay smoke as well. No real adapters, tokens, or files from
outside this checkout are used. The temporary relay process is always stopped.
"""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading

import httpx
import websockets

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bridge'))
import bridge


async def demo(base):
    calls = []
    release = threading.Event()
    class Synthetic:
        def execute(self, action, payload):
            calls.append((action['name'], payload))
            return {'synthetic': True}
    cfg = bridge.BridgeConfig(base.replace('http', 'ws') + '/v1/bridge', 'unused',
                              agents={'demo': {}},
                              actions={('demo', 'write'): {'name': 'write', 'read_only': False},
                                       ('demo', 'read'): {'name': 'read', 'read_only': True}})
    original = bridge.local_approval
    def approve(request, deadline):
        assert not calls
        assert request['input'] == {'synthetic': 1}
        return release.wait(5)
    bridge.local_approval = approve
    async with httpx.AsyncClient(base_url=base, trust_env=False) as client:
        async with websockets.connect(cfg.relay_url) as ws:
            code = await bridge._await_code(ws)
            paired = (await client.post('/v1/pair', json={'code': code})).json()
            assert json.loads(await ws.recv())['pairing_token'] == paired['pairing_token']
        headers = {'Authorization': 'Bearer ' + paired['api_key']}
        worker = asyncio.create_task(bridge._run_with_auth(cfg, paired['pairing_token'], {'demo': Synthetic()}))
        try:
            for _ in range(100):
                if (await client.get('/v1/agents', headers=headers)).json()['agents']:
                    break
                await asyncio.sleep(.02)
            response = await client.post('/v1/tasks', headers=headers,
                json={'agent': 'demo', 'action': 'write', 'input': {'synthetic': 1}, 'approved': True})
            task = response.json()
            assert task['status'] == 'pending_approval', task
            assert calls == []
            print('PASS: real bridge reports pending; no adapter execution')
            release.set()
            for _ in range(100):
                result = (await client.get('/v1/tasks/' + task['id'], headers=headers)).json()
                if result.get('status') == 'completed':
                    break
                await asyncio.sleep(.02)
            assert result['result'] == {'synthetic': True}
            assert calls == [('write', {'synthetic': 1})]
            print('PASS: local approval executes exactly the reviewed synthetic task')
            bridge.local_approval = lambda *_: False
            denied = (await client.post('/v1/tasks', headers=headers,
                json={'agent': 'demo', 'action': 'write', 'input': {'synthetic': 2}})).json()
            for _ in range(100):
                result = (await client.get('/v1/tasks/' + denied['id'], headers=headers)).json()
                if result.get('status') == 'denied':
                    break
                await asyncio.sleep(.02)
            assert result['status'] == 'denied'
            assert len(calls) == 1
            read = (await client.post('/v1/tasks', headers=headers,
                json={'agent': 'demo', 'action': 'read', 'input': {}})).json()
            assert read['ok'] and len(calls) == 2
            print('PASS: denial does not execute; explicit read-only still works')
            release.clear()
            bridge.local_approval = lambda *_: release.wait(5)
            pending = (await client.post('/v1/tasks', headers=headers,
                json={'agent': 'demo', 'action': 'write', 'input': {'synthetic': 3}})).json()
            assert pending['status'] == 'pending_approval'
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
            release.set()
            for _ in range(100):
                if cfg.executor.seen[pending['id']][1]['status'] == 'denied':
                    break
                await asyncio.sleep(.02)
            assert cfg.executor.seen[pending['id']][1]['status'] == 'denied'
            assert len(calls) == 2
            print('PASS: cancelled connection cannot execute a later local approval')
        finally:
            release.set()
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
            bridge.local_approval = original


async def main():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    env = {**os.environ, 'TASK_TTL_SECONDS': '3', 'RELAY_BASE': base}
    process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app:app',
        '--host', '127.0.0.1', '--port', str(port), '--no-access-log'],
        cwd=ROOT / 'relay', env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            for _ in range(100):
                try:
                    if (await client.get(base + '/healthz')).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(.05)
            else:
                raise RuntimeError('temporary relay did not start')
        await demo(base)
        smoke = await asyncio.to_thread(subprocess.run,
            [sys.executable, str(ROOT / 'relay/test_bridge_smoke.py')], env=env, check=True)
        print('PASS: local end-to-end demo and existing relay smoke completed')
    finally:
        process.terminate()
        await asyncio.to_thread(process.wait, timeout=10)


if __name__ == '__main__':
    asyncio.run(main())
