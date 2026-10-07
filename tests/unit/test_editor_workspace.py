"""The editor is the vHIL workspace (step 7 of
docs/architecture/editor-workspace.md; editor/pipeline-manager/CHANGELOG-VHIL.md):
Pipeline Manager's page is laid out by src/vhil/, and the shell's Editor
route only sends the browser there. The browser checks (layout, Run, the
role select, the CSP console) are in the PR that laid it out."""
import re

from vhil.system import REPO

FRONTEND = REPO / "editor/pipeline-manager/pipeline_manager/frontend"
VHIL = FRONTEND / "src/vhil"
STATIC = REPO / "vhil/server/static"


def test_the_page_is_the_workspace_not_pipeline_managers_navbar():
    home = (FRONTEND / "src/components/Home.vue").read_text()
    template = home.split("<script>")[0]
    for part in ("VhilTopBar", "VhilRail", "VhilInspector", "VhilDock", "VhilStatus"):
        assert f"<{part}" in template, part
    assert "<NavBar" not in template and "<TerminalPanel" not in template
    # The palette stays inside the canvas (its drag-and-drop injection) and
    # is shown in the sidebar.
    assert re.search(r'<Teleport to="#vhil-palette-host" defer>\s*<Palette />', template)
    assert 'id="vhil-palette-host"' in (VHIL / "VhilRail.vue").read_text()


def test_the_keyboard_map():
    keys = (VHIL / "shortcuts.js").read_text()
    for key in ("'F5'", "ev.shiftKey", "'b'", "'j'", "Digit([1-7])", "'?'"):
        assert key in keys, key


def test_run_is_the_shells_run_request():
    """One copy of what Run sends: the shell's editor-run.js, copied in by
    the image build (not in git under src/vhil/shell/)."""
    workspace = (VHIL / "workspace.js").read_text()
    assert "from './shell/editor-run.js'" in workspace
    dockerfile = (REPO / "docker/editor.Dockerfile").read_text()
    assert "vhil/server/static/editor-run.js" in dockerfile
    assert "!vhil/server/static/editor-run.js" in (
        REPO / "docker/editor.Dockerfile.dockerignore").read_text()


def test_commit_builds_a_new_branch_on_the_opened_commit():
    """As the shell's page did (#167): a commit names the commit the system
    was opened at (or last committed as) as its base, so the new branch
    runs with this deployment's code."""
    workspace = (VHIL / "workspace.js").read_text()
    assert "base: s.ref || ''" in workspace
    assert "if (ws.base) body.base = ws.base;" in workspace
    assert "ws.base = out.ref;" in workspace


def test_board_properties_live_in_the_inspector():
    node = (FRONTEND / "src/custom/CustomNode.vue").read_text()
    assert "vhilKind(props.node.type) === 'board'" in node


def test_the_shells_editor_route_redirects_to_the_workspace():
    editor = (STATIC / "editor.js").read_text()
    assert 'new URL("/editor/"' in editor and "location.replace" in editor
    assert "<iframe" not in editor
    assert 'href="/editor/"' in (STATIC / "index.html").read_text()
    assert ".ed-grid" not in (STATIC / "app.css").read_text()
