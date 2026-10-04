"""The shared web app's API service (M5, #13; docs/architecture/m5-web-app.md).

    python -m vhil.server [--host 0.0.0.0] [--port 8080]

Configuration comes from the environment (vhil.server.config.Settings). The
system files in the workspace stay the source of truth: this service reads and
(later) commits them; it never keeps a system only in its own store.
"""
from vhil.server.app import create_app

__all__ = ["create_app"]
