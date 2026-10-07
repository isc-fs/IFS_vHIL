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


def test_the_shells_runs_and_systems_redirect_into_the_workspace():
    """Step 9: #/runs/<id> opens REPLAY, #/runs the Runs view, #/systems[/<id>]
    a system; the shell's own pages stay under #/classic/ (a run's signals
    and artifacts, which REPLAY doesn't show yet)."""
    app = (STATIC / "app.js").read_text()
    assert 'editorPage(view, "", { run: Number(arg) })' in app
    assert 'editorPage(view, "", { view: "runs" })' in app
    assert "systems: (arg) => editorPage(view, arg)" in app
    assert 'if (parts[1] !== "classic")' in app
    editor = (STATIC / "editor.js").read_text()
    assert 'u.searchParams.set("run", String(run))' in editor
    inspect = (STATIC / "inspect.js").read_text()
    assert "`#/classic/runs/${id}/${t}`" in inspect and 'href="/editor/?run=' in inspect
    assert "#/classic/runs/" in (STATIC / "runs.js").read_text()
    assert "`/#/classic/runs/${id}`" in (VHIL / "api.js").read_text()
    workspace = (VHIL / "workspace.js").read_text()
    assert "const run = Number(q.get('run'));" in workspace and "await openRun(run);" in workspace


def test_replay():
    """Opening a run enters REPLAY: the mode, the cursor-paged trace, the
    scrubber in the top bar, the Bus tab."""
    workspace = (VHIL / "workspace.js").read_text()
    assert "ws.mode = 'REPLAY';" in workspace and "ws.layout.dockTab = 'bus';" in workspace
    assert "{ id: 'bus', label: 'Bus' }," in workspace
    replay = (VHIL / "replay.js").read_text()
    assert "/api/runs/${run.id}/trace?" in replay and "q.set('cursor', cursor)" in replay
    assert "/api/runs/${run.id}/contract" in replay
    top = (VHIL / "VhilTopBar.vue").read_text()
    assert 'type="range"' in top and 'v-model.number="replay.t"' in top
    assert "<VhilBus v-else-if=\"tab.id === 'bus'\" />" in (VHIL / "VhilDock.vue").read_text()
    assert '@click="openRun(r.id)"' in (VHIL / "VhilRail.vue").read_text()


def test_the_bus_tab_reuses_the_shells_table_and_decoder():
    """One copy of vtable.js and decode.js: the image build copies them into
    src/vhil/shell/ (and the editor CI job compares the copies); no fork."""
    assert "from './shell/decode.js'" in (VHIL / "VhilBus.vue").read_text()
    assert "from './shell/vtable.js'" in (VHIL / "rowtable.js").read_text()
    for name in ("vtable.js", "decode.js"):
        assert not (VHIL / name).exists() and not (VHIL / "shell" / name).is_symlink()
        assert f"vhil/server/static/{name}" in (REPO / "docker/editor.Dockerfile").read_text()
        assert f"!vhil/server/static/{name}" in (
            REPO / "docker/editor.Dockerfile.dockerignore").read_text()
    ci = (REPO / ".github/workflows/editor.yml").read_text()
    assert "for f in tokens.css editor-run.js vtable.js decode.js; do" in ci
    assert 'cmp "/opt/pm/pipeline_manager/frontend/src/vhil/shell/$f" "vhil/server/static/$f"' in ci


def test_the_bus_tables_stay_fast_and_csp_clean():
    """Rows reused and filled as text (no parsed HTML, no style attribute),
    drawn once per animation frame; only the rows in view decoded."""
    table = (VHIL / "rowtable.js").read_text()
    assert "requestAnimationFrame" in table and "innerHTML" not in table
    assert "this.fill(i, row)" in table and "this.pool" in table
    bus = (VHIL / "VhilBus.vue").read_text()
    assert "innerHTML" not in bus and "v-html" not in bus
    assert "requestAnimationFrame(draw)" in bus and "decoded.get(i)" in bus
    frames = (VHIL / "frames.js").read_text()
    assert "new Float64Array(capacity)" in frames and "this.dropped += 1;" in frames
