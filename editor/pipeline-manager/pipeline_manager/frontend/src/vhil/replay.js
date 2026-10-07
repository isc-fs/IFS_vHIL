/*
 * vHIL: REPLAY, a finished (or running) run's trace on the workspace (step 9
 * of docs/architecture/editor-workspace.md; CHANGELOG-VHIL.md). Opening a
 * run from Runs loads its record (GET /api/runs/<id>), its CAN contract
 * (GET /api/runs/<id>/contract: the firmware's own .def files,
 * vhil/server/decode.py) and its trace in cursor-paged pages
 * (GET /api/runs/<id>/trace, X-Trace-Cursor); the top bar's clock becomes a
 * scrubber over its virtual time, and the Bus tab shows the frames as of
 * that time.
 *
 * The frames live in a FrameStore (typed arrays), outside Vue's reactivity;
 * `replay.version` says when they changed. Live data never goes through
 * Pipeline Manager's RPC.
 *
 * Step 13: the trace's samples and edges too, and, once the contract (with
 * each board's state view) and the trace are both in, `replay.stateTrace`
 * (state.js): what the State tab, the inspector and the node pills show at
 * the scrubber's time. The worker's `bus_load` records (one per bus and
 * slice, vhil/worker.py; #174) come with them, for the Bus tab's load.
 *
 * Step 15: a live session fills the same store and model as its records
 * stream (beginLive, feedLive; session.js): `replay.live` while it runs,
 * `replay.tick` bumped per batch (the Bus tab draws the new frames without
 * folding the old ones again), the time following the session's clock.
 */

import { markRaw, reactive } from 'vue';
import { call, tracePage } from './api.js';
import { FrameStore } from './frames.js';
import { StateTrace } from './state.js';
// eslint-disable-next-line import/no-unresolved, import/extensions -- copied by the build
import { rawValue } from './shell/decode.js';

const PAGE = 50000;

export const store = markRaw(new FrameStore());

export const replay = reactive({
    id: null, // the run on the workspace, or null
    run: null, // its record
    state: 'idle', // idle | loading | ready | error
    error: '',
    loaded: 0, // frames read so far
    t: 0, // the scrubber, virtual µs
    end: 0, // the run's virtual length, µs
    contract: null, // {buses: {bus: {id: message}}} (raw, not reactive)
    contractNote: '',
    logs: [], // the run's log records
    edges: [], // its pin edges (the scenario timeline's actual levels; step 11)
    samples: [], // its symbol samples (step 13)
    stateTrace: null, // the boards' state over the run (state.js; raw), once all is in
    loads: [], // the run's bus_load records, in time order (#174)
    version: 0, // bumped when the frames change
    live: false, // a live session's records stream in (step 15)
    tick: 0, // bumped per live batch: frames appended, nothing else changed
});

let loading = 0; // the current load: a newer one cancels an older one

function reset() {
    store.clear();
    Object.assign(replay, {
        id: null,
        run: null,
        state: 'idle',
        error: '',
        loaded: 0,
        t: 0,
        end: 0,
        contract: null,
        contractNote: '',
        logs: [],
        edges: [],
        samples: [],
        stateTrace: null,
        loads: [],
        version: replay.version + 1,
        live: false,
    });
}

/** The boards' state over the run, once its contract and trace are in. */
function buildState() {
    if (replay.state !== 'ready' || !replay.contract || replay.stateTrace) return;
    const tr = new StateTrace(replay.contract, { rawOf: rawValue });
    replay.samples.forEach((r) => tr.add(r));
    replay.edges.forEach((r) => tr.add(r));
    tr.addFrames(store);
    tr.end = Math.max(tr.end, replay.end);
    replay.stateTrace = markRaw(tr);
}

/** Leaves REPLAY: no run, no frames (and a load in flight is dropped). */
export function clearReplay() {
    loading += 1;
    reset();
}

/** The contract note: which boards' contracts are missing, or conflict. */
function noteOf(c) {
    const missing = Object.entries(c.boards || {}).filter(([, b]) => b.error).map(([k]) => k);
    const parts = [];
    if (c.error) parts.push(c.error);
    if (missing.length) parts.push(`no contract for ${missing.join(', ')} (not built here)`);
    if (c.conflicts?.length) parts.push(`${c.conflicts.length} id(s) declared differently by two firmwares`);
    return parts.join('; ');
}

/**
 * Loads run `run` (its record) into REPLAY. Resolves when the trace is in;
 * `onLog(record)` gets each of its log records.
 */
export async function loadRun(run) {
    loading += 1;
    const mine = loading;
    reset();
    Object.assign(replay, {
        id: run.id, run, state: 'loading', end: run.virtual_us || 0,
    });
    // The contract decodes; a run without one still replays raw.
    call('GET', `/api/runs/${run.id}/contract`).then((c) => {
        if (mine !== loading) return;
        replay.contract = markRaw(c);
        replay.contractNote = noteOf(c);
        buildState();
        replay.version += 1;
    }).catch((e) => {
        if (mine === loading) replay.contractNote = `no contract: ${e.message}`;
    });
    // Every frame and log record, page by page (the cursor resumes where
    // the last page stopped, so each costs what it returns).
    const frames = [];
    const logs = [];
    const edges = [];
    const samples = [];
    const loads = [];
    const into = {
        log: logs, edge: edges, sample: samples, bus_load: loads,
    };
    let cursor = '';
    try {
        for (;;) {
            const q = new URLSearchParams({ kinds: 'frame,log,edge,sample,bus_load', limit: String(PAGE) });
            if (cursor) q.set('cursor', cursor);
            // eslint-disable-next-line no-await-in-loop
            const page = await tracePage(`/api/runs/${run.id}/trace?${q}`);
            if (mine !== loading) return;
            page.data.forEach((r) => (into[r.kind] || frames).push(r));
            replay.loaded = frames.length;
            if (page.data.length < PAGE || !page.cursor || page.cursor === cursor) break;
            cursor = page.cursor;
        }
    } catch (e) {
        if (mine !== loading) return;
        replay.state = 'error';
        replay.error = e.message;
        throw e;
    }
    // In virtual-time order (the worker writes a slice bus by bus).
    store.append(frames);
    const last = store.length ? store.tAt(store.length - 1) : 0;
    Object.assign(replay, {
        state: 'ready',
        end: Math.max(replay.end, last),
        t: Math.max(replay.end, last),
        logs: markRaw(logs),
        edges: markRaw(edges),
        samples: markRaw(samples),
        loads: markRaw(loads),
    });
    buildState();
    replay.version += 1;
}

// A live session's samples and edges kept for the views that list them
// (the State tab's note, the scenario overlay): the newest, at most this many
// (StateTrace keeps its own series of what the cards show).
const LIVE_KEEP = 200000;

/** Starts a live session's view of run `run`: no trace to load, its contract
 *  fetched, its records fed as they come (feedLive). */
export function beginLive(run) {
    loading += 1;
    const mine = loading;
    reset();
    Object.assign(replay, {
        id: run.id,
        run,
        state: 'ready',
        live: true,
        t: run.virtual_us || 0,
        end: run.virtual_us || 0,
        logs: markRaw([]),
        edges: markRaw([]),
        samples: markRaw([]),
        loads: markRaw([]),
    });
    call('GET', `/api/runs/${run.id}/contract`).then((c) => {
        if (mine !== loading) return;
        replay.contract = markRaw(c);
        replay.contractNote = noteOf(c);
        buildState();
        replay.version += 1;
    }).catch((e) => {
        if (mine === loading) replay.contractNote = `no contract: ${e.message}`;
    });
}

const trim = (list) => {
    if (list.length > LIVE_KEEP) list.splice(0, list.length - LIVE_KEEP / 2);
};

/** A batch of a live session's records (frames, samples, edges, logs): into
 *  the store and the boards' state. Clock and op records are session.js's. */
export function feedLive(records) {
    if (!replay.live) return;
    const frames = [];
    const tr = replay.stateTrace;
    records.forEach((r) => {
        if (r.kind === 'frame') {
            frames.push(r);
            if (tr && !r.src) tr.addFrame(r);
        } else if (r.kind === 'sample') {
            replay.samples.push(r);
            if (tr) tr.add(r);
        } else if (r.kind === 'edge') {
            replay.edges.push(r);
            if (tr) tr.add(r);
        } else if (r.kind === 'log') {
            replay.logs.push(r);
        } else if (r.kind === 'bus_load') {
            replay.loads.push(r);
        }
        if (tr && r.t_us > tr.end) tr.end = r.t_us;
    });
    trim(replay.samples);
    trim(replay.edges);
    trim(replay.logs);
    trim(replay.loads);
    if (frames.length) store.append(frames);
    replay.tick += 1;
}

/** The live session ended: what streamed stays, now a replay of the run. */
export function endLive(run) {
    if (!replay.live) return;
    replay.live = false;
    if (run) replay.run = run;
    replay.version += 1;
}

/**
 * A bus's load as of virtual time `t`: {load, peak, exact} from its last
 * bus_load record at or before `t` and the highest one so far, or null.
 */
export function loadAt(bus, t) {
    let last = null;
    let peak = 0;
    replay.loads.some((r) => {
        if (r.t_us > t) return true;
        if (r.bus === bus) {
            last = r;
            peak = Math.max(peak, r.load);
        }
        return false;
    });
    return last && { load: last.load, peak, exact: last.exact };
}

/** "1.234 s". */
export const seconds = (us) => `${(us / 1e6).toFixed(3)} s`;

/** Re-reads a run's record (its state may have moved on). */
export const runRecord = (id) => call('GET', `/api/runs/${id}`);
