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
 * Pipeline Manager's RPC. The worker's `bus_load` records (one per bus and
 * slice, vhil/worker.py; #174) come with them, for the Bus tab's load.
 */

import { markRaw, reactive } from 'vue';
import { call, tracePage } from './api.js';
import { FrameStore } from './frames.js';

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
    loads: [], // the run's bus_load records, in time order
    version: 0, // bumped when the frames change
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
        loads: [],
        version: replay.version + 1,
    });
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
        replay.version += 1;
    }).catch((e) => {
        if (mine === loading) replay.contractNote = `no contract: ${e.message}`;
    });
    // Every frame and log record, page by page (the cursor resumes where
    // the last page stopped, so each costs what it returns).
    const frames = [];
    const logs = [];
    const loads = [];
    const sink = { log: logs, bus_load: loads };
    let cursor = '';
    try {
        for (;;) {
            const q = new URLSearchParams({ kinds: 'frame,log,bus_load', limit: String(PAGE) });
            if (cursor) q.set('cursor', cursor);
            // eslint-disable-next-line no-await-in-loop
            const page = await tracePage(`/api/runs/${run.id}/trace?${q}`);
            if (mine !== loading) return;
            page.data.forEach((r) => (sink[r.kind] ?? frames).push(r));
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
        loads: markRaw(loads),
        version: replay.version + 1,
    });
}

/**
 * A bus's load as of virtual time `t`: {load, peak, exact} from its last
 * bus_load record at or before `t` and the highest one so far, or null.
 */
export function loadAt(bus, t) {
    let last = null;
    let peak = 0;
    for (const r of replay.loads) {
        if (r.t_us > t) break;
        if (r.bus === bus) {
            last = r;
            peak = Math.max(peak, r.load);
        }
    }
    return last && { load: last.load, peak, exact: last.exact };
}

/** "1.234 s". */
export const seconds = (us) => `${(us / 1e6).toFixed(3)} s`;

/** Re-reads a run's record (its state may have moved on). */
export const runRecord = (id) => call('GET', `/api/runs/${id}`);
