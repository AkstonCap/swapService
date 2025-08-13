#!/usr/bin/env python3
"""
Thin entrypoint that delegates to src.main.run().
All polling, swapping, refunds, heartbeat, etc. live under src/.
"""

from src.main import run


if __name__ == "__main__":
    run()
