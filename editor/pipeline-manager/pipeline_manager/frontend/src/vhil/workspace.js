/*
 * vHIL: the workspace's state and what its controls do (step 7 of
 * docs/architecture/editor-workspace.md; CHANGELOG-VHIL.md). This is what
 * the shell's Editor page (vhil/server/static/editor.js) did around the
 * editor before it moved in here: open a system, pick firmware refs, check,
 * commit to a branch, open a PR, and Run, a normal run of the saved system
 * through POST /api/runs (shell/editor-run.js, the shell's own copy).
 */

import { reactive, watch } from 'vue';
import { call, runPage } from './api.js';
import {
    centerOn, entryGraph, loadGraph, nodeById, nodeName, nodeOfMessage, prop,
    propValue, saveDataflow, selectNode, setProp, signature,
} from './graph.js';
import { setTheme, THEMES } from './theme.ts';
// eslint-disable-next-line import/no-unresolved, import/extensions -- copied by the build
import { frameCounts, runBlocker, runRequest } from './shell/editor-run.js';
import { terminalStore } from '../core/stores.js';
import NotificationHandler from '../core/notifications.js';

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
    { id: 'scenario', label: 'Scenario', step: 10 },
    { id: 'state', label: 'State', step: 13 },
    { id: 'bus', label: 'Bus', step: 9 },
    { id: 'signals', label: 'Signals', step: 13 },
    { id: 'log', label: 'Log' },
    { id: 'debug', label: 'Debug', step: 17 },
    { id: 'problems', label: 'Problems' },
];

const LAYOUT = {
    sidebar: true,
    view: 'palette',
    inspector: true,
    inspectorW: 320,
    dock: true,
    dockTab: 'log',
    dockH: 240,
};

export const ws = reactive({
    config: { run_virtual_ms: 3000, base_branch: 'dev', can_open_pr: false },
    systems: [],
    firmware: {}, // id -> catalogue firmware (repo, ref)
    // The open system. saved: the signature of the graph when it was opened
    // or last saved (the dirty dot); savedYaml: the file it gave then (Run
    // refuses while the graph gives another); runRef: the commit that file
    // is at ("" for the checked-out tree), which Run runs.
    id: null,
    isNew: false,
    branch: '',
    ref: '',
    runRef: '',
    saved: null,
    savedYaml: null,
    savedBranch: null,
    extra: null,
    dirty: false,
    commit: { branch: '', message: '' },
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
    picker: null, // the open ref picker: {nodeId, anchor}
    dialog: null, // 'commit' | 'pr' | 'shortcuts'
});

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

export const problemCount = (severity) => ws.problems.filter((p) => p.severity === severity).length;

// The URL names what is open, so a reload or a shared link opens it again.
function remember() {
    const u = new URL(window.location.href);
    if (ws.id && !ws.isNew) u.searchParams.set('system', ws.id); else u.searchParams.delete('system');
    if (ws.branch) u.searchParams.set('branch', ws.branch); else u.searchParams.delete('branch');
    window.history.replaceState(null, '', u);
}

const today = () => new Date().toISOString().slice(0, 10).replace(/-/g, '');

export function open(id, { branch = '', isNew = false } = {}) {
    return guarded(`could not open ${id}`, async () => {
        ws.status = `opening ${id}…`;
        const q = new URLSearchParams();
        if (branch) q.set('branch', branch);
        if (isNew) q.set('new', 'true');
        const s = await call('GET', `/api/systems/${encodeURIComponent(id)}/dataflow?${q}`);
        ws.selectedId = null;
        ws.picker = null;
        await loadGraph(s.dataflow);
        Object.assign(ws, {
            id,
            isNew: !s.exists,
            branch,
            ref: s.ref || '',
            runRef: branch ? s.ref : '',
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

// -- firmware refs --------------------------------------------------------------

// One refs request per firmware while a picker is open (the API caches
// ls-remote).
const refsCache = new Map();
export function refsOf(fwId) {
    if (!refsCache.has(fwId)) {
        const p = call('GET', `/api/firmware/${encodeURIComponent(fwId)}/refs`);
        p.catch(() => refsCache.delete(fwId));
        refsCache.set(fwId, p);
    }
    return refsCache.get(fwId);
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

export function commit({ takeover = false } = {}) {
    return guarded('Commit', async () => {
        if (!ws.id) throw new Error('open a system first');
        const body = {
            dataflow: currentGraph(), branch: ws.commit.branch.trim(), message: ws.commit.message,
        };
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
        ws.saved = signature(saveDataflow());
        ws.savedYaml = (await previewYaml()).yaml;
        refreshDirty();
        if (out.warnings?.length) setProblems([], out.warnings);
        if (out.changed) {
            ws.savedBranch = out.branch;
            ws.branch = out.branch;
            ws.ref = out.ref;
            remember();
            say(`Committed ${out.path} on ${out.branch} @ ${out.ref.slice(0, 8)}`);
        } else {
            say(`Nothing to commit: identical to ${out.ref.slice(0, 8)}`);
        }
        return out;
    });
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

export function loadRuns() {
    if (!ws.id) { ws.runs = []; return undefined; }
    return call('GET', `/api/runs?limit=25&system=${encodeURIComponent(ws.id)}`)
        .then((runs) => { ws.runs = runs; })
        .catch(() => { ws.runs = []; });
}

export const runActive = () => ws.run && !TERMINAL.has(ws.run.state);

// Follow a run over /api/runs/{id}/live until it ends: frames counted per bus
// (not the scenario's own), its log lines into the Log.
function follow(id, body) {
    const at = body.ref ? ` @ ${body.ref.slice(0, 8)}` : '';
    const fw = Object.entries(body.firmware).map(([k, v]) => `${k}=${v}`).join(', ');
    const what = `${body.system}${at}, ${body.scenario.virtual_ms} ms${fw ? `, ${fw}` : ''}`;
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
            return;
        }
        if (run.state === 'queued') {
            run.state = 'running';
            log(`run ${id} running`);
            ws.status = `Run ${id} running`;
        }
        if (rec.kind === 'log') log(`  ${(rec.t_us / 1e6).toFixed(3)} s  ${rec.text}`);
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

export function runNow() {
    return guarded('Run', async () => {
        if (runActive()) throw new Error(`run ${ws.run.id} is still ${ws.run.state}: stop it first`);
        const virtualMs = Number(ws.virtualMs);
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
            system: ws.id, ref: ws.runRef, dataflow, virtualMs,
        });
        const out = await call('POST', '/api/runs', body);
        follow(out.run_id, body);
    });
}

export function stopRun() {
    return guarded('Stop', async () => {
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
    if (id) {
        await open(id, { branch: q.get('branch') || '' });
    } else {
        // Nothing to show yet: offer the systems.
        ws.layout.sidebar = true;
        ws.layout.view = 'systems';
    }
}
