# Local Agent Bridge — Relay API

The cloud component of Local Agent Bridge: a FastAPI service that Meta's Muse
connector platform calls over HTTPS, and that users' local bridge daemons dial
OUT to over a persistent WebSocket. **The relay never dials in to anyone's
home.** Task payloads live in memory only with a 5-minute TTL; nothing is
written to disk and no message contents are ever logged.

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

Or with Docker:

```bash
docker build -t lab-relay .
docker run -p 8000:8000 lab-relay
```

Environment knobs: `TASK_TIMEOUT_SECONDS` (default 30), `TASK_TTL_SECONDS`
(default 300), `CODE_TTL_SECONDS` (default 600), `PORT` (default 8000).

## Auth

- **Meta-facing HTTP** (`/v1/*` except `/v1/pair`): bearer API key in the
  `Authorization` header. This is the **adapter layer for Meta's connector
  auth — key issuance/validation to be finalized against Meta's published
  connector spec**. Currently keys are issued by `POST /v1/pair` and held in
  memory only.
- **Bridge WebSocket** (`/v1/bridge`): the pairing token issued at pair time,
  passed as `?token=<pairing_token>`. The one exception is the initial
  code-registration handshake, which connects anonymously.
- `POST /v1/pair` is unauthenticated by design: the 8-char device code is the
  credential (device-code flow).

## Endpoints

| Method & path       | Auth            | Request body | Response | Notes |
|---------------------|-----------------|--------------|----------|-------|
| `GET /healthz`      | none            | — | `{"status":"ok","bridges_paired":n}` | liveness |
| `POST /v1/pair`     | code (no bearer) | `{"code":"ABCDEFGH"}` | `{"paired":true,"pairing_token":"lbt_…","api_key":"lab_…"}` | Codes are random, single-use, 10-min TTL. `410` if invalid/expired. |
| `GET /v1/agents`    | bearer API key  | — | `{"agents":[{"name","read_only","description"}]}` | Names + flags + descriptions only; never config contents, URLs, or tokens. |
| `POST /v1/tasks`    | bearer API key  | `{"agent","action","input"}` | `{"id","ok","result"\|"error","requires_confirmation"}` | Forwards over the bridge socket, waits up to `TASK_TIMEOUT_SECONDS`. `503` if the bridge is offline, `504` if it doesn't reply in time. |
| `GET /v1/tasks/{id}`| bearer API key  | — | stored result | Results expire after `TASK_TTL_SECONDS` (default 5 min); `404` after expiry. |
| `POST /v1/events`   | bearer pairing token or API key | `{"agent","event","payload"}` | `{"accepted":true}` | Bridge→Muse push intake (HTTP alternate; primary path is the socket message below). |

### Bridge WebSocket: `WS /v1/bridge`

Opened **outbound from the home** by the local bridge daemon. The pairing
token is offered as a `Sec-WebSocket-Protocol` subprotocol (keeps the token
out of URLs and access logs); `?token=` query param is accepted as a legacy
fallback. Omit auth only for the initial code-registration handshake. Message
protocol (JSON, one object per frame):

Bridge → relay:

| `type` | fields | meaning |
|---|---|---|
| `request_code` | — | (anonymous socket) ask the relay for a pairing code → `{"type":"code_issued","code":"ABCDEFGH"}` |
| `register_agents` | `agents: [{name, read_only, description, …}]` | register capabilities; relay keeps only name/flag/description |
| `task_reply` | `id, ok, result?, error?, requires_confirmation` | answer to a dispatched task |
| `push_event` | `agent, event, payload` | proactive event → handed to `MetaDeliveryAdapter` (type + agent name only) |

Relay → bridge:

| `type` | fields | meaning |
|---|---|---|
| `task` | `id, agent, action, input` | dispatched task; reply with `task_reply` |
| `code_issued` / `agents_registered` / `error` | — | acknowledgements |
| `paired` | `pairing_token` | sent down the code-requesting socket after `POST /v1/pair` succeeds; the bridge stores the token and reconnects with it |

Pairing flow: bridge connects anonymously → `request_code` → user sees the
code in the bridge UI → user gives the code to Muse → Muse calls
`POST /v1/pair` → relay returns `api_key` (for Muse's HTTP calls) and
`pairing_token` (user pastes into bridge config) → bridge reconnects offering
the pairing token as a WebSocket subprotocol and calls `register_agents`.

## How the Meta-connector adapter layer attaches

The seam is `meta_adapter.py: MetaDeliveryAdapter` (see the TODO in the file):

- **Proactive pushes today:** `push_event` messages and `POST /v1/events`
  land in `MetaDeliveryAdapter.deliver(agent, event_type)`, which currently
  logs the event type + agent name only. Once Meta publishes the connector
  push mechanism, implement `deliver()` there to POST to Meta's endpoint —
  one module changes, nothing else.
- **Auth today:** `require_api_key()` in `app.py` is the documented adapter
  layer for Meta's connector auth. When Meta's spec lands, replace the
  in-memory key table with their token validation (e.g. signed connector
  tokens), keeping the `Depends` signature so endpoints don't change.
- **Payloads:** keep everything else as-is — in-memory, TTL-evicted, never
  logged, never persisted.

## Privacy

- Task inputs/results live in memory only, evicted after `TASK_TTL_SECONDS`.
- No disk writes, no request/response content logging, no analytics.
- Logs carry only counters: tasks relayed, task errors, events by type,
  pairings completed. Never contents, codes, tokens, or keys.
- `/v1/agents` strips agent configs down to name / `read_only` / description.
