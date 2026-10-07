# Copyright (c) 2022-2026 Antmicro <www.antmicro.com>
#
# SPDX-License-Identifier: Apache-2.0

"""
Provides function for creating FastAPI application.
"""

import os
import shutil
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from pipeline_manager import frontend, prebuilt_frontend


def allowed_origins() -> list:
    """
    vHIL: the origins allowed to call this server from another origin
    (CORS, and socket.io's handshake), from ``PM_ALLOWED_ORIGINS``
    (comma-separated). Upstream allowed any (``*``); by default none is,
    as the editor is served on the web app's own origin (behind its proxy).

    Returns
    -------
    list
        The allowed origins (empty: same origin only).
    """
    return [
        o.strip()
        for o in os.environ.get("PM_ALLOWED_ORIGINS", "").split(",")
        if o.strip()
    ]


class ContentSecurityPolicy:
    """
    vHIL: an ASGI middleware that sets ``PM_CSP`` (a Content-Security-Policy)
    on every HTTP response, as ``Content-Security-Policy-Report-Only`` when
    ``PM_CSP_REPORT_ONLY=1``. Unset, no policy is sent (upstream's
    behaviour). vHIL's policy is vhil/server/security.py's ``editor_csp``.
    """

    def __init__(self, app, policy: str, report_only: bool = False):
        self.app = app
        self.header = (
            b"content-security-policy-report-only"
            if report_only
            else b"content-security-policy"
        )
        self.policy = policy.encode("latin-1")

    async def __call__(self, scope, receive, send):  # noqa: D102
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_policy(message):
            if message["type"] == "http.response.start":
                headers = [
                    (k, v)
                    for k, v in message.get("headers") or []
                    if k.lower() != self.header
                ]
                headers.append((self.header, self.policy))
                message = {**message, "headers": headers}
            await send(message)

        return await self.app(scope, receive, send_with_policy)


def get_default_frontend_path() -> Path:
    """
    Returns the path to the default, built frontend directory.

    Prefers a frontend built directly in the repository
    (``pipeline_manager/frontend/dist``, e.g. produced by running
    ``./build server-app`` locally), falling back to the frontend
    prebuilt in server mode and bundled with the package
    (``pipeline_manager.prebuilt_frontend``).

    Returns
    -------
    Path
        Path to the default, built frontend directory.
    """
    frontend_dist_path = Path(frontend.__file__).parent / "dist"
    if frontend_dist_path.is_dir():
        return frontend_dist_path
    return Path(prebuilt_frontend.__file__).parent / "dist"


def create_app(
    frontend_dir: Optional[Path] = None,
    relative_pm_url: Optional[Path] = None,
    follow_symlink: bool = False,
) -> FastAPI:
    """
    Hosts frontend application.

    Parameters
    ----------
    frontend_dir : Optional[Path]
        Path where the built frontend is stored.
    relative_pm_url : Optional[Path]
        Path in URL where Pipeline Manager should be served
    follow_symlink : bool
        Whether StaticFiles should follow symlinks when resolving files
        under frontend_dir. Needed when frontend_dir, or files within it,
        are only reachable through symlinks.

    Returns
    -------
    FastAPI
        FastAPI instance

    Raises
    ------
    ValueError
        Raised when relative_pm_url is provided
        but frontend_path is None.
    """
    app = FastAPI(title="Pipeline Manager")

    if not frontend_dir:
        frontend_dir = get_default_frontend_path()
    elif relative_pm_url:
        if frontend_dir is None:
            raise ValueError(
                "When relative_pm_url parameter is provided, frontend_dir needs to be specified."  # noqa: E501
            )
        if relative_pm_url.is_relative_to("/"):
            relative_pm_url = relative_pm_url.relative_to("/")

        shutil.copytree(
            get_default_frontend_path(),
            frontend_dir / relative_pm_url,
            dirs_exist_ok=True,
        )
    app.mount(
        "/",
        StaticFiles(
            directory=frontend_dir, html=True, follow_symlink=follow_symlink
        ),
        name="static",
    )

    # vHIL: only the origins PM_ALLOWED_ORIGINS names (upstream: "*").
    origins = allowed_origins()
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_headers=["*"],
            allow_methods=["GET"],
            max_age=None,
        )
    policy = os.environ.get("PM_CSP", "").strip()
    if policy:
        app.add_middleware(
            ContentSecurityPolicy,
            policy=policy,
            report_only=os.environ.get("PM_CSP_REPORT_ONLY", "") == "1",
        )

    return app
