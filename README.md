# Local Agent Bridge

A prototype outbound WebSocket bridge for configured local agents. The Python
bridge and FastAPI relay work locally; Meta/Muse connector integration is not
implemented. `MetaDeliveryAdapter` only logs event metadata. An accepted event
does **not** mean anything was delivered externally.

Task inputs and adapter results pass through the relay and its authenticated
HTTP API. Local configuration and adapter credentials are not registered with
the relay, but results may contain sensitive data. Choose exposed actions
accordingly. Outbound connectivity does not make the relay untrusted: it can
request any configured action.

## Project status

Application submitted to the Meta Connections team; review and approval are pending.
This application status is reported by the project maintainer. Approval is not
guaranteed, and submission does not mean the integration is live. The local
bridge and relay demo work; Meta/Muse delivery is still a logging stub.

## Reproduce the local demo

Python 3.11 or 3.12 recommended (verified with Python 3.12 on Windows). From the
repository root in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q tests --basetemp .pytest_cache\verification
.\.venv\Scripts\python.exe tests\local_demo.py
```

On macOS/Linux replace `.\.venv\Scripts\python.exe` with `.venv/bin/python`.
The demo starts a temporary relay on an available loopback port, pairs a real
bridge protocol client, checks approval before execution using an in-memory
synthetic adapter, and runs the existing 17-check relay smoke test. It shuts
down its relay afterward. It uses no household services or existing credentials.
The demo injects a trusted test approval callback; normal operation uses a local
interactive terminal, never an API approval flag.

## Run manually

From the repository root, after installing dependencies:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app:app --app-dir relay --host 127.0.0.1 --port 8080 --no-access-log
```

In another terminal, copy `bridge/config.example.yaml` to `bridge/config.yaml`,
set `relay_url: ws://127.0.0.1:8080/v1/bridge`, and replace the illustrative
agents with adapters you actually operate. The example includes Linux commands
and a Home Assistant adapter requiring `HASS_TOKEN`; it is not a ready-to-run
household configuration. Then:

```powershell
.\.venv\Scripts\python.exe bridge\bridge.py --config bridge\config.yaml
```

The bridge prints a pairing code. In a third terminal:

```powershell
$pair = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8080/v1/pair -ContentType application/json -Body '{"code":"REPLACE8"}'
$headers = @{ Authorization = "Bearer $($pair.api_key)" }
Invoke-RestMethod -Uri http://127.0.0.1:8080/v1/agents -Headers $headers
```

The pairing token reaches the waiting bridge automatically and is saved in
`token_file`. Keep the returned API key private. The relay keeps pairing state
in memory: after relay restart, remove the local token file and pair again.
Use TLS (`wss`/HTTPS) for non-loopback use; no public deployment is configured
by these instructions. Run a single relay worker; state is not shared.

## Configuration and approval

The supported structure is `agents` with nested `actions`:

```yaml
relay_url: ws://127.0.0.1:8080/v1/bridge
token_file: ./demo.token
auto_approve: []
agents:
  - name: example
    type: webhook
    url: http://127.0.0.1:9999/hook
    actions:
      - name: send_notification
        read_only: false
```

This example needs a separately operated endpoint; the automated demo does not.
`read_only` must be a YAML boolean. It is an operator assertion about the
adapter's behavior, not a sandbox. Generic HTTP and agent endpoints may have
side effects: do not label them read-only unless that contract is enforced.

Writes return `status: pending_approval` with `ok: false` before execution.
Review the task ID, agent, action, complete input, and local action definition
at the bridge terminal. Type `approve <task-id>` within 120 seconds. Any other
answer, expiry, unavailable terminal, or approval error denies execution.
Poll `GET /v1/tasks/{id}` for the final outcome. There is no remote approval
endpoint; caller-supplied approval flags cannot authorize execution.

Only explicit local `auto_approve` entries such as `example/send_notification`
bypass the prompt. Bare action names are rejected to avoid approving the same
name on another agent. The default list is empty. Headless services can execute
read-only or specifically auto-approved actions, but cannot approve other writes.

## Retries and limits

- A task ID is bound to its original agent/action/input. Within one bridge
  process, repeats return the saved outcome; altered requests are rejected.
  Denied, expired, and failed attempts cannot be approved or executed on replay.
- The bridge keeps request hashes and outcomes in memory, capped at 10,000
  accepted IDs, then rejects new tasks. A local restart clears this history.
  Writes requiring approval need a fresh local decision after restart.
- Every new HTTP POST creates a new task ID. Do not automatically retry POSTs,
  especially for auto-approved actions or after an uncertain adapter failure.
  This prototype does not provide durable exactly-once side effects.
- Relay results are readable only by the paired API key, expire after 300
  seconds by default, and are swept within another 60 seconds. Keep the TTL
  longer than the approval window. Adapter outputs cached for bridge retry
  protection remain in local process memory until exit.
- An adapter may finish despite a connection failure. Inspect the actual
  adapter state before submitting a new write. A pending status is not success.
- Remote human approval would require a separate authenticated human channel;
  the task caller's bearer credential is deliberately insufficient. Choosing
  that channel is future product design, not a reason to weaken this gate.

See [bridge reference](bridge/README.md) and [relay reference](relay/README.md).
No credentials, live services, or deployment are needed for the synthetic demo.

## License

MIT; see [LICENSE](LICENSE).
