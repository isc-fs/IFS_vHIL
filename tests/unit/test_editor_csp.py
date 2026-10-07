"""The editor runs on the web app's origin under one strict CSP (step 6 of
docs/architecture/editor-workspace.md; editor/pipeline-manager/CHANGELOG-VHIL.md):
`script-src 'self'` and `style-src 'self'`, so nothing compiles code at run
time and nothing writes inline styles, and it talks only to its own origin.

The source checks run everywhere; the built-bundle checks where a built
Pipeline Manager is ($PM_DIR: the editor image, where the editor CI job runs
this file). The browser check (deploy/smoke.sh's stack, the console free of
CSP reports) is in the PR that enforced the policy."""
import importlib.util
import os
import re
from pathlib import Path

import pytest

from vhil.system import REPO

# vhil/server/security.py by path: the editor image's CI job has no FastAPI,
# which importing the vhil.server package needs.
_spec = importlib.util.spec_from_file_location("security", REPO / "vhil/server/security.py")
_security = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_security)
EDITOR_CSP = _security.EDITOR_CSP

PM = REPO / "editor/pipeline-manager/pipeline_manager"
SRC = PM / "frontend/src"
BUILT = Path(os.environ.get("PM_DIR", "/nonexistent")) / "pipeline_manager/frontend/dist"


def sources(*suffixes):
    for path in SRC.rglob("*"):
        if path.suffix in suffixes and "validators" not in path.parts and path.is_file():
            yield path, path.read_text()


def test_the_policy_allows_nothing_unsafe():
    directives = dict(d.split(" ", 1) for d in EDITOR_CSP.split("; "))
    for name in ("default-src", "script-src", "style-src", "connect-src"):
        assert directives[name] == "'self'", name
    assert directives["frame-ancestors"] == "'self'"
    assert "unsafe" not in EDITOR_CSP and "*" not in EDITOR_CSP


def test_no_validator_is_compiled_in_the_browser():
    """Ajv compiles with `new Function`: only the build script may use it."""
    for path, text in sources(".js", ".ts", ".vue"):
        assert not re.search(r"""\bnew Ajv|from ['"]ajv|\bajv\.compile\(""", text), path


def test_the_terminal_is_a_plain_log():
    """hterm wrote inline styles; the log view writes none."""
    assert not (SRC / "third-party/hterm_all.js").exists()
    for path, text in sources(".js", ".ts", ".vue"):
        assert "hterm_all" not in text, path
    log = (SRC / "vhil/LogView.vue").read_text()
    assert "v-html" not in log and "innerHTML" not in log


def test_no_html_string_carries_a_style_attribute():
    """HTML built in code (v-html, innerHTML) is parsed by the browser, and
    a style attribute in it is refused (custom/CustomNode.vue's subtitle
    was). A template's own style bindings are set through the CSSOM: fine."""
    for path, text in sources(".js", ".ts", ".vue"):
        assert not re.search(r"`[^`\n]*(?<![:\w-])style=[\"'][^`\n]*`", text), path


def test_post_message_names_its_origin():
    for path, text in sources(".js", ".ts", ".vue"):
        assert not re.search(r"postMessage\([^;]*,\s*'\*'\s*\)", text), path
    editor = (SRC / "custom/Editor.vue").read_text()
    assert "event.origin !== window.location.origin" in editor


def test_the_backend_allows_no_other_origin_by_default():
    fastapi = (PM / "backend/fastapi.py").read_text()
    socketio = (PM / "backend/socketio.py").read_text()
    assert 'allow_origins=["*"]' not in fastapi
    assert 'cors_allowed_origins="*"' not in socketio


def test_socket_io_follows_the_page_under_its_prefix():
    backend = (SRC / "core/communication/externalApp/backend.ts").read_text()
    assert "new URL('socket.io', document.baseURI).pathname" in backend


@pytest.mark.skipif(not BUILT.is_dir(), reason="no built Pipeline Manager ($PM_DIR: the editor image)")
def test_the_built_page_has_no_inline_script_or_style():
    html = (BUILT / "index.html").read_text()
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "an inline <script>"
    assert "<style" not in html and "style=" not in html


@pytest.mark.skipif(not BUILT.is_dir(), reason="no built Pipeline Manager ($PM_DIR: the editor image)")
def test_the_bundle_generates_no_code():
    """No eval, and no Function() but webpack's `Function("return this")`
    global fallback, which a browser with globalThis never reaches."""
    for path in sorted((BUILT / "js").glob("*.js")):
        text = path.read_text()
        assert not re.search(r"(?<![\w$.])eval\(", text), path.name
        calls = re.findall(r"(?<![\w$.])Function\(([^)]{0,40})\)", text)
        assert all(c == '"return this"' for c in calls), (path.name, calls)
