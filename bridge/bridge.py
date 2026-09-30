#!/usr/bin/env python3
"""Local Agent Bridge daemon.

The home machine ALWAYS dials out: this daemon opens a single persistent
OUTBOUND WebSocket to the relay URL, authenticates with a pairing token,
receives tasks, executes them against configured local adapters, and pushes
events back up the socket. No inbound ports. No port forwarding.

Usage:
    python3 bridge.py --config ./config.yaml

Env vars:
    BRIDGE_CONFIG   path to YAML config (overridden by --config)
    HASS_TOKEN      Home Assistant long-lived token (adapter type 'homeassistant')

The config file is read from the path given by --config / BRIDGE_CONFIG,
default ./config.yaml. NEVER put secrets in config.example.yaml.
"""

from __future__ import annotations

import hashlib
import copy
import time
import threading
import argparse
import asyncio
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    print("missing dependency: pyyaml (pip install -r requirements.txt)",
          file=sys.stderr)
    sys.exit(2)

try:
    import websockets
except ImportError:  # pragma: no cover
    print("missing dependency: websockets (pip install -r requirements.txt)",
          file=sys.stderr)
    sys.exit(2)

LOG = logging.getLogger("local-agent-bridge")

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class BridgeError(Exception):
    """A task failure reported back to the relay as ok:false."""


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------

# Shared HTTP POST helper (urllib, no extra deps).
def _http_post(url: str, payload: dict, headers: dict | None = None,
               timeout: int = 30) -> Any:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise BridgeError(f"HTTP {e.code} from adapter: {e.read()[:200].decode('utf-8', 'replace')}")
    except (urllib.error.URLError, OSError) as e:
        raise BridgeError(f"adapter unreachable: {e}")
    try:
        return json.loads(body)
    except ValueError:
        return {"raw": body}


def _http_get(url: str, headers: dict | None = None,
              timeout: int = 30) -> Any:
    req = urllib.request.Request(url, method="GET", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise BridgeError(f"HTTP {e.code} from event source")
    except (urllib.error.URLError, OSError) as e:
        raise BridgeError(f"event source unreachable: {e}")
    try:
        return json.loads(body)
    except ValueError:
        return {"raw": body}


class BaseAdapter:
    def execute(self, action: dict, task_input: dict) -> Any:
        raise NotImplementedError

    def poll(self) -> Any:
        """For event sources: return current state, or None if not pollable."""
        return None


class HermesAdapter(BaseAdapter):
    """HTTP POST to a local Hermes API URL. Body: {action, input}."""

    def __init__(self, cfg: dict):
        self.url = cfg["url"]

    def execute(self, action: dict, task_input: dict) -> Any:
        return _http_post(self.url, {"action": action["name"],
                                     "input": task_input})


class OpenClawAdapter(BaseAdapter):
    """HTTP gateway URL for OpenClaw. Body: {action, input}."""

    def __init__(self, cfg: dict):
        self.url = cfg["url"]

    def execute(self, action: dict, task_input: dict) -> Any:
        return _http_post(self.url, {"action": action["name"],
                                     "input": task_input})


class HomeAssistantAdapter(BaseAdapter):
    """Home Assistant REST API. Token from HASS_TOKEN env or config.

    Actions: 'call_service' (input: domain, service, entity_id?, data?),
    'get_state' (input: entity_id). Can also poll state for events.
    """

    def __init__(self, cfg: dict):
        self.base = cfg["base_url"].rstrip("/")
        self.token = os.environ.get("HASS_TOKEN") or cfg.get("token")
        if not self.token:
            raise BridgeError("homeassistant adapter needs HASS_TOKEN env "
                              "or 'token' in config")
        self._headers = {"Authorization": f"Bearer {self.token}",
                         "Content-Type": "application/json"}
        self.poll_entity = cfg.get("poll_entity")  # optional default

    def execute(self, action: dict, task_input: dict) -> Any:
        name = action["name"]
        if name == "get_state":
            entity = task_input.get("entity_id") or self.poll_entity
            if not entity:
                raise BridgeError("get_state needs input.entity_id")
            return _http_get(f"{self.base}/api/states/{entity}",
                             headers=self._headers)
        if name == "call_service":
            domain = task_input["domain"]
            service = task_input["service"]
            payload: dict = {}
            if task_input.get("entity_id"):
                payload["entity_id"] = task_input["entity_id"]
            if isinstance(task_input.get("data"), dict):
                payload.update(task_input["data"])
            return _http_post(f"{self.base}/api/services/{domain}/{service}",
                              payload, headers=self._headers)
        raise BridgeError(f"unknown homeassistant action: {name}")

    def poll(self) -> Any:
        if self.poll_entity:
            return _http_get(f"{self.base}/api/states/{self.poll_entity}",
                             headers=self._headers)
        return None


class WebhookAdapter(BaseAdapter):
    """Generic HTTP POST to a URL. Body: {action, input}."""

    def __init__(self, cfg: dict):
        self.url = cfg["url"]

    def execute(self, action: dict, task_input: dict) -> Any:
        return _http_post(self.url, {"action": action["name"],
                                     "input": task_input})

    def poll(self) -> Any:
        return _http_get(self.url)


class CommandAdapter(BaseAdapter):
    """Shell exec restricted to an explicit allowlist in config.

    Action config declares e.g.:
        actions:
          - name: disk_usage
            command: "df -h /"          # exact string must be in allowlist
    Any resolved command not on the allowlist is refused. Arguments come
    from the action declaration only, never from task input.
    """

    def __init__(self, cfg: dict):
        self.allowlist: set[str] = set(cfg.get("allowlist", []))

    def execute(self, action: dict, task_input: dict) -> Any:
        cmd = action.get("command")
        if not cmd:
            raise BridgeError("command adapter action needs 'command'")
        if cmd not in self.allowlist:
            raise BridgeError(f"refused: command not on allowlist: {cmd}")
        argv = shlex.split(cmd)
        if not shutil.which(argv[0]):
            raise BridgeError(f"refused: binary not found: {argv[0]}")
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=60, check=False)
        except subprocess.TimeoutExpired:
            raise BridgeError("command timed out")
        return {"returncode": proc.returncode, "stdout": proc.stdout,
                "stderr": proc.stderr}


ADAPTERS = {
    "hermes": HermesAdapter,
    "openclaw": OpenClawAdapter,
    "homeassistant": HomeAssistantAdapter,
    "webhook": WebhookAdapter,
    "command": CommandAdapter,
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class BridgeConfig:
    relay_url: str
    token_file: str
    agents: dict[str, dict] = field(default_factory=dict)
    actions: dict[tuple[str, str], dict] = field(default_factory=dict)
    auto_approve: set[str] = field(default_factory=set)
    events: list[dict] = field(default_factory=list)
    executor: Any = field(default_factory=lambda: TaskExecutor(), repr=False)

    @classmethod
    def load(cls, path: str) -> "BridgeConfig":
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        if "adapters" in raw or "actions" in raw:
            raise BridgeError("use agents with nested actions; legacy config is unsupported")
        relay_url = raw.get("relay_url", "")
        if not relay_url:
            raise BridgeError(f"config {path}: 'relay_url' is required")

        token_file = os.path.expanduser(
            raw.get("token_file", "~/.local-agent-bridge/token"))

        agents: dict[str, dict] = {}
        actions: dict[tuple[str, str], dict] = {}
        auto_approve: set[str] = set(raw.get("auto_approve", []) or [])

        for agent in raw.get("agents", []) or []:
            name = agent["name"]
            if not isinstance(name, str) or not name or "/" in name:
                raise BridgeError("agent names must be nonempty strings without slashes")
            atype = agent["type"]
            if atype not in ADAPTERS:
                raise BridgeError(f"unknown adapter type '{atype}' for agent '{name}'")
            if name in agents:
                raise BridgeError("duplicate agent name")
            agents[name] = agent
            for act in agent.get("actions", []) or []:
                aname = act["name"]
                if not isinstance(aname, str) or not aname or "/" in aname:
                    raise BridgeError("action names must be nonempty strings without slashes")
                if type(act.get("read_only")) is not bool:
                    raise BridgeError(
                        f"agent '{name}' action '{aname}': "
                        "'read_only: true|false' is required")
                if (name, aname) in actions:
                    raise BridgeError("duplicate action name")
                actions[(name, aname)] = act

        known = {f"{agent}/{action}" for agent, action in actions}
        if not auto_approve <= known:
            raise BridgeError("auto_approve entries must be known agent/action pairs")
        events = raw.get("events", []) or []
        for ev in events:
            if not {"agent", "event", "interval"}.issubset(ev):
                raise BridgeError(f"event rule missing keys: {ev}")
        return cls(relay_url=relay_url, token_file=token_file,
                   agents=agents, actions=actions, auto_approve=auto_approve,
                   events=events)


# ---------------------------------------------------------------------------
# Pairing (device flow over the relay WebSocket)
#
# First run: open an anonymous socket, ask for a code, print it. The user
# enters the code in Muse; Muse calls POST /v1/pair; the relay pushes the
# pairing token back down this same socket. The token is stored mode 600
# and used as a WebSocket subprotocol on every later connection.
# ---------------------------------------------------------------------------

async def _await_code(ws) -> str:
    await ws.send(json.dumps({"type": "request_code"}))
    raw = await asyncio.wait_for(ws.recv(), timeout=30)
    try:
        msg = json.loads(raw)
    except ValueError:
        raise BridgeError("non-JSON response to code request")
    if msg.get("type") == "code_issued" and msg.get("code"):
        return msg["code"]
    raise BridgeError(f"unexpected pairing response: {msg.get('type')!r}")


def _store_token(cfg: BridgeConfig, token: str) -> None:
    path = Path(cfg.token_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    os.chmod(path, 0o600)


async def ensure_paired(cfg: BridgeConfig) -> str:
    """Return a pairing token, running the device flow on first run."""
    if os.path.exists(cfg.token_file):
        with open(cfg.token_file, encoding="utf-8") as f:
            token = f.read().strip()
        if token:
            return token

    print("No pairing token found; starting device-flow pairing.", flush=True)
    while True:  # re-request a code if the pairing socket drops
        try:
            async with websockets.connect(cfg.relay_url) as ws:
                code = await _await_code(ws)
                # The code is printed for the user to see; it is never
                # written to the log.
                print(f"\nYour pairing code is: {code}\n", flush=True)
                print("POST /v1/pair on your relay with "
                      "this code. The bridge picks up the pairing "
                      "automatically.\n", flush=True)
                LOG.info("waiting for pairing to complete")
                try:
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), timeout=600)
                        try:
                            msg = json.loads(raw)
                        except ValueError:
                            continue  # ignore non-JSON while waiting
                        if (msg.get("type") == "paired"
                                and msg.get("pairing_token")):
                            token = msg["pairing_token"]
                            _store_token(cfg, token)
                            LOG.info("paired; token stored at %s (mode 600)",
                                     cfg.token_file)
                            return token
                        # Ignore anything else while waiting for pairing.
                except asyncio.TimeoutError:
                    raise BridgeError("pairing timed out after 10 minutes")
        except BridgeError:
            raise
        except Exception as e:
            LOG.warning("pairing socket dropped (%s); requesting a new code",
                        type(e).__name__)
            await asyncio.sleep(2)


# ---------------------------------------------------------------------------
# Task execution
# ---------------------------------------------------------------------------

APPROVAL_SECONDS = 120
MAX_TASKS = 10000


def local_approval(request: dict, deadline: float) -> bool:
    """Trusted local console only; never accepts a decision from the relay."""
    if not sys.stdin.isatty():
        return False
    # JSON escaping prevents terminal-control characters in untrusted input.
    print("\nLOCAL APPROVAL (task data will cross the relay):\n" +
          json.dumps(request, ensure_ascii=True, sort_keys=True), flush=True)
    expected = "approve " + request["id"]
    print(f"Type {expected!r} within {APPROVAL_SECONDS}s; anything else denies:",
          flush=True)
    if os.name == "nt":
        import msvcrt
        chars = []
        while time.monotonic() < deadline:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\r", "\n"):
                    print()
                    return "".join(chars) == expected
                if ch == "\x03":
                    return False
                if ch == "\b":
                    if chars:
                        chars.pop()
                        print("\b \b", end="", flush=True)
                elif ch.isprintable():
                    chars.append(ch)
                    print(ch, end="", flush=True)
            time.sleep(0.05)
        return False
    import select
    ready, _, _ = select.select([sys.stdin], [], [], max(0, deadline-time.monotonic()))
    return bool(ready) and sys.stdin.readline().strip() == expected


class TaskExecutor:
    """Process-lifetime deduplication; no remote approval capability exists.

    Snapshot the request and local action before review. Consume the task ID
    before calling the adapter, including when execution fails. At capacity,
    reject new tasks rather than evict replay protection.
    """
    def __init__(self):
        self.seen = {}
        self.lock = threading.Lock()

    def execute(self, cfg, adapters, msg, approve=None, notify=None):
        with self.lock:
            return self._execute(cfg, adapters, msg, approve, notify)

    def _execute(self, cfg, adapters, msg, approve, notify):
        tid = msg.get("id")
        def reply(status, **kw):
            return {"id": tid, "ok": status == "completed", "status": status,
                    "requires_confirmation": status == "pending_approval", **kw}
        if not isinstance(tid, str) or not tid or len(tid) > 128:
            return reply("rejected", error="invalid task id")
        agent, name = msg.get("agent"), msg.get("action")
        payload = msg.get("input")
        if not isinstance(agent, str) or not isinstance(name, str):
            return reply("rejected", error="invalid agent or action")
        if payload is not None and not isinstance(payload, dict):
            return reply("rejected", error="input must be an object")
        request = copy.deepcopy({"id": tid, "agent": agent, "action": name,
                                 "input": payload if payload is not None else {}})
        try:
            fingerprint = hashlib.sha256(json.dumps(
                request, sort_keys=True, allow_nan=False).encode()).hexdigest()
        except (TypeError, ValueError):
            return reply("rejected", error="input must be valid JSON")
        if tid in self.seen:
            previous, result = self.seen[tid]
            if previous != fingerprint:
                return reply("rejected", error="task id already bound to another request")
            return copy.deepcopy(result)
        if len(self.seen) >= MAX_TASKS:
            return reply("rejected", error="task capacity reached; local restart required")
        action = copy.deepcopy(cfg.actions.get((agent, name)))
        if action is None or agent not in adapters:
            return reply("rejected", error="unknown agent or action")
        if type(action.get("read_only")) is not bool:
            return reply("rejected", error="invalid read_only policy")
        needs = not action["read_only"] and f"{agent}/{name}" not in cfg.auto_approve
        result = reply("denied", error="local approval required")
        self.seen[tid] = (fingerprint, result)
        if needs:
            deadline = time.monotonic() + APPROVAL_SECONDS
            if notify:
                notify(reply("pending_approval", error="awaiting local terminal approval"))
            try:
                approved = approve(copy.deepcopy({**request, "local_action": action}), deadline) if approve else False
            except Exception:
                approved = False
            if time.monotonic() >= deadline:
                result = reply("expired", error="local approval expired")
            elif approved is not True:
                result = reply("denied", error="local approval denied or unavailable")
            else:
                result = None
            if result is not None:
                self.seen[tid] = (fingerprint, result)
                return copy.deepcopy(result)
        # Mark consumed before side effects. An error never permits an automatic retry.
        result = reply("failed", error="execution interrupted; outcome unknown")
        self.seen[tid] = (fingerprint, result)
        try:
            value = adapters[agent].execute(action, request["input"])
            result = reply("completed", result=value)
        except Exception as exc:
            result = reply("failed", error=f"adapter error: {type(exc).__name__}")
        self.seen[tid] = (fingerprint, copy.deepcopy(result))
        return result


def execute_task(cfg, adapter_instances, msg, approve=None, notify=None):
    if cfg.executor is None:
        cfg.executor = TaskExecutor()
    return cfg.executor.execute(cfg, adapter_instances, msg, approve, notify)


# ---------------------------------------------------------------------------
# Events: poll local sources, push {type:"event", ...} on change
# ---------------------------------------------------------------------------

async def event_loop(cfg: BridgeConfig,
                     adapter_instances: dict[str, BaseAdapter],
                     send) -> None:
    """Poll each configured event source on its interval; on change matching
    the rule's `match` spec, push {type:"event", agent, event, payload}."""
    if not cfg.events:
        return
    last: dict[int, Any] = {}
    while True:
        for i, rule in enumerate(cfg.events):
            agent_name = rule["agent"]
            adapter = adapter_instances.get(agent_name)
            if adapter is None:
                continue
            try:
                state = adapter.poll()
            except BridgeError:
                continue
            prev = last.get(i, None)
            last[i] = state
            if prev is None:
                continue  # first sample: establish baseline, no event
            if state != prev and _match(rule.get("match"), state, prev):
                await send({"type": "push_event", "agent": agent_name,
                            "event": rule["event"], "payload": _summarize(state)})
                LOG.info("event pushed: agent=%s event=%s",
                         agent_name, rule["event"])
        # sleep at the smallest configured interval
        interval = min(r["interval"] for r in cfg.events)
        await asyncio.sleep(interval)


def _match(match: dict | None, state: Any, prev: Any) -> bool:
    """Rule match: {"key": <dotted path>, "from": X, "to": Y} optionally.
    Without a match spec, any change counts."""
    if not match:
        return True
    key = match.get("key")
    if key:
        cur = _dig(state, key)
        old = _dig(prev, key)
        if "from" in match and old != match["from"]:
            return False
        if "to" in match and cur != match["to"]:
            return False
        return cur != old
    return True


def _dig(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _summarize(state: Any) -> Any:
    """Metadata-only payloads for events: never push raw payload contents."""
    if isinstance(state, dict):
        return {"changed_keys": sorted(state.keys())}
    return {"type": type(state).__name__}


# ---------------------------------------------------------------------------
# WebSocket client: outbound-only, exponential backoff reconnect
# ---------------------------------------------------------------------------

async def run(cfg: BridgeConfig, token: str) -> None:
    adapter_instances: dict[str, BaseAdapter] = {}
    for name, agent_cfg in cfg.agents.items():
        try:
            adapter_instances[name] = ADAPTERS[agent_cfg["type"]](agent_cfg)
        except BridgeError as e:
            LOG.error("agent '%s' disabled: %s", name, e)

    # Reconnect loop: the relay never dials in; we always dial out.
    backoff = 1.0
    while True:
        try:
            await _run_with_auth(cfg, token, adapter_instances)
            backoff = 1.0
        except BridgeError as e:
            LOG.error("fatal: %s", e)
            raise SystemExit(1)
        except Exception as e:
            LOG.warning("connection lost (%s); reconnecting in %.0fs",
                        type(e).__name__, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)


def _agent_registrations(cfg: BridgeConfig) -> list[dict]:
    """Capability list for the relay: name, read_only, description only.

    read_only is true when every action on the agent is read-only. Never
    includes URLs, tokens, or config contents.
    """
    regs = []
    for name, agent in cfg.agents.items():
        acts = [cfg.actions[k] for k in cfg.actions if k[0] == name]
        regs.append({
            "name": name,
            "read_only": all(bool(a.get("read_only", True)) for a in acts),
            "description": agent.get("description", ""),
        })
    return regs


async def _run_with_auth(cfg: BridgeConfig, token: str,
                         adapter_instances: dict[str, BaseAdapter]) -> None:
    # Token travels as a WebSocket subprotocol (keeps it out of URLs and
    # access logs), per the relay's documented auth. Wrapped so the token
    # itself never reaches the log.
    async with websockets.connect(cfg.relay_url, subprotocols=[token]) as ws:
        await ws.send(json.dumps({"type": "register_agents",
                                  "agents": _agent_registrations(cfg)}))
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
            ack = json.loads(raw)
            if ack.get("type") != "agents_registered":
                LOG.warning("unexpected registration ack: %r", ack.get("type"))
        except (asyncio.TimeoutError, ValueError):
            LOG.warning("no registration ack from relay; continuing")
        LOG.info("websocket connected to relay")
        send_lock = asyncio.Lock()

        async def send(msg: dict) -> None:
            async with send_lock:
                await ws.send(json.dumps(msg))

        ev_task = asyncio.create_task(event_loop(cfg, adapter_instances, send))
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    LOG.warning("ignoring non-JSON frame")
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("type") == "task":
                    loop = asyncio.get_running_loop()
                    def notify(reply):
                        asyncio.run_coroutine_threadsafe(
                            send({"type": "task_reply", **reply}), loop).result()
                    cancelled = threading.Event()
                    def approve(request, deadline):
                        decision = local_approval(request, deadline)
                        return (decision is True and not cancelled.is_set()
                                and ws.state.name == "OPEN")
                    try:
                        reply = await asyncio.to_thread(
                            execute_task, cfg, adapter_instances, msg, approve, notify)
                    finally:
                        cancelled.set()
                    LOG.info("task: agent=%s action=%s ok=%s",
                             msg.get("agent"), msg.get("action"), reply["ok"])
                    await send({"type": "task_reply", **reply})
                elif msg.get("type") == "ping":
                    await send({"type": "pong"})
        finally:
            ev_task.cancel()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Local Agent Bridge daemon")
    parser.add_argument("--config", default=os.environ.get("BRIDGE_CONFIG",
                                                           "./config.yaml"))
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg = BridgeConfig.load(args.config)
    LOG.info("config loaded: %d agents, %d actions, %d event rules",
             len(cfg.agents), len(cfg.actions), len(cfg.events))
    asyncio.run(_amain(cfg))


async def _amain(cfg: BridgeConfig) -> None:
    token = await ensure_paired(cfg)
    await run(cfg, token)


if __name__ == "__main__":
    main()
