#!/usr/bin/env python3
"""
Thin entrypoint that delegates to src.main.run().
All polling, swapping, refunds, heartbeat, etc. live under src/.
"""

from src.main import run


if __name__ == "__main__":
    # ``False`` is reserved for an admission-control rejection before state is opened.
    # Preserve successful graceful shutdowns (which return ``None``) as exit status zero.
    if run() is False:
        raise SystemExit(1)
