# ============================================================================
# PRIVACY GUARANTEES — read before reviewing. These are the product, not
# just engineering choices:
#
#  1. Task payloads live in MEMORY ONLY and are evicted after a short TTL
#     (default 5 minutes, configurable via TASK_TTL_SECONDS). Nothing is
#     written to disk: no database, no files, no request/response body logs.
#
#  2. Application logs contain ONLY aggregate counters (tasks relayed, task
#     errors, events by type, pairings completed). They NEVER contain message
#     contents, pairing codes, pairing tokens, API keys, agent configs, URLs,
#     or payloads.
#
#  3. No analytics, no user accounts. Bridges are identified solely by random
#     pairing tokens. Pairing codes are random, single-use, and expire after
#     10 minutes.
#
#  4. The relay never dials in to anyone's home. The local bridge daemon
#     always opens the WebSocket OUTBOUND; the relay only answers on it.
# ============================================================================

import asyncio
import json
import logging
import os
import secrets
import string
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from meta_adapter import MetaDeliveryAdapter

# --- configuration -----------------------------------------------------------
TASK_TIMEOUT_SECONDS = float(os.getenv("TASK_TIMEOUT_SECONDS", "30"))
TASK_TTL_SECONDS = float(os.getenv("TASK_TTL_SECONDS", "300"))   # 5 minutes
CODE_TTL_SECONDS = float(os.getenv("CODE_TTL_SECONDS", "600"))   # 10 minutes
SWEEP_INTERVAL_SECONDS = 60

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # unambiguous, no 0/O/1/I

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("relay")

app = FastAPI(
    title="Local Agent Bridge — Relay API",
    description="Privacy-first cloud relay between Meta's Muse and users' local AI agents.",
    version="0.1.0",
)

meta_adapter = MetaDeliveryAdapter()

# --- in-memory state (never persisted; see privacy guarantees above) ---------

@dataclass
class BridgeState:
    """One paired home bridge."""
    socket: Optional[WebSocket] = None
    agents: List[Dict[str, Any]] = field(default_factory=list)

# pairing_token -> BridgeState
bridges: Dict[str, BridgeState] = {}
# api_key -> pairing_token
api_keys: Dict[str, str] = {}
# pairing code -> {"token": pairing_token, "expires": ts}  (bound at pair time)
pending_codes: Dict[str, Dict[str, Any]] = {}
# task_id -> {"ok","result"/"error","requires_confirmation","expires"}
task_results: Dict[str, Dict[str, Any]] = {}
# task_id -> asyncio.Future waiting on the bridge's reply
pending_tasks: Dict[str, asyncio.Future] = {}
# Task IDs are bound to their paired bridge before dispatch.
task_owners: Dict[str, str] = {}

# --- counters only, never contents -------------------------------------------
counters = {"tasks_relayed": 0, "task_errors": 0, "pairings_completed": 0}


def _new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))


def _new_id(prefix: str) -> str:
    return f"{prefix}_" + secrets.token_urlsafe(12)


def _now() -> float:
    return time.time()


# --- auth --------------------------------------------------------------------

async def require_api_key(authorization: Optional[str] = Header(default=None)) -> str:
    """Bearer API key for the Meta-facing HTTP side.

    NOTE: this is the adapter layer for Meta's connector auth. Key
    issuance/validation is to be finalized against Meta's published
    connector spec; for now keys are issued by POST /v1/pair and stored
    in memory only.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer API key")
    key = authorization[len("Bearer "):].strip()
    if key not in api_keys:
        raise HTTPException(status_code=401, detail="invalid API key")
    return key


def _bridge_for_key(api_key: str) -> Optional[BridgeState]:
    token = api_keys.get(api_key)
    if token is None:
        return None
    return bridges.get(token)


# --- request models ----------------------------------------------------------

class PairRequest(BaseModel):
    code: str = Field(..., min_length=8, max_length=8, description="8-char pairing code")


class TaskRequest(BaseModel):
    agent: str = Field(..., description="Registered agent name")
    action: str = Field(..., description="Action to perform")
    input: Any = Field(default=None, description="Action input payload")


class EventRequest(BaseModel):
    agent: str
    event: str
    payload: Any = None


# --- background eviction -----------------------------------------------------

async def _sweeper():
    """Periodically evict expired codes and task results (in-memory only)."""
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        now = _now()
        for code in [c for c, v in pending_codes.items() if v["expires"] <= now]:
            del pending_codes[code]  # counter only; code value never logged
        for tid in [t for t, v in task_results.items() if v["expires"] <= now]:
            del task_results[tid]
            task_owners.pop(tid, None)


@app.on_event("startup")
async def _startup():
    asyncio.create_task(_sweeper())


# --- HTTP: health ------------------------------------------------------------

@app.get("/healthz", tags=["ops"])
async def healthz():
    return {"status": "ok", "bridges_paired": len(bridges)}


# --- HTTP: pairing -----------------------------------------------------------

@app.post("/v1/pair", tags=["pairing"])
async def pair(req: PairRequest):
    """Exchange an 8-char device code for credentials.

    The local bridge opens an anonymous socket and asks for a code
    (`request_code` -> `code_issued`). The Meta/Muse side presents the code
    here; if valid and unexpired, the relay issues an API key (Meta-facing
    HTTP auth) and a pairing token (bridge socket auth), binds the bridge's
    record to them, and pushes the pairing token back down the socket that
    requested the code so the device flow completes automatically.

    This is the one Meta-facing endpoint that does not require a bearer key —
    the code itself is the credential (device-code flow).
    """
    code = req.code.strip().upper()
    entry = pending_codes.get(code)
    if entry is None or entry["expires"] <= _now():
        pending_codes.pop(code, None)
        raise HTTPException(status_code=410, detail="code invalid or expired")
    # Single-use: consume immediately.
    notify_sock = entry.get("socket")
    del pending_codes[code]

    pairing_token = _new_id("lbt")
    api_key = "lab_" + secrets.token_urlsafe(24)
    bridges[pairing_token] = BridgeState()  # bridge re-attaches via subprotocol
    api_keys[api_key] = pairing_token
    counters["pairings_completed"] += 1
    log.info("pairing completed total=%d", counters["pairings_completed"])
    # Best-effort: hand the pairing token to the bridge over the socket that
    # requested the code, so the device flow completes without manual paste.
    # That socket is the bridge's own outbound TLS connection, so only the
    # bridge sees the token. If the socket is gone, the user falls back to
    # pasting the token manually (see note below).
    if notify_sock is not None:
        try:
            await notify_sock.send_text(json.dumps({
                "type": "paired",
                "pairing_token": pairing_token,
            }))
        except Exception:
            pass
    return {
        "paired": True,
        "pairing_token": pairing_token,
        "api_key": api_key,
        "note": "pairing_token is delivered to your bridge automatically over its pairing socket; if that fails, save it in the configured token_file manually. Use api_key as the bearer key on Meta-facing calls.",
    }


# --- HTTP: agents ------------------------------------------------------------

@app.get("/v1/agents", tags=["agents"])
async def list_agents(api_key: str = Depends(require_api_key)):
    """List the agents/capabilities the caller's paired bridge registered.

    Returns names, read_only flags, and descriptions only — never config
    contents, URLs, or tokens.
    """
    bridge = _bridge_for_key(api_key)
    agents = bridge.agents if bridge else []
    return {"agents": agents}


# --- HTTP: tasks -------------------------------------------------------------

@app.post("/v1/tasks", tags=["tasks"])
async def submit_task(req: TaskRequest, api_key: str = Depends(require_api_key)):
    """Submit a task to a local agent via the caller's paired bridge.

    Forwards over the bridge's socket and waits for its reply (configurable
    timeout). A pending_approval response means no action has executed.
    Approval occurs only at the local bridge terminal; poll GET for completion.
    """
    bridge = _bridge_for_key(api_key)
    if bridge is None or bridge.socket is None:
        raise HTTPException(
            status_code=503,
            detail="paired bridge is offline; the local bridge daemon must be running and connected",
        )
    if not any(a.get("name") == req.agent for a in bridge.agents):
        raise HTTPException(status_code=404, detail=f"unknown agent: {req.agent}")

    task_id = _new_id("tsk")
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    pending_tasks[task_id] = future
    task_owners[task_id] = api_keys[api_key]
    task_results[task_id] = {"id": task_id, "ok": False, "status": "dispatched",
                             "requires_confirmation": True,
                             "expires": _now() + TASK_TTL_SECONDS}
    try:
        await bridge.socket.send_text(json.dumps({
            "type": "task",
            "id": task_id,
            "agent": req.agent,
            "action": req.action,
            "input": req.input,
        }))
    except Exception:
        pending_tasks.pop(task_id, None)
        counters["task_errors"] += 1
        log.info("task send failed count=%d", counters["task_errors"])
        raise HTTPException(status_code=503, detail="paired bridge connection failed")

    try:
        reply = await asyncio.wait_for(asyncio.shield(future), timeout=TASK_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        pending_tasks.pop(task_id, None)
        counters["task_errors"] += 1
        log.info("task timeout count=%d", counters["task_errors"])
        raise HTTPException(status_code=504, detail="bridge did not reply in time")
    finally:
        pending_tasks.pop(task_id, None)

    counters["tasks_relayed"] += 1
    log.info("task relayed total=%d", counters["tasks_relayed"])
    return reply


@app.get("/v1/tasks/{task_id}", tags=["tasks"])
async def get_task(task_id: str, api_key: str = Depends(require_api_key)):
    """Fetch a recent task result. Results expire after the TTL (default 5 min)."""
    entry = task_results.get(task_id)
    if task_owners.get(task_id) != api_keys[api_key]:
        raise HTTPException(status_code=404, detail="task result not found or expired")
    if entry is None or entry["expires"] <= _now():
        task_results.pop(task_id, None)
        task_owners.pop(task_id, None)
        raise HTTPException(status_code=404, detail="task result not found or expired")
    return {k: v for k, v in entry.items() if k != "expires"}


# --- HTTP: events (alternate ingress; primary path is the socket) ------------

def _handle_event(agent: str, event_type: str) -> Dict[str, Any]:
    """Accept a bridge push event and hand it to the Meta delivery adapter.

    Only the event TYPE and agent name reach the adapter — payload contents
    are intentionally dropped here (privacy is the product).
    """
    accepted = meta_adapter.deliver(agent=agent, event_type=event_type)
    return {"accepted": accepted}


@app.post("/v1/events", tags=["events"])
async def post_event(req: EventRequest, authorization: Optional[str] = Header(default=None)):
    """Bridge→Muse proactive push intake (HTTP alternate).

    Accepts the bridge's pairing token OR the API key as bearer. The primary
    path is the `push_event` message over the bridge's WebSocket; this
    endpoint exists for bridges that push over plain HTTPS.
    """
    token = (authorization or "")[len("Bearer "):].strip() if (authorization or "").startswith("Bearer ") else ""
    if token not in bridges and token not in api_keys:
        raise HTTPException(status_code=401, detail="invalid bridge credential")
    return _handle_event(req.agent, req.event)


# --- WebSocket: bridge channel -----------------------------------------------

@app.websocket("/v1/bridge")
async def bridge_channel(websocket: WebSocket):
    """Persistent channel opened OUTBOUND by the local bridge daemon.

    Auth: the pairing token issued at pair time. Preferred: offered as a
    `Sec-WebSocket-Protocol` subprotocol (keeps the token out of URLs and
    therefore out of access logs). Legacy: `?token=<pairing_token>` query
    param (works, but the token can appear in access-log URLs — prefer the
    subprotocol). Omitted only for the initial code-registration handshake.
    """
    offered = websocket.scope.get("subprotocols", []) or []
    token = next((s for s in offered if s.startswith("lbt_")), None)
    if token is None:
        token = websocket.query_params.get("token")  # legacy path
    bridge: Optional[BridgeState] = bridges.get(token) if token else None
    accept_kwargs = {"subprotocol": token} if (bridge is not None and token in offered) else {}
    await websocket.accept(**accept_kwargs)

    if bridge is not None:
        bridge.socket = websocket  # (re)bind socket to this pairing token
        log.info("bridge socket bound bridges_paired=%d", len(bridges))

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({"type": "error", "detail": "invalid JSON"}))
                continue
            if not isinstance(msg, dict):
                continue
            mtype = msg.get("type")

            if mtype == "request_code":
                if bridge is not None:
                    await websocket.send_text(json.dumps({"type": "error", "detail": "already paired"}))
                    continue
                code = _new_code()
                while code in pending_codes:  # avoid collision
                    code = _new_code()
                pending_codes[code] = {
                    "expires": _now() + CODE_TTL_SECONDS,
                    "socket": websocket,  # for pushing the pairing token back
                }
                await websocket.send_text(json.dumps({"type": "code_issued", "code": code}))
                # Note: the code value is sent to the bridge only; never logged.
                # Codes survive the issuing socket closing — they live by TTL
                # (single-use) so a dropped connection doesn't strand the user.
                # On successful POST /v1/pair the token is pushed back down
                # this socket as {"type":"paired","pairing_token":...}.

            elif mtype == "register_agents":
                if bridge is None:
                    await websocket.send_text(json.dumps({"type": "error", "detail": "not paired"}))
                    continue
                agents = msg.get("agents", [])
                # Keep only name / read_only / description — never config contents.
                bridge.agents = [
                    {"name": a.get("name"), "read_only": bool(a.get("read_only", False)),
                     "description": a.get("description", "")}
                    for a in agents if isinstance(a, dict) and a.get("name")
                ]
                await websocket.send_text(json.dumps({"type": "agents_registered", "count": len(bridge.agents)}))

            elif mtype == "task_reply":
                tid = msg.get("id")
                if not isinstance(tid, str):
                    continue
                entry = task_results.get(tid)
                if (bridge is None or bridge.socket is not websocket
                        or task_owners.get(tid) != token or entry is None
                        or entry["expires"] <= _now()):
                    continue
                if entry.get("status") not in ("dispatched", "pending_approval"):
                    continue  # terminal replies cannot be overwritten/replayed
                result = {"id": tid, "ok": msg.get("ok") is True,
                          "requires_confirmation": msg.get("requires_confirmation") is True,
                          "status": msg.get("status", "completed" if msg.get("ok") else "failed")}
                if result["ok"]:
                    result["result"] = msg.get("result")
                else:
                    result["error"] = msg.get("error", "bridge reported failure")
                task_results[tid] = {**result, "expires": entry["expires"]}
                fut = pending_tasks.get(tid)
                if fut is not None and not fut.done():
                    fut.set_result(result)

            elif mtype == "push_event":
                if bridge is None:
                    await websocket.send_text(json.dumps({"type": "error", "detail": "not paired"}))
                    continue
                _handle_event(str(msg.get("agent", "")), str(msg.get("event", "")))
                # Payload contents are dropped; adapter logs type+agent only.

            else:
                await websocket.send_text(json.dumps({"type": "error", "detail": "unknown message type"}))
    except WebSocketDisconnect:
        pass
    finally:
        if bridge is not None and bridge.socket is websocket:
            bridge.socket = None  # pairing survives; bridge can reconnect
            log.info("bridge socket dropped bridges_paired=%d", len(bridges))
