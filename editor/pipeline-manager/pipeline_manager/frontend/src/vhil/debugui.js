/*
 * vHIL: the debugger in the workspace (step 17 of
 * docs/architecture/editor-workspace.md; docs/debugger.md): which board the
 * Debug tab and the inspector show, which frame, the sources fetched, and
 * the debug ops the Debug tab, the inspector, the State tab's watches and the
 * shortcuts send over the session channel (session.js `request`).
 *
 * What the debuggers did comes from the trace (replay.debug, debug.js), the
 * same for every tab on the run; only the tab that controls the session
 * sends ops, and a REPLAY sends none (it has no emulator to stop).
 */

import { reactive, watch } from 'vue';
import { call } from './api.js';
import { replay } from './replay.js';
import { live, request, cannotSend } from './session.js';
import { isExpression, parseLocation } from './debug.js';

export const dbg = reactive({
    board: '', // the board the Debug tab shows (follows the canvas, else the held one)
    frame: 0, // the stack frame shown (its source line, its locals)
    frameLocals: null, // {key, list} of a frame other than the top one
    view: 'source', // source | disasm
    disasm: null, // {key, list} for the current stop
    busy: false,
    status: '', // the last op's outcome, for the tab's aria-live line
    sources: {}, // `${board}|${path}` -> {lines, path, root} | {error}
});

/** The boards of the run on the workspace (its contract's), else none. */
export function runBoards() {
    return Object.keys(replay.contract?.boards || {});
}

/** The stop the board is held at (its `stopped` record), or null. */
export function stopOf(board) {
    replay.debugVersion; // eslint-disable-line no-unused-expressions
    return replay.debug.boards.get(board)?.stop ?? null;
}

/** Why a debug op can't be sent from here, or null. */
export function cannotDebug() {
    if (!replay.live) return 'debugging needs a LIVE session (a replay has no emulator to stop)';
    return cannotSend();
}

/** One debug op on `board`: resolves with its result, or null when it was
 *  refused (said in dbg.status). */
export async function debugOp(board, op) {
    const why = cannotDebug();
    if (why) {
        dbg.status = `Not sent: ${why}`;
        return null;
    }
    dbg.busy = true;
    try {
        const ack = await request({ kind: 'debug', board, ...op });
        dbg.status = `${board}: ${op.cmd} done`;
        return ack.result ?? true;
    } catch (e) {
        dbg.status = `${board}: ${op.cmd} refused: ${e.message}`;
        return null;
    } finally {
        dbg.busy = false;
    }
}

/** continue | next | step | finish on the held board. */
export function resume(how, board = dbg.board) {
    dbg.frame = 0;
    dbg.frameLocals = null;
    return debugOp(board, { cmd: how });
}

/** A breakpoint from what the user typed (a function, file:line, 0x…). */
export function addBreakpoint(board, text) {
    const location = parseLocation(text);
    if (!location) {
        dbg.status = `"${text}" is not a function, a file:line or an address`;
        return Promise.resolve(null);
    }
    return debugOp(board, { cmd: 'break', location });
}

/** The breakpoint gutter: set one at file:line, or clear the one there. */
export function toggleLine(board, file, line, number) {
    if (number) return debugOp(board, { cmd: 'clear', number });
    return debugOp(board, { cmd: 'break', location: { file, line } });
}

/** The board's watch list, to `exprs` (shared by the Debug and State tabs). */
export function setWatches(board, exprs) {
    const bad = exprs.find((e) => !isExpression(e));
    if (bad !== undefined) {
        dbg.status = `"${bad}" is not a variable path (names, '.', '->', '[n]', '*', '&')`;
        return Promise.resolve(null);
    }
    return debugOp(board, { cmd: 'watches', exprs });
}

/** The locals of frame `n` of the held board (the top frame's come with
 *  the stop). */
export async function pickFrame(board, n) {
    dbg.frame = n;
    dbg.frameLocals = null;
    const stop = stopOf(board);
    if (!n || !stop) return;
    const key = `${board}|${stop.t_us}|${stop.at_us}|${n}`;
    const list = await debugOp(board, { cmd: 'locals', frame: n });
    if (list && dbg.frame === n) dbg.frameLocals = { key, list };
}

/** The disassembly around the held board's pc. */
export async function loadDisasm(board) {
    const stop = stopOf(board);
    if (!stop) return;
    const key = `${board}|${stop.t_us}|${stop.at_us}`;
    if (dbg.disasm?.key === key) return;
    const list = await debugOp(board, { cmd: 'disassemble' });
    if (list) dbg.disasm = { key, list };
}

/** A firmware source file of `board` (GET /api/runs/<id>/debug/source),
 *  cached per run. */
export async function loadSource(board, path) {
    const key = `${board}|${path}`;
    if (dbg.sources[key] || !replay.id) return;
    dbg.sources[key] = { loading: true };
    try {
        const q = new URLSearchParams({ board, path });
        dbg.sources[key] = await call('GET', `/api/runs/${replay.id}/debug/source?${q}`);
    } catch (e) {
        dbg.sources[key] = { error: e.message };
    }
}

/** A new run on the workspace: nothing of the last one's. */
export function resetDebug() {
    Object.assign(dbg, {
        frame: 0, frameLocals: null, disasm: null, status: '', sources: {},
    });
}

// Another run on the workspace: its own sources and stops.
watch(() => replay.id, resetDebug);

export { live };
