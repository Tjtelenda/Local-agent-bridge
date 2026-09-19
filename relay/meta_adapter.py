"""Adapter layer for delivering bridge events to Meta's Muse.

This module is the single seam where the relay hands proactive push events
to Meta's side. Right now it is a stub: Meta's developer-connector push
mechanism has not been published yet, so there is nowhere real to send to.

PRIVACY: deliver() receives only the event TYPE and the agent name. The event
payload contents are intentionally NOT passed in — they never leave the
caller's memory, and this stub logs only counters/type names, never contents.
"""


class MetaDeliveryAdapter:
    """Stub: forwards bridge push events toward Muse.

    TODO: deliver to Muse via Meta's connector push mechanism once their
    spec is published. When that happens:
      1. Add the push endpoint/auth Meta specifies to `__init__` (or env config).
      2. Implement deliver() to POST the (type, agent) envelope — keep payload
         contents out of it unless Meta's spec explicitly requires them and
         the user has opted in; privacy is the product.
      3. Add retry/backoff with a bounded in-memory queue (never disk).
    """

    def __init__(self):
        self.events_accepted = 0  # counter only

    def deliver(self, agent: str, event_type: str) -> bool:
        """Accept an event for delivery to Muse.

        Args:
            agent: name of the local agent that raised the event.
            event_type: short event type label (e.g. "motion_detected").
                       Never the payload contents.

        Returns:
            True if the event was accepted for delivery.
        """
        # Log ONLY the event type and agent name — never payload contents,
        # codes, tokens, or keys.
        self.events_accepted += 1
        print(f"[meta-adapter] event accepted: type={event_type} agent={agent} "
              f"total_accepted={self.events_accepted}", flush=True)
        return True
