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


def test_shaped_nodes_keep_their_properties_in_the_inspector():
    """A board's, bus's and device's properties are edited in the
    inspector, not on the node (step 8: every vHIL kind is a shape)."""
    node = (FRONTEND / "src/custom/CustomNode.vue").read_text()
    assert "const inInspector = vhilType !== null;" in node
    assert "if (inInspector) return bigBuses.value;" in node


def test_node_shapes():
    """Step 8: a board card with a role band and sub-line, a bus rail with
    its name and bitrate, a device card with a count pill, an empty slot for
    the live state; the classes nodes.css draws them from."""
    node = (FRONTEND / "src/custom/CustomNode.vue").read_text()
    template = node.split("<script setup>")[0]
    for part in ('class="vhil-node-head"', 'class="vhil-node-role"', 'class="vhil-node-count"',
                 'class="vhil-node-state"', 'class="vhil-node-sub"', 'class="vhil-bus-label"',
                 'class="vhil-pin-groups"'):
        assert part in template, part
    assert "import '../vhil/nodes.css';" in node
    css = (VHIL / "nodes.css").read_text()
    for rule in (".--vhil-board", ".--vhil-bus", ".--vhil-model", "border: 1px dashed",
                 ".vhil-node.--role-ams { --vhil-role: var(--role-ams); }",
                 *(f".vhil-node.--can-{i} {{ --vhil-tint: var(--can-{i}); }}" for i in range(1, 5))):
        assert rule in css, rule
    # On the tokens only: no literal colour.
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", css)


def test_wires_by_type_bus_tint_and_tooltip():
    view = (FRONTEND / "src/custom/connection/ConnectionView.vue").read_text()
    assert "[`--t-${type}`]" in view and "var(--can-${tint.value}" in view
    assert '@pointermove="showTip"' in view and '@pointerleave="hideWireTip"' in view
    scss = (FRONTEND / "styles/_connection.scss").read_text()
    for width in ("--w: 2px;", "&.--t-can {\n        --w: 3px;", "&.--t-gpio {\n        --w: 1.5px;"):
        assert width in scss, width
    shapes = (VHIL / "shapes.js").read_text()
    # The tooltip is placed through the CSSOM, never a style attribute.
    assert "tip.style.transform" in shapes and "setAttribute('style'" not in shapes
    assert "innerHTML" not in shapes


def test_zoom_icons_follow_the_theme():
    for icon in ("Plus", "Minus", "Crosshair"):
        text = (FRONTEND / f"src/icons/{icon}.vue").read_text()
        assert "#ffffff" not in text and "stroke: $white;" in text, icon


def test_the_shells_editor_route_redirects_to_the_workspace():
    editor = (STATIC / "editor.js").read_text()
    assert 'new URL("/editor/"' in editor and "location.replace" in editor
    assert "<iframe" not in editor
    assert 'href="/editor/"' in (STATIC / "index.html").read_text()
    assert ".ed-grid" not in (STATIC / "app.css").read_text()
