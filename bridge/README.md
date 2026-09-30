# Local Agent Bridge — bridge daemon

The bridge is a small Python daemon that runs on the user's home machine. It
opens a single **outbound** WebSocket to the cloud relay, receives tasks, runs
them against configured local agents (Hermes, OpenClaw, Home Assistant,
webhooks, shell commands), and pushes local events back up the socket.

Nothing on the internet ever dials in: no listening sockets, no inbound ports,
no port forwarding. The relay (built by a sibling piece of this project) just
shuttles messages between an HTTP client and this bridge. Meta/Muse delivery
is a logging stub, not an implemented integration.

## Install

```bash
cd ~/workspace/local-agent-bridge/bridge
pip install -r requirements.txt      # pyyaml, websockets
cp config.example.yaml config.yaml
# edit config.yaml for your local agents (see Config reference)
python3 bridge.py --config ./config.yaml
```

Config path can also come from the `BRIDGE_CONFIG` env var (default
`./config.yaml`).

Optional Linux systemd template (headless writes requiring approval are denied):

```bash
# copy local-agent-bridge.service to ~/.config/systemd/user/
# adjust the ExecStart paths, then:
systemctl --user daemon-reload
systemctl --user enable --now local-agent-bridge
```

If you use the `homeassistant` adapter, put the token in
`~/.local-agent-bridge/env` as `HASS_TOKEN=...` (mode 600) and point the
unit's `EnvironmentFile` at it — never put the token in the YAML.

## Pairing flow (device flow)

1. On first run with no stored token, the bridge opens an anonymous socket
   to the relay and asks for a pairing code. The relay issues an 8-character
   code (no ambiguous chars like `0`/`O` or `1`/`I`) and the bridge prints:
   > Submit this code to the relay using `POST /v1/pair`.
2. The bridge waits on that socket for up to 10 minutes.
3. When a client completes pairing (`POST /v1/pair`), the relay
   pushes the pairing token back down the same socket. The bridge stores it
   in `~/.local-agent-bridge/token` with mode `0600` and reconnects.
4. Every later run skips pairing and offers the stored token as a WebSocket
   subprotocol. To re-pair, delete the token file and restart.

## Config reference

```yaml
relay_url: "wss://relay.example.com/v1/bridge"   # outbound WebSocket, required
token_file: "~/.local-agent-bridge/token" # ~ resolves to the user's home
auto_approve: []  # optional exact agent/action pairs; empty by default
```

### Agents

Each entry under `agents:` has `name`, `type`, and type-specific fields.
Adapter types:

| type | fields | notes |
|---|---|---|
| `hermes` | `url` | POST `{action, input}` to a local Hermes HTTP API |
| `openclaw` | `url` | POST `{action, input}` to the OpenClaw gateway |
| `homeassistant` | `base_url`, optional `token:`, optional `poll_entity:` | Token from `HASS_TOKEN` env (preferred). Actions: `get_state` `{entity_id}`, `call_service` `{domain, service, entity_id?, data?}` |
| `webhook` | `url` | Generic HTTP POST `{action, input}` |
| `command` | `allowlist:` | Shell exec; each action declares `command:`, which must match an `allowlist` entry **exactly** or it is refused. Arguments come from the action declaration only, never from task input. |

### Actions and confirmations

Every action must declare a YAML boolean `read_only: true|false`. A read-only
label is a local policy assertion, not enforcement of the downstream API.

Writes require local terminal approval **before** the adapter is called. The
first reply is `{id, ok: false, status: "pending_approval",
requires_confirmation: true}`. Review the displayed request and local action
configuration, then type `approve <task-id>` within 120 seconds. All other
answers, EOF/unavailable terminals, exceptions, and expiry deny execution.
The final reply has status `completed`, `denied`, `expired`, or `failed`.
The caller polls the relay's `GET /v1/tasks/{id}` for the final outcome.

No relay message or caller-controlled approval flag can grant approval.
`auto_approve` is an optional local list of exact `agent/action` pairs; bare
action names are rejected. A configured pair authorizes every input accepted
by that action, so keep such actions narrowly constrained at the adapter.

Task IDs are bound to a snapshot of agent/action/input. Replay returns the
saved outcome and never invokes the adapter again within the bridge process.
Changing the input under an existing ID is rejected. Failures consume the ID
because a side effect may already have occurred. The cache holds at most
10,000 IDs and fails closed when full; restart clears it. It is not durable
exactly-once execution, and a new HTTP POST creates a new task ID. Never blindly
retry writes after uncertain outcomes. See the root README for demo commands.

## Events

```yaml
events:
  - agent: homeassistant
    event: front_door_opened
    interval: 30
    match: {key: "state", from: "off", to: "on"}
```

Each rule polls a pollable adapter (`homeassistant` with `poll_entity`, or a
`webhook` URL via GET) every `interval` seconds. The first sample only sets a
baseline. On a change matching the optional `match` spec (dotted key path,
optional `from`/`to` values), the bridge pushes up the socket:

```json
{"type": "push_event", "agent": "homeassistant", "event": "front_door_opened",
 "payload": {"changed_keys": ["state", "last_changed"]}}
```

Event payloads are metadata-only (changed key names), never raw contents.

## Security notes

- **Outbound-only.** The bridge never opens a listening port; the connection
  is always initiated from the home machine. Reconnects use exponential
  backoff (1s, doubling, capped at 300s).
- **Token file.** Pairing tokens live in `token_file` (default
  `~/.local-agent-bridge/token`) written with mode `0600` on POSIX; Windows security depends on directory
  ACLs. Delete it to re-pair. This does not revoke old relay credentials; the
  prototype clears them only when the relay restarts.
- **HA tokens.** The Home Assistant token comes from the `HASS_TOKEN`
  environment variable (or `token:` in config). It is never logged.
- **Command allowlist.** `command`-type actions execute only if the exact
  command string is listed in the adapter's `allowlist`, and the binary must
  exist on PATH. Anything else is refused. Task input can never inject
  arguments — the command is fixed in the config.
- **Console and logs.** Ordinary logs omit task contents. The trusted local
  approval console intentionally displays the full task and action definition.
  Avoid terminal recording when inputs contain sensitive data.
- **Cloud surface.** The only cloud dependency is the relay URL itself. Local
  agent URLs stay on the LAN/loopback.
