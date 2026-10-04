"""FastAPI application factory."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from vhil.server import auth
from vhil.server.config import Settings
from vhil.server.workspace import NotFound, Workspace

STATIC = Path(__file__).parent / "static"


def _version() -> str:
    try:
        return version("ifs-vhil")
    except PackageNotFoundError:
        return "dev"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    ws = Workspace(settings.workspace)
    app = FastAPI(title="IFS vHIL", version=_version())
    app.state.settings, app.state.workspace = settings, ws
    auth.install(app, settings)  # login + /api/* and WebSocket guard (vhil/server/auth.py)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": _version(), "auth": settings.auth,
                "workspace_ref": ws.ref()}

    @app.get("/api/catalog")
    def catalog():
        return ws.catalog()

    @app.get("/api/systems")
    def systems():
        return ws.systems()

    @app.get("/api/systems/{system_id}")
    def system(system_id: str):
        try:
            return ws.system(system_id)
        except NotFound:
            raise HTTPException(404, f"no system '{system_id}'")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
