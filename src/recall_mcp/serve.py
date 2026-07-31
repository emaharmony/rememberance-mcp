"""
Serve command — Start Recall REST API + NATS subscriber.

PATTERN: Service Runner (Process Manager)
============================================

This starts Recall as a long-running service with:
  1. REST API on port 8788 (HTTP interface for Go client, CLI, scripts)
  2. NATS subscriber on *.agent.output (automatic capture from Prism)

The service is designed to run alongside Prism. Prism publishes
agent output events to NATS; Recall subscribes and auto-captures.

Usage:
    python -m recall_mcp.serve
    python -m recall_mcp.serve --port 8788 --nats nats://localhost:4222
    python -m recall_mcp.serve --no-nats  # REST API only
"""

from __future__ import annotations

import argparse
import logging
import threading

from recall_mcp.config import Settings
from recall_mcp.logging_config import configure_logging, safe_endpoint
from recall_mcp.pipeline import MemoryPipeline
from recall_mcp.api.rest import start_rest_api

logger = logging.getLogger(__name__)

# Shutdown event — set by signal handler, checked by main loop
_shutdown_event = threading.Event()


def main():
    settings = Settings()
    parser = argparse.ArgumentParser(description="Recall Memory Service")
    parser.add_argument(
        "--host",
        default=settings.HOST,
        help="REST API bind address",
    )
    parser.add_argument("--port", type=int, default=settings.PORT, help="REST API port")
    parser.add_argument(
        "--nats",
        default=settings.NATS_URL,
        help="NATS server URL",
    )
    parser.add_argument(
        "--no-nats", action="store_true", help="Disable NATS subscriber (REST API only)"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable debug logging"
    )
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    configure_logging(level=level, json_logs=settings.JSON_LOGS)

    # Initialize pipeline
    pipeline = MemoryPipeline(settings=settings)
    logger.info("Recall pipeline initialized (db: %s)", settings.DB_PATH)

    # Start NATS subscriber (optional)
    nats_sub = None
    if not args.no_nats:
        try:
            from recall_mcp.nats_sub import NatsSubscriber

            nats_sub = NatsSubscriber(
                pipeline=pipeline,
                nats_url=args.nats,
                settings=settings,
            )
            nats_sub.start()
            logger.info("NATS subscriber started on %s", safe_endpoint(args.nats))
            pipeline.nats_sub = nats_sub
        except Exception:
            logger.warning("NATS subscriber failed to start")
            logger.debug("NATS startup failure detail", exc_info=True)
            logger.info("Continuing in REST-only mode")

    # Graceful shutdown via signal — sets event instead of sys.exit
    import signal

    def handle_shutdown(signum, frame):
        logger.info("Shutdown signal received, cleaning up...")
        _shutdown_event.set()

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    # Start REST API (blocking until shutdown event is set)
    logger.info(f"Recall service ready — REST API on http://{args.host}:{args.port}")
    if nats_sub:
        logger.info("NATS subscriber active — listening for agent output events")
    try:
        # start_rest_api blocks; we wrap it to allow graceful shutdown
        # by running in a thread and checking the shutdown event
        api_thread = threading.Thread(
            target=start_rest_api,
            args=(pipeline, args.host, args.port),
            daemon=True,
            name="recall-rest",
        )
        api_thread.start()

        # Wait for shutdown signal
        _shutdown_event.wait()
        logger.info("Shutting down Recall service...")
    except KeyboardInterrupt:
        logger.info("Interrupted")
    finally:
        if nats_sub:
            nats_sub.stop()
        pipeline.close()


if __name__ == "__main__":
    main()
