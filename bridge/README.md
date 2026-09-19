# Local Agent Bridge — bridge daemon

The bridge is a small Python daemon that runs on the user's home machine. It
opens a single **outbound** WebSocket to the cloud relay, receives tasks, runs
them against configured local agents (Hermes, OpenClaw, Home Assistant,
webhooks, shell commands), and pushes local events back up the socket.

Nothing on the internet ever dials in: no listening sockets, no inbound ports,
no port forwarding. The relay (built by a sibling piece of this project) just
shuttles messages between Meta's Muse and this bridge.

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

Optional: install the systemd unit so it starts at boot:

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
   > "In Muse, say 'connect my local agents' and enter this code."
2. The bridge waits on that socket for up to 10 minutes.
3. When the user completes pairing in Muse (`POST /v1/pair`), the relay
   pushes the pairing token back down the same socket. The bridge stores it
   in `~/.local-agent-bridge/token` with mode `0600` and reconnects.
4. Every later run skips pairing and offers the stored token as a WebSocket
   subprotocol. To re-pair, delete the token file and restart.

## Config reference

```yaml
relay_url: "wss://relay.example.com/v1/bridge"   # outbound WebSocket, required
token_file: "~/.local-agent-bridge/token" # ~ resolves to the user's home
auto_approve:                             # action names that skip confirmation
  - light_turn_on
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

Every action must declare `read_only: true|false`:

- `read_only: true` — the bridge runs it and replies `{id, ok, result}`.
- `read_only: false` — state-changing. The reply includes
  `"requires_confirmation": true`, and Muse asks the user to confirm before
  the action is allowed again. No confirmation is needed for `read_only`
  actions.

`auto_approve:` is a list of action **names** stored on the user's own
machine. A state-changing action whose name appears there skips confirmation —
the user-side override for routine actions (e.g. turning lights on/off).
Read-only actions never need it.

Task shape in, reply shape out:

```json
{"id": "abc123", "agent": "homeassistant", "action": "get_state",
 "input": {"entity_id": "light.kitchen"}}
```
```json
{"id": "abc123", "ok": true, "result": {"state": "off"}}
// state-changing action not in auto_approve:
{"id": "abc123", "ok": true, "result": {...}, "requires_confirmation": true}
// failure:
{"id": "abc123", "ok": false, "error": "unknown action: foo"}
```

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
{"type": "event", "agent": "homeassistant", "event": "front_door_opened",
 "payload": {"changed_keys": ["state", "last_changed"]}}
```

Event payloads are metadata-only (changed key names), never raw contents.

## Security notes

- **Outbound-only.** The bridge never opens a listening port; the connection
  is always initiated from the home machine. Reconnects use exponential
  backoff (1s, doubling, capped at 300s).
- **Token file.** Pairing tokens live in `token_file` (default
  `~/.local-agent-bridge/token`) written with mode `0600`. Delete it to
  re-pair.
- **HA tokens.** The Home Assistant token comes from the `HASS_TOKEN`
  environment variable (or `token:` in config). It is never logged.
- **Command allowlist.** `command`-type actions execute only if the exact
  command string is listed in the adapter's `allowlist`, and the binary must
  exist on PATH. Anything else is refused. Task input can never inject
  arguments — the command is fixed in the config.
- **Logging.** Logs to stdout carry only counters and metadata (agent name,
  action name, ok/error). Tokens, pairing codes (after use), HA tokens, task
  input contents, and event payloads are never logged.
- **Cloud surface.** The only cloud dependency is the relay URL itself. Local
  agent URLs stay on the LAN/loopback.
