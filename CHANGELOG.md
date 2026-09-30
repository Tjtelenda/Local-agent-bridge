# Changelog

## Unreleased

### Fixed

- Require local approval before state-changing adapter calls. Caller-supplied
  confirmation flags cannot authorize execution.
- Bind approval to the reviewed request and local action configuration. Denial,
  expiry, unavailable terminals, and cancelled connections fail closed.
- Deduplicate task IDs during the bridge process lifetime, reject changed
  requests under an existing ID, and consume failed execution attempts.
- Bind relay task replies and result retrieval to the owning pairing.
- Reject stale config structure and ambiguous auto-approval names; require
  explicit boolean read-only policy and exact agent/action approval pairs.
- Correct startup commands, example configuration, and the systemd template.

### Added

- Regression coverage for approval, replay, failure, pairing, and task ownership.
- A reproducible loopback demo using the real bridge protocol and synthetic data.
- Maintainer-reported Meta Connections application status: submitted, with
  review and approval pending. External delivery remains an unimplemented stub.

### Clarified

- Task inputs and adapter results cross the relay. Outbound connectivity is not
  a substitute for trust in the relay or constrained local adapter policy.
- Replay protection is bounded and process-local; a new POST is a new request.
  Read-only declarations are operator assertions, not an adapter sandbox.
