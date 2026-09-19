# Local Agent Bridge

Local Agent Bridge lets Meta's Muse personal agent reach your own in-home and local AI agents: Hermes, OpenClaw, Home Assistant-based agents, and anything else you can call from your own machine. Your home data stays home; Muse talks to your local agents through a privacy-first bridge, conversationally.

**Who it's for:** anyone who runs a local AI stack (home server agents, smart-home automations, custom tooling) and wants Muse to act on it without shipping their home data to the cloud.

## Architecture

```
 Muse (Meta personal agent)
   |
   |  HTTPS REST (bearer auth pending Meta's connector spec)
   v
 relay/  (FastAPI cloud API)
   POST /v1/pair      device-flow pairing
   POST /v1/tasks     submit a task to a local agent
   GET  /v1/tasks/{id}  check task status
   GET  /v1/agents     list paired agents and their capabilities
   POST /v1/events     push events up to Muse
   GET  /healthz       health check
   |
   |  persistent WebSocket, opened OUTBOUND from home
   |  (home dials out; the relay never dials in)
   v
 bridge/  (Python daemon on your own machine)
   - reads YAML config with adapters:
       hermes | openclaw | homeassistant | webhook | command
   - tags every capability read_only or state_changing
   - enforces per-action confirmation / auto_approve rules
   - pushes events up the socket
   |
   |  local adapters
   v
 Local agents:  Hermes  |  OpenClaw  |  Home Assistant  |  webhooks / shell
```

The key trust boundary: the bridge always initiates the connection. The relay never reaches into your home network. Payloads live in the relay's memory only, with a 5-minute TTL; nothing is written to disk and no message content is logged.

`website/` is the static product site (index, privacy, terms).

## Quickstart

### Run the bridge locally

```bash
cd bridge
cp config.example.yaml config.yaml      # edit: your adapters and approval rules
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m bridge
```

Pairing uses a device flow: the bridge shows an 8-character code, you approve it in your connector app, and the bridge opens its outbound WebSocket to the relay. See `bridge/README.md` for the full adapter reference.

### Run the relay locally (demo)

```bash
cd relay
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn relay:app --host 127.0.0.1 --port 8080
```

Open `http://127.0.0.1:8080/healthz` to confirm it is up. See `relay/README.md` for the API reference.

## Config example

```yaml
bridge_name: "home-bridge"
relay_url: "wss://relay.example/v1/bridge"

adapters:
  hermes:
    type: hermes
    socket: "/run/hermes.sock"

  homeassistant:
    type: homeassistant
    url: "http://homeassistant:8123"
    # token stays in config.yaml on YOUR machine; never committed

actions:
  - name: "lights.status"
    adapter: homeassistant
    kind: read_only

  - name: "lights.turn_on"
    adapter: homeassistant
    kind: state_changing
    requires_confirmation: true

  - name: "nightly.backup"
    adapter: command
    command: "/home/ttelenda/bin/nightly-backup.sh"
    kind: state_changing
    requires_confirmation: false
    auto_approve: true   # explicit per-action override on your machine
```

`config.yaml` is gitignored. Never commit tokens, configs, or `.env` files.

## Confirmation semantics

Every capability the bridge exposes is tagged:

- **read_only**: queries and reads. Safe to run without asking.
- **state_changing**: turns things on/off, sends messages, runs scripts. Defaults to **requires_confirmation: true**.

Unless a specific action carries an explicit `auto_approve: true` override in your own `config.yaml`, Muse asks you first before any state-changing action runs. The override lives on your machine, in your config, under your control; nothing about it is stored on the relay. Remove the override at any time to go back to ask-first.

## Events and proactive push

The bridge can push events up the open WebSocket at any time (sensor changes, task completions, alerts from local agents). The relay holds them in memory and delivers them to Muse through `POST /v1/events`. This is what lets your local setup tap Muse on the shoulder instead of waiting to be asked: a completed long-running task, a device going offline, a reminder your home agent wants to raise. Events follow the same privacy rules as tasks (memory only, 5-minute TTL).

## Privacy summary

- **No data collection.** The bridge runs on your machine; your local agents, tokens, and configs never leave your home except over the outbound socket you opened.
- **Memory-only relay.** The relay keeps payloads in memory with a 5-minute TTL. No disk writes, no content logging.
- **Home dials out.** The relay never initiates a connection into your network. If the socket drops, nothing can reach in.
- **Nothing to commit.** `.gitignore` excludes `config.yaml`, `*.token`, `.env`, and token files so credentials never land in the repo.

## Roadmap

Meta connector-platform adapter pending their published spec. The relay exposes a clean REST + WebSocket API ready to wire up; a `MetaDeliveryAdapter` stub is in place where Meta's delivery hooks will land. Pairing currently uses bearer auth documented as an adapter layer until Meta publishes the connector spec.

## Layout

- `bridge/`: open-source Python daemon for your own machine
- `relay/`: FastAPI cloud API
- `website/`: static product site (index, privacy, terms)

## License

MIT, see [LICENSE](LICENSE).
