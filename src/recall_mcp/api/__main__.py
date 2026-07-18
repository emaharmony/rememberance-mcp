"""Run the Recall REST API with ``python -m recall_mcp.api``."""

from __future__ import annotations

import argparse
import logging

from recall_mcp.config import Settings
from recall_mcp.pipeline import MemoryPipeline
from recall_mcp.api.rest import start_rest_api


def main() -> None:
    parser = argparse.ArgumentParser(description="Recall REST API")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address")
    parser.add_argument("--port", type=int, default=8788, help="Port number")
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable debug logging"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    pipeline = MemoryPipeline(settings=Settings())
    start_rest_api(pipeline, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
