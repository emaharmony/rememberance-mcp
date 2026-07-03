"""
Serve command — Start Remembrance REST API + NATS subscriber.

PATTERN: Service Runner (Process Manager)
============================================

This starts Remembrance as a long-running service with:
  1. REST API on port 8788 (HTTP interface for Go client, CLI, scripts)
  2. NATS subscriber on *.agent.output (automatic capture from Prism)

The service is designed to run alongside Prism. Prism publishes
agent output events to NATS; Remembrance subscribes and auto-captures.

Usage:
    python -m remembrance_mcp.serve
    python -m remembrance_mcp.serve --port 8788 --nats nats://localhost:4222
    python -m remembrance_mcp.serve --no-nats  # REST API only
"""

from __future__ import annotations

import argparse
import logging
import threading

from remembrance_mcp.api.rest import start_rest_api
from remembrance_mcp.config import Settings
from remembrance_mcp.pipeline import MemoryPipeline

logger = logging.getLogger(__name__)

# Shutdown event — set by signal handler, checked by main loop
_shutdown_event = threading.Event()


def main():
    parser = argparse.ArgumentParser(description="Remembrance Memory Service")
    parser.add_argument(
        "--host", default="127.0.0.1", help="REST API bind address (default: 127.0.0.1)"
    )
    parser.add_argument("--port", type=int, default=8788, help="REST API port (default: 8788)")
    parser.add_argument(
        "--nats",
        default="nats://localhost:4222",
        help="NATS server URL (default: nats://localhost:4222)",
    )
    parser.add_argument(
        "--no-nats", action="store_true", help="Disable NATS subscriber (REST API only)"
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    # Configure logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Initialize pipeline
    settings = Settings()
    pipeline = MemoryPipeline(settings=settings)
    logger.info(f"Remembrance pipeline initialized (db: {settings.DB_PATH})")

    # Start NATS subscriber (optional)
    nats_sub = None
    if not args.no_nats:
        try:
            from remembrance_mcp.nats_sub import NatsSubscriber

            nats_sub = NatsSubscriber(
                pipeline=pipeline,
                nats_url=args.nats,
                settings=settings,
            )
            nats_sub.start()
            logger.info(f"NATS subscriber started on {args.nats}")
        except Exception as e:
            logger.warning(f"NATS subscriber failed to start: {e}")
            logger.info("Continuing in REST-only mode")

    # Graceful shutdown via signal — sets event instead of sys.exit
    import signal

    def handle_shutdown(signum, frame):
        logger.info("Shutdown signal received, cleaning up...")
        _shutdown_event.set()

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    # Start REST API (blocking until shutdown event is set)
    logger.info(f"Remembrance service ready — REST API on http://{args.host}:{args.port}")
    if nats_sub:
        logger.info("NATS subscriber active — listening for agent output events")
    try:
        # start_rest_api blocks; we wrap it to allow graceful shutdown
        # by running in a thread and checking the shutdown event
        api_thread = threading.Thread(
            target=start_rest_api,
            args=(pipeline, args.host, args.port),
            daemon=True,
            name="remembrance-rest",
        )
        api_thread.start()

        # Wait for shutdown signal
        _shutdown_event.wait()
        logger.info("Shutting down Remembrance service...")
    except KeyboardInterrupt:
        logger.info("Interrupted")
    finally:
        if nats_sub:
            nats_sub.stop()


if __name__ == "__main__":
    main()
