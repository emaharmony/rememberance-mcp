"""
NATS Subscriber — Listens for agent output events and auto-captures.

PATTERN: Event-Driven Ingestion
================================

Remembrance subscribes to the NATS bus (same bus Prism uses) and
automatically captures agent output events. This means:

1. Prism doesn't need to call Remembrance synchronously for capture.
2. Remembrance ingests events as they happen — no data loss.
3. The Go client stays for synchronous reads (search, build_context).

WHY NATS?
- Prism already uses NATS JetStream for its event bus.
- Both services share the same cluster — loose coupling.
- Fire-and-forget: agent output events are captured automatically.

IDEMPOTENCY:
- Each capture uses (agent_id + session_id + turn_number) as an
  idempotency key. If the same event arrives twice (NATS replay,
  network hiccup), Remembrance skips the duplicate.

SUBJECT: *.agent.output
- Matches: lumi.agent.output, mango.agent.output, prism.agent.output
- Payload: JSON with "content", "agent", "session_id", "turn", "project"
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from typing import Optional, Set

from remembrance_mcp.config import Settings
from remembrance_mcp.pipeline import MemoryPipeline

logger = logging.getLogger(__name__)

# Idempotency: bounded deque + set for O(1) append and O(1) lookup
_MAX_SEEN = 1000


class NatsSubscriber:
    """
    Subscribes to NATS agent output events and auto-captures them.

    Usage:
        sub = NatsSubscriber(pipeline=my_pipeline)
        sub.start()   # non-blocking, runs in background thread
        # ... later ...
        sub.stop()    # graceful shutdown
    """

    def __init__(
        self,
        pipeline: MemoryPipeline,
        nats_url: str = "nats://localhost:4222",
        subject: str = "*.agent.output",
        settings: Optional[Settings] = None,
    ):
        self.pipeline = pipeline
        self.nats_url = nats_url
        self.subject = subject
        self.settings = settings or Settings()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._seen_deque: deque[str] = deque(maxlen=_MAX_SEEN)  # bounded FIFO
        self._seen_set: Set[str] = set()  # O(1) lookup companion
        self._nc = None

    def start(self) -> None:
        """Start subscribing in a background thread."""
        if self._running:
            logger.warning("NATS subscriber already running")
            return

        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="remembrance-nats")
        self._thread.start()
        logger.info(f"Remembrance NATS subscriber started on {self.nats_url}")

    def stop(self) -> None:
        """Stop the subscriber gracefully."""
        self._running = False
        if self._nc:
            try:
                self._nc.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Remembrance NATS subscriber stopped")

    def _run(self) -> None:
        """Main loop: connect to NATS and subscribe."""
        try:
            import nats
        except ImportError:
            logger.error("nats-py not installed. Install with: pip install nats-py")
            logger.info(
                "Falling back to HTTP-only mode. "
                "Capture will only work via REST API or pipeline calls."
            )
            self._running = False
            return

        import asyncio

        async def _subscribe():
            try:
                self._nc = await nats.connect(self.nats_url)
            except Exception as e:
                logger.error(f"Failed to connect to NATS at {self.nats_url}: {e}")
                logger.info("Running in HTTP-only mode (no NATS connection)")
                self._running = False
                return

            async def _handler(msg):
                try:
                    self._on_agent_output(msg.data.decode("utf-8"))
                except Exception as e:
                    logger.error(f"Error processing agent output event: {e}")

            await self._nc.subscribe(self.subject, cb=_handler)
            logger.info(f"Subscribed to {self.subject}")

            # Keep the connection alive
            while self._running:
                await asyncio.sleep(0.5)

            # Cleanup
            try:
                await self._nc.drain()
            except Exception:
                pass

        # Run the async subscriber in a new event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_subscribe())
        except Exception as e:
            logger.error(f"NATS subscriber error: {e}")
        finally:
            loop.close()

    def _on_agent_output(self, data: str) -> None:
        """
        Process an agent output event from NATS.

        Expected payload:
        {
            "content": "The actual text the agent said",
            "agent": "lumi",
            "session_id": "abc-123",
            "turn": 5,
            "project": "prism",
            "metadata": {}
        }
        """
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as e:
            logger.warning(f"Invalid JSON in agent output event: {e}")
            return

        content = payload.get("content", "")
        if not content or not content.strip():
            return

        # Idempotency check
        agent = payload.get("agent", "unknown")
        session_id = payload.get("session_id", "")
        turn = payload.get("turn", 0)
        idem_key = f"{agent}:{session_id}:{turn}"

        if idem_key in self._seen_set:
            logger.debug(f"Skipping duplicate capture: {idem_key}")
            return

        # Track the key (deque auto-evicts oldest, set is kept in sync)
        if len(self._seen_deque) >= _MAX_SEEN:
            evicted = self._seen_deque[0]  # oldest will be evicted by deque
            self._seen_set.discard(evicted)
        self._seen_deque.append(idem_key)
        self._seen_set.add(idem_key)

        # Source tag
        source = f"nats:{agent}"

        # Project for category
        project = payload.get("project", "")
        category = project if project else None

        # Capture through the pipeline
        try:
            result = self.pipeline.capture(
                text=content,
                source=source,
                category=category,
            )
            decision = result.get("decision", "unknown")
            logger.info(
                f"Captured agent output: agent={agent}, "
                f"decision={decision}, id={result.get('id', 'skip')}"
            )
        except Exception as e:
            logger.error(f"Failed to capture agent output: {e}")
