#!/usr/bin/env python3
"""Entrypoint for the read-only operator dashboard.

Runs as a SEPARATE process from swapService.py. It opens the state database read-only
and needs no vault keypair, no Nexus PIN and no RPC access.

    python3 dashboard.py                 # http://127.0.0.1:8787
    DASHBOARD_PORT=9000 python3 dashboard.py

Remote access: keep the default localhost bind and use an SSH tunnel —
    ssh -L 8787:127.0.0.1:8787 operator@host
Binding a non-loopback address requires DASHBOARD_TOKEN to be set, and should be
placed behind TLS.
"""
from src.dashboard import serve

if __name__ == "__main__":
    serve()
