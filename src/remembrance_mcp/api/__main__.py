"""Run the Remembrance REST API with ``python -m remembrance_mcp.api``."""

from __future__ import annotations

import argparse
import logging

from remembrance_mcp.api.rest import start_rest_api
from remembrance_mcp.config import Settings
from remembrance_mcp.pipeline import MemoryPipeline


def main() -> None:
    parser = argparse.ArgumentParser(description="Remembrance REST API")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address")
    parser.add_argument("--port", type=int, default=8788, help="Port number")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    pipeline = MemoryPipeline(settings=Settings())
    start_rest_api(pipeline, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
