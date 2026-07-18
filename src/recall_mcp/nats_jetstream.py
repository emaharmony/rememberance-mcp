"""Durable NATS JetStream ingestion for Recall."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
from typing import Any

from recall_mcp.config import Settings
from recall_mcp.pipeline import MemoryPipeline

logger = logging.getLogger(__name__)


class DurableNatsSubscriber:
    """Pull agent output events with explicit, post-commit acknowledgement."""

    def __init__(
        self,
        pipeline: MemoryPipeline,
        nats_url: str | None = None,
        subject: str | None = None,
        settings: Settings | None = None,
    ):
        self.pipeline = pipeline
        self.settings = settings or pipeline.settings
        self.nats_url = nats_url or self.settings.NATS_URL
        self.subject = subject or self.settings.NATS_SUBJECT
        self._running = False
        self._thread: threading.Thread | None = None
        self._connected = False
        self._last_error = ""
        self._processed = 0
        self._failed = 0
        self._dead_lettered = 0
        self._pending = 0
        self._redelivered = 0
        self._stop_event = threading.Event()

    def start(self) -> None:
        if self._running:
            return
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="recall-jetstream",
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)

    def health(self) -> dict[str, Any]:
        return {
            "connected": self._connected,
            "stream": self.settings.NATS_STREAM,
            "pending": self._pending,
            "redelivered": self._redelivered,
            "consumer": self.settings.NATS_CONSUMER,
            "processed": self._processed,
            "failed": self._failed,
            "dead_lettered": self._dead_lettered,
            "last_error": self._last_error,
        }

    def _run(self) -> None:
        while self._running:
            try:
                asyncio.run(self._consume())
            except Exception as exc:
                self._last_error = str(exc)
                self._connected = False
                logger.exception("JetStream subscriber disconnected: %s", exc)
            if self._running:
                self._stop_event.wait(2)

    async def _consume(self) -> None:
        try:
            import nats
            from nats.errors import TimeoutError as NatsTimeoutError
            from nats.js.api import AckPolicy, ConsumerConfig, StorageType
            from nats.js.errors import NotFoundError
        except ImportError:
            self._last_error = "nats-py is not installed"
            self._running = False
            logger.warning(self._last_error)
            return

        async def disconnected_cb() -> None:
            self._connected = False

        async def reconnected_cb() -> None:
            self._connected = True
            self._last_error = ""

        async def closed_cb() -> None:
            self._connected = False

        options: dict[str, Any] = {
            "servers": [self.nats_url],
            "name": "recall-2.1",
            "connect_timeout": 5,
            "max_reconnect_attempts": -1,
            "reconnect_time_wait": 2,
            "disconnected_cb": disconnected_cb,
            "reconnected_cb": reconnected_cb,
            "closed_cb": closed_cb,
        }
        if self.settings.NATS_CREDS_FILE:
            options["user_credentials"] = str(self.settings.NATS_CREDS_FILE)

        nc = await nats.connect(**options)
        self._connected = True
        js = nc.jetstream()

        required_subjects = {self.subject, self.settings.NATS_DLQ_SUBJECT}
        try:
            stream_info = await js.stream_info(self.settings.NATS_STREAM)
        except NotFoundError:
            await js.add_stream(
                name=self.settings.NATS_STREAM,
                subjects=sorted(required_subjects),
                storage=StorageType.FILE,
            )
        else:
            configured = set(stream_info.config.subjects or [])
            if not required_subjects.issubset(configured):
                stream_info.config.subjects = sorted(configured | required_subjects)
                await js.update_stream(config=stream_info.config)

        config = ConsumerConfig(
            durable_name=self.settings.NATS_CONSUMER,
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=60,
            max_ack_pending=64,
            max_deliver=10,
            backoff=[1, 5, 15, 30, 60, 120, 300, 300, 300, 300],
            filter_subject=self.subject,
        )
        subscription = await js.pull_subscribe(
            self.subject,
            durable=self.settings.NATS_CONSUMER,
            stream=self.settings.NATS_STREAM,
            config=config,
        )
        logger.info(
            "JetStream consumer ready: %s/%s",
            self.settings.NATS_STREAM,
            self.settings.NATS_CONSUMER,
        )

        try:
            while self._running:
                try:
                    messages = await subscription.fetch(batch=16, timeout=1)
                except NatsTimeoutError:
                    try:
                        info = await js.consumer_info(
                            self.settings.NATS_STREAM,
                            self.settings.NATS_CONSUMER,
                        )
                        self._pending = int(info.num_pending)
                    except Exception:
                        pass
                    continue
                for message in messages:
                    await self._handle_message(js, message)
        finally:
            self._connected = False
            try:
                await nc.drain()
            finally:
                await nc.close()

    @staticmethod
    def _event_id(payload: dict[str, Any], raw: bytes) -> str:
        explicit = payload.get("event_id") or payload.get("id")
        if explicit:
            return str(explicit)
        identity = "|".join(
            [
                str(payload.get("agent", "")),
                str(payload.get("session_id", "")),
                str(payload.get("turn", "")),
            ]
        )
        digest_source = identity.encode("utf-8") + b"|" + raw
        return "nats_" + hashlib.sha256(digest_source).hexdigest()

    async def _dead_letter(self, jetstream: Any, message: Any, reason: str) -> None:
        envelope = json.dumps(
            {
                "reason": reason[:1000],
                "subject": message.subject,
                "payload": message.data.decode("utf-8", errors="replace"),
            }
        ).encode("utf-8")
        await jetstream.publish(self.settings.NATS_DLQ_SUBJECT, envelope)
        await message.ack()
        self._dead_lettered += 1

    async def _handle_message(self, jetstream: Any, message: Any) -> None:
        try:
            payload = json.loads(message.data.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
            content = payload.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("content must be a non-empty string")
            if len(content) > self.settings.MAX_CAPTURE_CHARS:
                raise ValueError("content exceeds configured capture limit")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            await self._dead_letter(jetstream, message, str(exc))
            return

        event_id = self._event_id(payload, message.data)
        if not self.pipeline.store.reserve_event(event_id):
            await message.ack()
            return

        try:
            result = self.pipeline.capture(
                content,
                source=f"nats:{payload.get('agent', 'unknown')}",
                project=str(payload.get("project") or ""),
                agent=str(payload.get("agent") or ""),
            )
            self.pipeline.store.complete_event(event_id, result.get("id"))
            await message.ack()
            self._processed += 1
        except Exception as exc:
            self.pipeline.store.fail_event(event_id, str(exc))
            self._failed += 1
            self._last_error = str(exc)
            delivered = getattr(getattr(message, "metadata", None), "num_delivered", 1)
            if delivered > 1:
                self._redelivered += 1
            if delivered >= 10:
                await self._dead_letter(jetstream, message, str(exc))
            else:
                delay = min(300, 2 ** min(delivered, 8))
                await message.nak(delay=delay)
