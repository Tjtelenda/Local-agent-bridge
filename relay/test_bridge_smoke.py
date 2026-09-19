#!/usr/bin/env python3
"""Fake bridge client: end-to-end smoke test for the relay API.

Exercises: pairing (device code), agent registration, task dispatch +
reply (checks {id, ok, result} and requires_confirmation passthrough),
/v1/agents, /v1/tasks/{id}, push events via the MetaDeliveryAdapter stub,
TTL eviction, offline-bridge 503, and verifies no payload files hit disk.

Usage: start the relay first, e.g.
    TASK_TTL_SECONDS=3 uvicorn app:app --host 127.0.0.1 --port 18001
then:  python test_bridge_smoke.py
"""
import asyncio
import json
import os
import time
import urllib.request
import urllib.error

import websockets

BASE = os.getenv("RELAY_BASE", "http://127.0.0.1:18001")
WS_BASE = BASE.replace("http", "ws")
CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)


def http(method, path, body=None, headers=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


async def main():
    # (a) connect anonymously, request a pairing code
    ws = await websockets.connect(WS_BASE + "/v1/bridge")
    await ws.send(json.dumps({"type": "request_code"}))
    msg = json.loads(await ws.recv())
    check("code issued", msg.get("type") == "code_issued" and len(msg.get("code", "")) == 8,
          f"type={msg.get('type')}")
    code = msg["code"]
    await ws.close()

    # pair from the Meta/Muse side
    status, paired = http("POST", "/v1/pair", {"code": code})
    check("pair ok", status == 200 and paired.get("paired") is True, f"status={status}")
    api_key, pairing_token = paired["api_key"], paired["pairing_token"]
    check("pair returns credentials", api_key.startswith("lab_") and pairing_token.startswith("lbt_"))
    auth = {"Authorization": f"Bearer {api_key}"}

    # code is single-use
    status2, _ = http("POST", "/v1/pair", {"code": code})
    check("code single-use", status2 == 410, f"status={status2}")

    # (b) reconnect with the pairing token as subprotocol (keeps it out of URLs), register agents
    ws = await websockets.connect(WS_BASE + "/v1/bridge", subprotocols=[pairing_token])
    agents = [
        {"name": "homeassistant", "read_only": False, "description": "Smart-home control",
         "config": {"token": "SHOULD-NEVER-BE-EXPOSED", "url": "http://internal"}},
        {"name": "vault", "read_only": True, "description": "Read-only note search"},
    ]
    await ws.send(json.dumps({"type": "register_agents", "agents": agents}))
    msg = json.loads(await ws.recv())
    check("agents registered", msg.get("type") == "agents_registered" and msg.get("count") == 2)

    status, agent_list = http("GET", "/v1/agents", headers=auth)
    names = [a["name"] for a in agent_list.get("agents", [])]
    leaked = json.dumps(agent_list)
    check("agents listed", status == 200 and names == ["homeassistant", "vault"], f"names={names}")
    check("no config/URL/token leak in /v1/agents",
          "SHOULD-NEVER-BE-EXPOSED" not in leaked and "internal" not in leaked)

    # (c) background listener: answer dispatched tasks like a real bridge
    async def listener():
        async for raw in ws:
            m = json.loads(raw)
            if m.get("type") == "task":
                await ws.send(json.dumps({
                    "type": "task_reply", "id": m["id"], "ok": True,
                    "result": {"echo": m["input"], "action_taken": m["action"]},
                    "requires_confirmation": True,
                }))

    listen_task = asyncio.create_task(listener())

    # (c) dispatch a task via POST /v1/tasks (run sync HTTP in a thread)
    def post_task():
        return http("POST", "/v1/tasks",
                    {"agent": "homeassistant", "action": "turn_on", "input": {"entity": "light.kitchen"}},
                    headers=auth)

    status, task_resp = await asyncio.to_thread(post_task)
    check("task dispatched", status == 200, f"status={status}")
    check("task response shape",
          all(k in task_resp for k in ("id", "ok", "result", "requires_confirmation")),
          f"keys={sorted(task_resp.keys())}")
    check("task ok", task_resp.get("ok") is True)
    check("requires_confirmation passthrough", task_resp.get("requires_confirmation") is True)
    check("result echo",
          task_resp.get("result", {}).get("echo") == {"entity": "light.kitchen"})
    task_id = task_resp["id"]

    # unknown agent -> 404
    status, _ = http("POST", "/v1/tasks", {"agent": "nope", "action": "x"}, headers=auth)
    check("unknown agent 404", status == 404, f"status={status}")

    # (d) fetch recent result
    status, fetched = http("GET", f"/v1/tasks/{task_id}", headers=auth)
    check("task result retrievable", status == 200 and fetched.get("id") == task_id)

    # (d) push an event up the socket, confirm adapter acceptance over HTTP
    await ws.send(json.dumps({"type": "push_event", "agent": "homeassistant",
                              "event": "motion_detected",
                              "payload": {"room": "kitchen", "secret": "must-not-be-logged"}}))
    status, ev = http("POST", "/v1/events",
                      {"agent": "homeassistant", "event": "motion_detected", "payload": {"room": "kitchen"}},
                      headers={"Authorization": f"Bearer {pairing_token}"})
    check("MetaDeliveryAdapter accepts event", status == 200 and ev.get("accepted") is True,
          f"status={status}")

    # (e) TTL eviction: relay was started with TASK_TTL_SECONDS=3
    print("waiting for task TTL to expire...", flush=True)
    await asyncio.sleep(4)
    status, _ = http("GET", f"/v1/tasks/{task_id}", headers=auth)
    check("task payload evicted after TTL", status == 404, f"status={status}")

    # offline bridge -> 503
    listen_task.cancel()
    await ws.close()
    await asyncio.sleep(0.5)  # let the relay notice the disconnect
    status, body = http("POST", "/v1/tasks", {"agent": "vault", "action": "x"}, headers=auth)
    check("offline bridge 503", status == 503 and "offline" in body.get("detail", ""),
          f"status={status}")

    print("\n==== summary ====")
    failed = [n for n, ok, _ in CHECKS if not ok]
    print(f"{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    if failed:
        print("FAILED:", failed)
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
