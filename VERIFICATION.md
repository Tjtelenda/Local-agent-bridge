# Local verification record

Date: 2026-09-30. Environment: Windows, Python 3.12, isolated `.venv`.
Source: public GitHub archive `Tjtelenda/Local-agent-bridge`, snapshot `bdbb90b`.
The local repair was verified against that public snapshot before publication.
No live services, household data, or existing credentials were used. No
repository AGENTS.md or SKILL.md files existed.

## Results

- `python -m pytest -q tests --basetemp .pytest_cache/verification2`:
  **29 passed**, three existing framework deprecation warnings, 0.42 seconds.
  Earlier default pytest temp access was denied by the sandbox; using a
  workspace-local temp directory resolved that environmental issue.
- `python tests/local_demo.py`: exit 0. Real loopback relay and bridge protocol
  passed pending-before-execution, exact synthetic task approval, denial,
  read-only execution, and cancellation-before-late-approval checks.
- The same demo ran `relay/test_bridge_smoke.py`: **17/17 checks passed**,
  covering pairing, single-use codes, registration, task dispatch/retrieval,
  metadata filtering, stub event acceptance, TTL expiry, and offline response.
- `python -m compileall -q bridge relay tests`: exit 0.
- `python -m pip check`: no broken requirements.
- `git diff --check`: clean (Git also reports its Windows line-ending warning).
- `docs/openapi.json` regenerated directly from the modified FastAPI app.

The synthetic demo injects a trusted approval callback into the real bridge;
it does not automate household actions. Windows terminal approval parsing is
covered with mocked keyboard input. An interactive human console session,
Linux terminal path, Docker/systemd execution, real adapters, and external
Meta/Muse delivery were not tested. Temporary relay processes were terminated.

## Security design reviewed

The remote task caller cannot grant approval. The local console is the trusted
approval channel. A reviewed snapshot includes agent, action, input, and local
action configuration. Denial, expiry, exceptions, headless execution, and
connection cancellation fail closed. Adapter execution occurs only afterward.
Replay protection consumes IDs before side effects, including adapter failures;
concurrent identical submissions execute once, and substitutions are rejected.
Only explicit boolean read-only policy or exact local agent/action auto-approval
bypasses prompting. Config migration rejects legacy global action names.

Relay tasks are bound to their paired bridge before dispatch. Unpaired or
other-bridge sockets cannot forge replies; other API keys cannot retrieve
results. Pending and final responses retain the same ID and TTL; terminal
results cannot be overwritten or resurrected after expiry.

## Remaining limits and design choices

- This is an in-memory prototype, not durable exactly-once execution. A bridge
  restart clears its bounded 10,000-task replay cache; a new HTTP POST is a new
  request. Do not automatically retry uncertain writes. Required writes need
  fresh local approval after restart; explicitly auto-approved actions do not.
- Read-only labels are operator assertions, not downstream sandboxing. Generic
  agent endpoints and broad Home Assistant service calls need careful local
  policy. No new auto-approval, credentials, persistent access, or live service
  changes were made.
- Remote conversational approval is intentionally absent. Supporting it needs
  a separate trusted human authentication channel; the task caller's bearer
  key must not become an approval credential. Choosing that channel needs
  product/user input before future implementation; it does not block this demo.
- Pairings require re-pairing after relay restart. Existing lack of a revocation
  API, durable storage, multi-worker state, and production hardening remains.
- Task data crosses the relay. Bridge retry outcomes remain in local memory
  until exit; relay results expire after the configured TTL plus sweep delay.
  Local approval displays task data. MetaDeliveryAdapter remains a logging stub.

## Changed files

Runtime/config: `bridge/bridge.py`, `bridge/config.example.yaml`,
`bridge/local-agent-bridge.service`, `relay/app.py`.

Verification: `requirements-dev.txt`, `tests/test_approval.py`,
`tests/test_relay.py`, `tests/local_demo.py`, this record.

Documentation: root/bridge/relay READMEs, `docs/openapi.json`,
`docs/{index,privacy,terms}.html`, `website/{index,privacy,terms}.html`,
`legal/{privacy,terms}.md`. The static files were edited locally only.
