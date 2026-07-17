from __future__ import annotations

import argparse

import uvicorn

from .runtime import build_coordinator
from .scheduler import Scheduler
from .webhook import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenHands Symphony service")
    parser.add_argument("--config", help="path to config.toml")
    args = parser.parse_args()
    config, store, coordinator = build_coordinator(args.config)
    scheduler = Scheduler(config, store, coordinator)
    app = create_app(store, coordinator, scheduler, config.service.webhook_secret_file)
    uvicorn.run(app, host=config.service.listen_host, port=config.service.listen_port, log_level="info")


if __name__ == "__main__":
    main()
