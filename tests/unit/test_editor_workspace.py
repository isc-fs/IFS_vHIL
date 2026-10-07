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
    template = node.split("\n<script setup>")[0]
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
    assert "ws.mode = 'REPLAY';" in workspace and "ws.layout.dockTab = tab;" in workspace
    assert "export function openRun(id, { tab = 'bus' } = {}) {" in workspace
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


def test_the_scenario_tab():
    """Step 10: the Scenario tab edits the selected scenario's rows, frames
    through the shell's decode.js (one copy, which encodes too), checked on
    the server on each edit; Run sends its rows; Commit… commits it."""
    assert "<VhilScenario v-else-if=\"tab.id === 'scenario'\" />" in (
        VHIL / "VhilDock.vue").read_text()
    tab = (VHIL / "VhilScenario.vue").read_text() + (VHIL / "VhilScenarioRow.vue").read_text()
    assert "from './shell/decode.js'" in tab and "dec.encodeField(" in tab
    assert "v-html" not in tab and "innerHTML" not in tab
    scenarios = (VHIL / "scenarios.js").read_text()
    assert "/preview${query(c)}" in scenarios and "/contract${query(c)}" in scenarios
    assert "from './workspace.js'" not in scenarios      # bound, not imported (no cycle)
    workspace = (VHIL / "workspace.js").read_text()
    assert "system: ws.id, ref: ws.runRef, dataflow, virtualMs, scenario," in workspace
    assert "await commitScenario({" in workspace
    run = (STATIC / "editor-run.js").read_text()
    assert "Object.assign(run, { name: scenario.name, stimuli, watch, expect });" in run
    assert "export function encodeField(" in (STATIC / "decode.js").read_text()


def test_the_timeline_and_the_tests_view():
    """Step 11: the timeline draws the scenario on REPLAY's time axis (SVG
    attributes and classes, no style attribute), drags with a snap, zooms
    with Ctrl+wheel and lays the run's results over its lanes; a scenario's
    run is replayed on the Scenario tab when it ends; Tests lists them."""
    assert "<VhilTimeline :buses=\"buses\" :boards=\"boardNames\" />" in (
        VHIL / "VhilScenario.vue").read_text()
    tl = (VHIL / "VhilTimeline.vue").read_text()
    template = tl.split("<script>")[0]
    assert " style=" not in template and ":style" not in template and "v-html" not in template
    assert "snap(drag.t0 + (ev.clientX - drag.x0) / scale.value, snapMs.value)" in tl
    assert "if (!ev.ctrlKey && !ev.metaKey) return;" in tl
    assert "replay.t = Math.min(replay.end, r.tUs);" in tl
    assert "kinds: 'frame,log,edge,sample'" in (VHIL / "replay.js").read_text()
    workspace = (VHIL / "workspace.js").read_text()
    assert "openRun(id, { tab: 'scenario' });" in workspace
    rail = (VHIL / "VhilRail.vue").read_text()
    assert "id: 'tests', label: 'Tests'" in rail and "call('GET', '/api/scenarios')" in rail


def test_the_state_tab_inspector_card_and_node_pill():
    """Step 13: a card per board in the State tab (with its history), the
    selected board's in the inspector and a pill on its node, all from the
    run's trace (samples too) through state.js; status as text and glyphs;
    positions through the CSSOM (:style objects), never a style attribute."""
    dock = (VHIL / "VhilDock.vue").read_text()
    assert "<VhilState v-else-if=\"tab.id === 'state'\" />" in dock
    assert "{ id: 'state', label: 'State' }," in (VHIL / "workspace.js").read_text()
    replay = (VHIL / "replay.js").read_text()
    assert "new StateTrace(replay.contract, { rawOf: rawValue })" in replay
    assert "from './shell/decode.js'" in replay
    card = (VHIL / "VhilStateCard.vue").read_text()
    template = card.split("<script>")[0]
    assert " style=" not in template and "v-html" not in template
    for text in ("no active faults", "'■'", "'□'", "✕", "○", "stale", "for {{ duration("):
        assert text in card, text
    assert "<VhilStateCard" in (VHIL / "VhilInspector.vue").read_text()
    node = (FRONTEND / "src/custom/CustomNode.vue").read_text()
    assert '<VhilNodeState :board="node.title || node.type" />' in node
    assert "✕ fault" in (VHIL / "VhilNodeState.vue").read_text()
    for css in ("state.css", "nodes.css"):
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", (VHIL / css).read_text()), css
