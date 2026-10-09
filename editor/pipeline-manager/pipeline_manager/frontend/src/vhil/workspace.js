/*
 * vHIL: the workspace's state and what its controls do (step 7 of
 * docs/architecture/editor-workspace.md; CHANGELOG-VHIL.md). This is what
 * the shell's Editor page (vhil/server/static/editor.js) did around the
 * editor before it moved in here: open a system, pick firmware refs, check,
 * commit to a branch, open a PR, and Run, a normal run of the saved system
 * through POST /api/runs (shell/editor-run.js, the shell's own copy). Step 9
 * adds REPLAY of a run (openRun; replay.js), which the shell's #/runs/<id>
 * now opens; step 10 the system's scenarios (scenarios.js): Run runs the
 * selected one, Commit… commits it too, and its problems are listed. Step 15
 * adds LIVE: a live session of the saved system (startLive, openRun of a
 * running live run; session.js), its topology locked, PAUSED while paused,
 * REPLAY of the same records once it ends, and its recording saved as a
 * scenario (saveSession). Step 18 takes in the last of the shell's pages:
 * a pytest run of tests/sim (runPytest; its results in the Artifacts tab).
 */

import { nextTick, reactive, watch } from 'vue';
import { call, runPage } from './api.js';
import {
    centerOn, entryGraph, loadGraph, nodeById, nodeName, nodeOfMessage, prop,
    propValue, saveDataflow, selectNode, setLocked, setProp, signature,
} from './graph.js';
import { setTheme, THEMES } from './theme.ts';
import {
    beginLive, clearReplay, endLive, loadRun, replay, runRecord, store as frames,
} from './replay.js';
import * as session from './session.js';
import { recordedDoc } from './live.js';
/* eslint-disable import/no-unresolved, import/extensions -- copied by the build */
import {
    firmwareRefs, frameCounts, runBlocker, runRequest,
} from './shell/editor-run.js';
/* eslint-enable import/no-unresolved, import/extensions */
import { terminalStore } from '../core/stores.js';
import { logEntry } from './runlog.js';
import NotificationHandler from '../core/notifications.js';
import {
    bindWorkspace, clearScenario, commitScenario, edited, loadContract, loadScenarios, newScenario,
    scen, scenarioProblems, selectScenario, setResults,
} from './scenarios.js';

const TERMINAL = new Set(['passed', 'failed', 'error', 'cancelled']);
const LAYOUT_KEY = 'vhil.layout';
const THEME_KEY = 'vhil.theme';

// Per-viewer conveniences (the plan's "Layout"): kept in localStorage, which
// may be missing or refuse (private windows): then they last the page.
function stored(key) {
    try { return JSON.parse(window.localStorage.getItem(key)); } catch { return null; }
}
function store(key, value) {
    try { window.localStorage.setItem(key, JSON.stringify(value)); } catch { /* not kept */ }
}

export const DOCK_TABS = [
    // id, label, the step that fills it (none: here now)
    { id: 'scenario', label: 'Scenario' },
    { id: 'state', label: 'State' },
    { id: 'bus', label: 'Bus' },
    { id: 'signals', label: 'Signals' },
    { id: 'log', label: 'Log' },
    { id: 'debug', label: 'Debug' },
    { id: 'problems', label: 'Problems' },
    { id: 'artifacts', label: 'Artifacts' },
];

const LAYOUT = {
    sidebar: true,
    view: 'palette',
    inspector: true,
    inspectorW: 320,
    dock: true,
    dockTab: 'log',
    dockH: 240,
    logHidden: [], // the Log's hidden sources (VhilLog.vue)
};

export const ws = reactive({
    config: { run_virtual_ms: 3000, base_branch: 'dev', can_open_pr: false },
    systems: [],
    firmware: {}, // id -> catalogue firmware (repo, ref)
    // The open system. saved: the signature of the graph when it was opened
    // or last saved (the dirty dot); savedYaml: the file it gave then (Run
    // refuses while the graph gives another); runRef: the commit that file
    // is at ("" for the checked-out tree), which Run runs; base: the commit
    // the graph was opened at or last committed as, which a commit to a new
    // branch builds on, so the branch runs with this deployment's code
    // (vhil/server/systems_write.py, Save.base).
    id: null,
    isNew: false,
    branch: '',
    ref: '',
    runRef: '',
    base: '',
    saved: null,
    savedYaml: null,
    savedBranch: null,
    extra: null,
    dirty: false,
    // What Commit… commits: the system file, the selected scenario, or both.
    commit: {
        branch: '', message: '', system: true, scenario: false, scenarioMessage: '',
    },
    pr: { title: '', body: '' },
    // Check's (and opening's) findings: {severity: error|warning, text, nodeId}.
    problems: [],
    checked: false,
    run: null, // {id, state, counts, what}
    runs: [], // the open system's history
    virtualMs: 3000,
    mode: 'DESIGN',
    theme: 'dark',
    backend: 'connecting', // the editor backend (vhil.editor serve)
    status: '', // the last thing the workspace did, for the status strip
    busy: false,
    selectedId: null,
    layout: { ...LAYOUT, ...(stored(LAYOUT_KEY) || {}) },
    dialog: null, // 'commit' | 'pr' | 'shortcuts' | 'save-session' | 'pytest'
    // A pytest run's form (runPytest): the selection, its timeout, the tests
    // the server collects.
    pytest: {
        select: '', timeoutS: 3600, tests: [], error: '',
    },
});

/** LIVE or PAUSED: a live session on the workspace. */
export const isLive = () => ws.mode === 'LIVE' || ws.mode === 'PAUSED';

watch(() => ws.layout, (v) => store(LAYOUT_KEY, v), { deep: true });

// -- messages -------------------------------------------------------------------

const log = (line) => terminalStore.add(line);
export function say(text, type = 'info', details = undefined) {
    ws.status = text;
    NotificationHandler.terminalLog(type, text, details);
}

async function guarded(what, fn) {
    ws.busy = true;
    try {
        return await fn();
    } catch (e) {
        say(`${what}: ${e.message}`, 'error', e.errors && e.errors.length > 1 ? e.errors : undefined);
        return undefined;
    } finally {
        ws.busy = false;
    }
}

// -- theme ----------------------------------------------------------------------

export function applyTheme(theme, keep = true) {
    ws.theme = theme;
    setTheme(theme);
    if (keep) store(THEME_KEY, theme);
}

/** dark -> light -> auto (the browser's) -> dark. */
export function cycleTheme() {
    applyTheme(THEMES[(THEMES.indexOf(ws.theme) + 1) % THEMES.length]);
}

// -- the graph ------------------------------------------------------------------

/** The dataflow with what Pipeline Manager drops put back (the file's id,
 *  description, bench wiring and source text, from when it was opened). */
export function currentGraph() {
    const dataflow = saveDataflow();
    const g = entryGraph(dataflow);
    if (ws.extra && !g.additionalData?.vhil) {
        g.additionalData = { ...(g.additionalData || {}), vhil: ws.extra };
    }
    return dataflow;
}

/** Whether the graph differs from what was opened or last saved. */
export function refreshDirty() {
    if (!ws.id || ws.saved === null) { ws.dirty = ws.isNew; return; }
    try { ws.dirty = ws.isNew || signature(saveDataflow()) !== ws.saved; } catch { /* mid-load */ }
}

async function previewYaml() {
    const p = await call('POST', `/api/systems/${encodeURIComponent(ws.id)}/preview`, { dataflow: currentGraph() });
    return p;
}

function setProblems(errors = [], warnings = []) {
    const item = (severity) => (text) => ({
        severity, text, nodeId: nodeOfMessage(text)?.id ?? null,
    });
    ws.problems = [...errors.map(item('error')), ...warnings.map(item('warning'))];
}

/** The system's problems (Check) and the selected scenario's (its check,
 *  its last run's failed expects). */
export const allProblems = () => [...ws.problems, ...scenarioProblems()];
export const problemCount = (severity) => allProblems()
    .filter((p) => p.severity === severity).length;

// The URL names what is open (and the run in REPLAY), so a reload or a
// shared link opens it again.
function remember() {
    const u = new URL(window.location.href);
    if (ws.id && !ws.isNew) u.searchParams.set('system', ws.id); else u.searchParams.delete('system');
    if (ws.branch) u.searchParams.set('branch', ws.branch); else u.searchParams.delete('branch');
    if (replay.id) u.searchParams.set('run', String(replay.id)); else u.searchParams.delete('run');
    if (scen.name && !scen.isNew) u.searchParams.set('scenario', scen.name);
    else u.searchParams.delete('scenario');
    u.searchParams.delete('view');
    u.searchParams.delete('tab');
    window.history.replaceState(null, '', u);
}

/** Leaves REPLAY (or a live session, which goes on without this tab) for DESIGN. */
export function exitReplay() {
    if (!replay.id && ws.mode !== 'REPLAY' && !isLive()) return;
    session.leave();
    setLocked(false);
    clearReplay();
    ws.mode = 'DESIGN';
    remember();
}

// Held at a debugger stop (step 17): the Debug tab comes up, once per stop.
watch(() => session.live.held, (held, was) => {
    if (held && !was && isLive()) {
        ws.layout.dock = true;
        ws.layout.dockTab = 'debug';
    }
});

// A paused session reads PAUSED on the mode pill.
watch(() => session.live.paused, (paused) => {
    if (isLive()) ws.mode = paused ? 'PAUSED' : 'LIVE';
});

const today = () => new Date().toISOString().slice(0, 10).replace(/-/g, '');

export function open(id, {
    branch = '', isNew = false, keepReplay = false, scenario = '',
} = {}) {
    return guarded(`could not open ${id}`, async () => {
        if (!keepReplay) exitReplay();
        ws.status = `opening ${id}…`;
        const q = new URLSearchParams();
        if (branch) q.set('branch', branch);
        if (isNew) q.set('new', 'true');
        const s = await call('GET', `/api/systems/${encodeURIComponent(id)}/dataflow?${q}`);
        ws.selectedId = null;
        await loadGraph(s.dataflow);
        Object.assign(ws, {
            id,
            isNew: !s.exists,
            branch,
            ref: s.ref || '',
            runRef: branch ? s.ref : '',
            base: s.ref || '',
            savedBranch: null,
            savedYaml: null,
            extra: entryGraph(s.dataflow).additionalData?.vhil || null,
            run: null,
        });
        ws.saved = signature(saveDataflow());
        if (s.exists) ws.savedYaml = (await previewYaml()).yaml;
        ws.commit.branch = branch || ws.commit.branch || `feat/system-${id}-${today()}`;
        ws.commit.message = s.exists ? `feat(systems): update ${id}` : `feat(systems): add ${id}`;
        ws.pr.title = ws.commit.message;
        refreshDirty();
        setProblems(s.errors, s.warnings);
        ws.checked = true;
        remember();
        document.title = `${id} · IFS vHIL`;
        clearScenario();
        // eslint-disable-next-line no-use-before-define
        await openScenarios(scenario);
        const where = `${branch || 'checked-out tree'} @ ${(s.ref || '').slice(0, 8)}`;
        say(`Opened ${id}${s.exists ? '' : ' (new)'} · ${where}`);
        if (s.errors.length) {
            ws.layout.dock = true;
            ws.layout.dockTab = 'problems';
        }
        // eslint-disable-next-line no-use-before-define
        loadRuns();
    });
}

export function newSystem(id) {
    if (ws.systems.some((s) => s.id === id)) {
        say(`'${id}' exists: open it instead`, 'warning');
        return undefined;
    }
    ws.commit.branch = '';
    return open(id, { isNew: true });
}

export function check() {
    return guarded('Check', async () => {
        if (!ws.id) throw new Error('open a system first');
        const p = await previewYaml();
        setProblems(p.errors, p.warnings);
        ws.checked = true;
        ws.layout.dock = true;
        ws.layout.dockTab = 'problems';
        if (p.errors.length) say(`${ws.id} is not valid: ${p.errors.length} error(s)`, 'error');
        else say(`${ws.id} is valid${p.warnings.length ? `, with ${p.warnings.length} warning(s)` : ''}`);
        return p;
    });
}

// -- scenarios ------------------------------------------------------------------

/** The graph's firmware refs, as a run takes them (or none mid-load). */
function graphRefs() {
    try { return firmwareRefs(saveDataflow()); } catch { return {}; }
}
bindWorkspace(() => ({ id: ws.id, branch: ws.branch, fw: graphRefs() }), say);

/** Lists the open system's scenarios and loads its contract; selects
 *  `name` if it is one of them. */
async function openScenarios(name = '') {
    await loadScenarios();
    loadContract();
    // eslint-disable-next-line no-use-before-define
    if (name && scen.list.some((x) => x.name === name)) await pickScenario(name);
}

/** Selects a scenario for Run and the Scenario tab ('' for none). */
export async function pickScenario(name) {
    await selectScenario(name);
    if (scen.doc) ws.virtualMs = Number(scen.doc.virtual_ms);
    ws.commit.scenario = Boolean(scen.name);
    ws.commit.scenarioMessage = scen.name
        ? `test(scenarios): ${scen.isNew ? 'add' : 'update'} ${ws.id}/${scen.name}` : '';
    remember();
}

/** The top bar's "New scenario…": the Scenario tab open, its name field
 *  focused (VhilScenario.vue watches scen.askName). */
export function askNewScenario() {
    if (!ws.id) {
        say('Open a system first', 'warning');
        return;
    }
    ws.layout.dock = true;
    ws.layout.dockTab = 'scenario';
    scen.askName += 1;
}

/** A new scenario of the open system, selected (committed by Commit…). */
export function createScenario(name) {
    if (!ws.id) {
        say('Open a system first', 'warning');
        return;
    }
    if (!newScenario(name, Number(ws.virtualMs) || 3000)) return;
    ws.commit.scenario = true;
    ws.commit.scenarioMessage = `test(scenarios): add ${ws.id}/${name}`;
    ws.layout.dock = true;
    ws.layout.dockTab = 'scenario';
    say(`New scenario ${name}: add rows, then Commit…`);
}

// -- firmware refs --------------------------------------------------------------

// One refs request per firmware (and listing) for every board's dropdowns
// (VhilBoardControls.vue; the API caches ls-remote and the GitHub API).
// `all`: every branch, not just the active ones.
const refsCache = new Map();
export function refsOf(fwId, all = false) {
    const key = `${fwId}${all ? '?all' : ''}`;
    if (!refsCache.has(key)) {
        const p = call('GET', `/api/firmware/${encodeURIComponent(fwId)}/refs${all ? '?all=1' : ''}`);
        p.catch(() => refsCache.delete(key));
        refsCache.set(key, p);
    }
    return refsCache.get(key);
}
export const clearRefs = () => refsCache.clear();

/**
 * A board's app and bootloader: the firmware each runs (the role sets which:
 * a node property shown read-only) and the ref picked for it. "udv (not in
 * the catalogue yet)" names firmware udv all the same.
 */
export function boardFirmware(node) {
    return [
        ['app', 'firmware', 'firmware_ref', ['branches', 'tags']],
        ['bootloader', 'bootloader', 'bootloader_ref', ['tags']],
    ].filter(([, fwProp]) => prop(node, fwProp)).map(([what, fwProp, refProp, kinds]) => {
        const fwId = String(propValue(node, fwProp)).split(' ')[0];
        return {
            what,
            refProp,
            kinds,
            fwId,
            fw: ws.firmware[fwId] || null,
            ref: String(propValue(node, refProp)),
        };
    }).filter((f) => f.fwId);
}

/** Picks a ref (empty: the catalogue's, so the file keeps no ref). */
export function pickRef(node, refProp, value) {
    setProp(node, refProp, value);
    refreshDirty();
    const what = refProp === 'bootloader_ref' ? 'bootloader' : 'app';
    say(`${nodeName(node)} ${what} → ${value || 'the catalogue’s ref'}: commit to keep it`);
}

// -- commit and PR --------------------------------------------------------------

/** After a commit at `out` (system or scenario) on a branch: the workspace
 *  is on that branch, at that commit, which Run runs. */
function committedAt(out) {
    ws.runRef = out.ref;
    ws.base = out.ref;
    if (out.changed) {
        ws.savedBranch = out.branch;
        ws.branch = out.branch;
        ws.ref = out.ref;
        remember();
    }
}

/** Commits what Commit… ticked: the system file, then the scenario on the
 *  same branch (on the system's new commit). */
export function commit({ takeover = false } = {}) {
    return guarded('Commit', async () => {
        if (!ws.id) throw new Error('open a system first');
        const { system, scenario } = ws.commit;
        if (!system && !(scenario && scen.name)) throw new Error('tick what to commit');
        let out = null;
        if (system) {
            // eslint-disable-next-line no-use-before-define
            out = await commitSystem({ takeover });
            if (!out) return undefined;
        }
        if (scenario && scen.name) {
            const branch = (out?.branch || ws.commit.branch).trim();
            const sc = await commitScenario({
                branch,
                message: ws.commit.scenarioMessage || `test(scenarios): update ${ws.id}/${scen.name}`,
                base: out ? out.ref : ws.base,
                takeover,
            });
            committedAt(sc);
            say(sc.changed ? `Committed ${sc.path} on ${sc.branch} @ ${sc.ref.slice(0, 8)}`
                : `Scenario ${scen.name}: nothing to commit`);
            await loadScenarios();
            ws.commit.scenarioMessage = `test(scenarios): update ${ws.id}/${scen.name}`;
            return out || sc;
        }
        return out;
    });
}

async function commitSystem({ takeover = false } = {}) {
    const body = {
        dataflow: currentGraph(), branch: ws.commit.branch.trim(), message: ws.commit.message,
    };
    if (ws.base) body.base = ws.base;
    if (takeover) body.takeover = true;
    const send = (b) => (ws.isNew
        ? call('POST', '/api/systems', { id: ws.id, ...b })
        : call('PUT', `/api/systems/${encodeURIComponent(ws.id)}`, b));
    let out;
    try {
        out = await send(body);
    } catch (e) {
        // Another member saved this branch last (vhil/server/systems_write.py):
        // moving it is a takeover, which the server logs.
        if (e.status !== 409 || !e.detail?.takeover) throw e;
        // eslint-disable-next-line no-alert
        if (!window.confirm(`${e.message}\n\nTake the branch over? This is logged.`)) throw e;
        out = await send({ ...body, takeover: true });
    }
    ws.isNew = false;
    ws.runRef = out.ref;
    ws.base = out.ref;
    ws.saved = signature(saveDataflow());
    ws.savedYaml = (await previewYaml()).yaml;
    refreshDirty();
    if (out.warnings?.length) setProblems([], out.warnings);
    committedAt(out);
    if (out.changed) {
        say(`Committed ${out.path} on ${out.branch} @ ${out.ref.slice(0, 8)}`);
    } else {
        say(`Nothing to commit: identical to ${out.ref.slice(0, 8)}`);
    }
    return out;
}

export function openPr() {
    return guarded('Open PR', async () => {
        if (!ws.id) throw new Error('open a system first');
        const branch = ws.savedBranch || ws.commit.branch.trim();
        const out = await call('POST', `/api/systems/${encodeURIComponent(ws.id)}/pr`, { branch, title: ws.pr.title, body: ws.pr.body });
        say(`Opened ${out.url}`);
        return out;
    });
}

// -- runs -----------------------------------------------------------------------

/** The open system's runs, or every system's with none open. */
export function loadRuns() {
    const q = ws.id ? `limit=25&system=${encodeURIComponent(ws.id)}` : 'limit=50';
    return call('GET', `/api/runs?${q}`)
        .then((runs) => { ws.runs = runs; })
        .catch(() => { ws.runs = []; });
}

/**
 * REPLAY: opens run `id`'s system (as it was saved on the run's branch) if
 * another is open, loads its trace and shows its frames in the Bus tab;
 * the top bar's clock scrubs its virtual time.
 */
export function openRun(id, { tab = 'bus' } = {}) {
    return guarded(`could not open run ${id}`, async () => {
        const run = await runRecord(id);
        if (run.system !== ws.id) {
            await open(run.system, { branch: run.ref_name || '', keepReplay: true });
        }
        if (run.scenario?.live && !TERMINAL.has(run.state)) {
            // eslint-disable-next-line no-use-before-define
            enterLive(run);
            return;
        }
        session.leave();
        setLocked(false);
        ws.mode = 'REPLAY';
        ws.layout.dock = true;
        // A pytest run's results are its JUnit and output, not its frames.
        ws.layout.dockTab = run.scenario?.kind === 'pytest' && tab === 'bus' ? 'artifacts' : tab;
        // A scenario's run: the scenario selected, its expect results on it.
        const name = run.scenario?.name;
        if (name && scen.name !== name && scen.list.some((s) => s.name === name)) {
            await pickScenario(name);
        }
        if (name) setResults(run);
        say(`Replaying run ${id}: loading its trace…`);
        const loaded = loadRun(run);
        remember();
        document.title = `run ${id} · ${run.system} · IFS vHIL`;
        await loaded;
        if (replay.id !== run.id) return;
        const buses = {};
        for (let i = 0; i < frames.length; i += 1) {
            const b = frames.busName(i);
            buses[b] = (buses[b] || 0) + 1;
        }
        log(`run ${id} replayed (${run.state}): ${frameCounts(buses) || 'no frames'}`);
        replay.logs.forEach((r) => log(logEntry(r)));
        const over = (replay.end / 1e6).toFixed(3);
        say(`Replaying run ${id} (${run.state}): ${frames.length} frames over ${over} s`);
    });
}

export const runActive = () => ws.run && !TERMINAL.has(ws.run.state);

// -- LIVE ---------------------------------------------------------------------------

/** The live session ended (stopped, idle, cancelled): what it streamed stays,
 *  now a REPLAY of the run, whose clock scrubs it. */
async function liveEnded(state) {
    setLocked(false);
    let run = null;
    try { run = await runRecord(replay.id); } catch { /* the record as it was */ }
    endLive(run);
    ws.mode = 'REPLAY';
    document.title = `run ${replay.id} · ${ws.id} · IFS vHIL`;
    if (ws.run && ws.run.id === replay.id) ws.run.state = state;
    const s = run?.summary || {};
    const why = { op: 'stopped', idle: 'stopped: idle', end: 'reached its cap' }[s.stopped] || state;
    say(`Live session ${replay.id} ${why}: replaying it`);
    NotificationHandler.showToast(state === 'passed' ? 'info' : 'warning',
        `Live session ${replay.id} ${why}`);
    loadRuns();
}

/** LIVE on run `run` (a running live session): its records stream into the
 *  Bus, State and Log; this tab controls it if the server gives it control. */
function enterLive(run) {
    beginLive(run);
    ws.mode = 'LIVE';
    setLocked(true);
    ws.layout.dock = true;
    if (!['bus', 'state'].includes(ws.layout.dockTab)) ws.layout.dockTab = 'state';
    session.connect(run, {
        control: true, log, say, onEnd: liveEnded,
    });
    remember();
    document.title = `LIVE run ${run.id} · ${run.system} · IFS vHIL`;
    say(`Live session ${run.id} on ${run.system}: the boards boot through their bootloaders first`);
}

/** Starts a live session of the saved system (as Run starts a run). */
export function startLive() {
    return guarded('Live', async () => {
        if (isLive()) throw new Error(`live session ${replay.id} is on: stop it first`);
        if (runActive()) throw new Error(`run ${ws.run.id} is still ${ws.run.state}: stop it first`);
        let dataflow = null;
        let current = null;
        if (ws.id && !ws.isNew) {
            dataflow = currentGraph();
            current = (await previewYaml()).yaml;
        }
        const why = runBlocker({
            id: ws.id, isNew: ws.isNew, saved: ws.savedYaml, current, virtualMs: 1,
        });
        if (why) {
            say('No live session', 'warning', why.replace('Run then runs', 'Live then runs'));
            return;
        }
        const body = runRequest({
            system: ws.id, ref: ws.runRef, dataflow, virtualMs: 1,
        });
        // Open-ended: the server caps it (VHIL_MAX_LIVE_MS).
        body.scenario = { kind: 'run', live: true };
        let out;
        try {
            out = await call('POST', '/api/runs', body);
        } catch (e) {
            say('Live session refused', 'error', e.errors || [e.message]);
            return;
        }
        exitReplay();
        log(`live session ${out.run_id} queued: ${body.system}${body.ref ? ` @ ${body.ref.slice(0, 8)}` : ''}`);
        enterLive(await runRecord(out.run_id));
        loadRuns();
    });
}

/** Pause, resume, stop or keep alive the live session. */
export const pauseLive = () => session.send({ kind: session.live.paused ? 'resume' : 'pause' });
export const stopLive = () => session.send({ kind: 'stop' });
export const keepAlive = () => session.send({ kind: 'keepalive' });

/** "Save session as scenario": the session's applied ops as a new scenario of
 *  the open system, in the Scenario tab; Commit… saves it. */
export function saveSession(name) {
    return guarded('Save session', async () => {
        if (!replay.id) throw new Error('no live session on the workspace');
        const out = await call('GET', `/api/runs/${replay.id}/session/scenario`);
        if (!newScenario(name, out.scenario.virtual_ms)) return false;
        Object.assign(scen.doc, recordedDoc(out.scenario));
        edited();
        ws.commit.scenario = true;
        ws.commit.scenarioMessage = `test(scenarios): add ${ws.id}/${name}`;
        ws.layout.dock = true;
        ws.layout.dockTab = 'scenario';
        remember();
        say(`Scenario ${name}: ${out.ops} op(s) of session ${replay.id}`
            + `${out.final ? '' : ' so far'}; Run replays it, Commit… saves it`);
        return true;
    });
}

// Follow a run over /api/runs/{id}/live until it ends: frames counted per bus
// (not the scenario's own), its log lines into the Log.
function follow(id, body) {
    const at = body.ref ? ` @ ${body.ref.slice(0, 8)}` : '';
    const fw = Object.entries(body.firmware || {}).map(([k, v]) => `${k}=${v}`).join(', ');
    const length = body.scenario.kind === 'pytest' ? `pytest ${body.scenario.select}`
        : `${body.scenario.virtual_ms} ms`;
    const what = `${body.system}${at}, ${length}${fw ? `, ${fw}` : ''}`;
    const run = reactive({
        id, state: 'queued', counts: {}, what,
    });
    ws.run = run;
    log(`run ${id} queued: ${what} (${window.location.origin}${runPage(id)})`);
    say(`Run ${id} queued`);
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const socket = new WebSocket(`${proto}://${window.location.host}/api/runs/${id}/live?kinds=frame,log`);
    const counts = {};
    let painted = 0;
    let ended = false;
    const paint = (force = false) => {
        if (!force && Date.now() - painted < 500) return;
        painted = Date.now();
        run.counts = { ...counts };
    };
    socket.onmessage = (ev) => {
        const rec = JSON.parse(ev.data);
        if (rec.kind === 'end') {
            ended = true;
            run.state = rec.state || 'unknown';
            paint(true);
            log(`run ${id} ${run.state}: ${frameCounts(counts)}`);
            const type = { passed: 'info', cancelled: 'warning' }[run.state] || 'error';
            NotificationHandler.showToast(type, `Run ${id} ${run.state}: ${frameCounts(counts)}`);
            ws.status = `Run ${id} ${run.state}`;
            loadRuns();
            // eslint-disable-next-line no-use-before-define
            if (body.scenario.name) scenarioRunEnded(id);
            // A pytest run's results are its JUnit and output: the Artifacts tab.
            else if (body.scenario.kind === 'pytest' && ws.mode !== 'REPLAY' && !isLive()) {
                openRun(id, { tab: 'artifacts' });
            }
            return;
        }
        if (run.state === 'queued') {
            run.state = 'running';
            log(`run ${id} running`);
            ws.status = `Run ${id} running`;
        }
        if (rec.kind === 'log') log(logEntry(rec));
        else if (!rec.src) counts[rec.bus] = (counts[rec.bus] || 0) + 1;
        paint();
    };
    socket.onclose = () => {
        if (ended) return;
        run.state = 'unknown';
        say(`Run ${id}: live view lost, see its page`, 'warning');
    };
    loadRuns();
}

/** A scenario's run ended: its expect results on the rows and in Problems. */
async function scenarioRunEnded(id) {
    try {
        const run = await runRecord(id);
        setResults(run);
        loadScenarios();
        if (ws.mode !== 'REPLAY') openRun(id, { tab: 'scenario' });
        const s = run.summary || {};
        if (s.expects) {
            const failed = s.expects_failed || 0;
            log(`run ${id}: ${s.expects_passed} expect(s) passed, ${failed} failed`);
            s.expects.forEach((r) => log(`  ${r.passed ? '✓ pass' : '✕ FAIL'}  ${r.name || r.check} `
                + `${r.signal}: ${r.detail}`));
        }
    } catch (e) {
        say(`Run ${id}: no results (${e.message})`, 'warning');
    }
}

export function runNow() {
    return guarded('Run', async () => {
        if (isLive()) throw new Error(`live session ${replay.id} is on: stop it first`);
        if (runActive()) throw new Error(`run ${ws.run.id} is still ${ws.run.state}: stop it first`);
        exitReplay();
        const scenario = scen.name && scen.doc ? { name: scen.name, doc: scen.doc } : null;
        if (scenario && scen.errors.length) {
            say(`Not run: scenario ${scen.name} has ${scen.errors.length} error(s)`, 'warning', scen.errors);
            return;
        }
        const virtualMs = Number(scenario ? scenario.doc.virtual_ms : ws.virtualMs);
        let dataflow = null;
        let current = null;
        if (ws.id && !ws.isNew) {
            dataflow = currentGraph();
            current = (await previewYaml()).yaml;
        }
        const why = runBlocker({
            id: ws.id, isNew: ws.isNew, saved: ws.savedYaml, current, virtualMs,
        });
        if (why) {
            say('Not run', 'warning', why);
            return;
        }
        const body = runRequest({
            system: ws.id, ref: ws.runRef, dataflow, virtualMs, scenario,
        });
        let out;
        try {
            out = await call('POST', '/api/runs', body);
        } catch (e) {
            // The API says why (e.g. a branch whose code differs from this
            // deployment's: vhil/server/runs.py).
            say('Run refused', 'error', e.errors || [e.message]);
            return;
        }
        follow(out.run_id, body);
    });
}

/** The test files and node ids a pytest run can select (GET /api/tests,
 *  tests/sim as checked out, cached on the server), once. */
let testList = null;
export function loadTests() {
    if (!testList) {
        testList = call('GET', '/api/tests').then((t) => {
            ws.pytest.tests = [...t.files, ...t.tests];
            ws.pytest.error = t.error || '';
        }).catch((e) => {
            testList = null;
            ws.pytest.error = e.message;
        });
    }
    return testList;
}

/**
 * Runs native tests (tests/sim) against the open system's firmware: a
 * `pytest` run (what the shell's Runs form started). It reads the checked-out
 * tests and systems, so no ref: HEAD only (vhil/server/runs.py). Its log
 * streams into the Log; when it ends, its JUnit and output are in the
 * Artifacts tab.
 */
export function runPytest() {
    return guarded('pytest', async () => {
        if (isLive()) throw new Error(`live session ${replay.id} is on: stop it first`);
        if (runActive()) throw new Error(`run ${ws.run.id} is still ${ws.run.state}: stop it first`);
        const tests = ws.pytest.select.trim();
        if (!ws.id || ws.isNew) throw new Error('open a saved system first');
        if (!tests) throw new Error('name a test file or node id under tests/sim');
        const body = {
            system: ws.id,
            scenario: {
                kind: 'pytest', select: tests, timeout_s: Number(ws.pytest.timeoutS) || 3600,
            },
        };
        let out;
        try {
            out = await call('POST', '/api/runs', body);
        } catch (e) {
            say('pytest run refused', 'error', e.errors || [e.message]);
            return false;
        }
        exitReplay();
        follow(out.run_id, body);
        return true;
    });
}

export function stopRun() {
    return guarded('Stop', async () => {
        if (isLive()) {
            stopLive();
            return;
        }
        if (!runActive()) throw new Error('no run in progress');
        await call('POST', `/api/runs/${ws.run.id}/cancel`);
        say(`Run ${ws.run.id}: stop requested`);
    });
}

// -- selection ------------------------------------------------------------------

// The canvas element, to centre a node in (not reactive: a DOM element).
let canvasEl = null;
export const setCanvas = (el) => { canvasEl = el; };

export function select(nodeId, { center = false } = {}) {
    const node = nodeId ? nodeById(nodeId) : null;
    selectNode(node);
    ws.selectedId = node?.id ?? null;
    if (node && center) centerOn(node, canvasEl);
}

/** A board's firmware chip (top bar): the board selected and centred, its
 *  firmware dropdown (VhilBoardControls.vue) focused, else its role's. */
export async function showBoard(nodeId) {
    select(nodeId, { center: true });
    await nextTick();
    const el = document.querySelector(`.baklava-node[data-node-id="${CSS.escape(nodeId)}"]`);
    const control = el?.querySelector('.vhil-board-row.--app select')
        ?? el?.querySelector('.vhil-board-controls select');
    control?.focus({ preventScroll: true });
}

// -- start ----------------------------------------------------------------------

export async function start() {
    document.title = 'IFS vHIL';
    const q = new URLSearchParams(window.location.search);
    if (!q.has('theme')) {
        const saved = stored(THEME_KEY);
        if (THEMES.includes(saved)) applyTheme(saved, false);
    } else {
        ws.theme = document.documentElement.dataset.theme || 'auto';
    }
    try {
        const [config, systems, firmware] = await Promise.all([
            call('GET', '/api/config'), call('GET', '/api/systems'), call('GET', '/api/firmware')]);
        ws.config = config;
        ws.virtualMs = Number(config.run_virtual_ms);
        ws.systems = systems;
        ws.firmware = Object.fromEntries(firmware.map((f) => [f.id, f]));
    } catch (e) {
        say(`The vHIL API did not answer: ${e.message}`, 'error');
        return;
    }
    const id = q.get('system');
    const run = Number(q.get('run'));
    if (id) {
        await open(id, {
            branch: q.get('branch') || '', keepReplay: true, scenario: q.get('scenario') || '',
        });
    }
    if (run > 0) {
        // ?tab=: the dock tab to replay it on (the shell's old run-page links
        // name one: vhil/server/static/app.js).
        const tab = DOCK_TABS.some((t) => t.id === q.get('tab')) ? q.get('tab') : 'bus';
        await openRun(run, { tab });
    } else if (!id) {
        // Nothing to show yet: offer the systems (or the runs, from the
        // shell's #/runs).
        ws.layout.sidebar = true;
        ws.layout.view = q.get('view') === 'runs' ? 'runs' : 'systems';
        if (ws.layout.view === 'runs') loadRuns();
    }
}
