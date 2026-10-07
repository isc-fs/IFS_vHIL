/*
 * vHIL: the debugger's model (step 17 of docs/architecture/editor-workspace.md;
 * docs/debugger.md). Pure, no DOM, no Vue: tests/js/debug.test.mjs runs it
 * under node.
 *
 * What a session's debuggers did is in its trace, as `debug` records (the
 * worker's: attached, breakpoints, stopped, running, detached), so every tab
 * on the run, the one that controls it and the ones that watch, and a REPLAY
 * of it, fold the same records into the same state: each board's
 * breakpoints and watches, and the stop it is held at (where, the stack,
 * locals, registers, the watches' values). The rest is how the Debug tab
 * reads a location the user types, and how a stop reads.
 */

/** "control.cpp:35" of a frame or stop ({file, line} or {func, addr}). */
export function whereText(frame) {
    if (!frame) return '';
    if (frame.file) return `${frame.file.split('/').pop()}:${frame.line}`;
    return frame.func && frame.func !== '??' ? frame.func : (frame.addr || '?');
}

const REASONS = {
    'breakpoint-hit': 'breakpoint',
    'end-stepping-range': 'step',
    'function-finished': 'finish',
    'signal-received': 'break',
};

/** "breakpoint in ecu (control.cpp:35)": a held board, as the top bar says it. */
export function heldText(at) {
    if (!at) return '';
    return `${REASONS[at.reason] || at.reason || 'stopped'} in ${at.board} (${whereText(at)})`;
}

/** The location a breakpoint field's text means: "0x0802310a" an address,
 *  "control.cpp:35" a file and line, anything else a function. Null when
 *  it is none of them (what the server refuses anyway; vhil/gdb.py). */
export function parseLocation(text) {
    const s = (text || '').trim();
    if (/^(?:\*\s*)?0x[0-9a-fA-F]{1,8}$/.test(s)) return { address: s.replace(/^\*\s*/, '') };
    const m = /^([A-Za-z0-9_][A-Za-z0-9_.+@/-]*):([0-9]{1,7})$/.exec(s);
    if (m) return { file: m[1], line: Number(m[2]) };
    if (/^[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_~][A-Za-z0-9_]*)*$/.test(s)) return { function: s };
    return null;
}

/** An expression the watch list and Eval take: a C variable path (names,
 *  '.', '->', '[n]', unary '*' and '&'), as vhil/gdb.py checks it. */
export const isExpression = (text) => /^[*&]{0,3}[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*(?:(?:\.|->)[A-Za-z_][A-Za-z0-9_]*|\[[0-9]{1,6}\])*$/
    .test((text || '').trim()) && (text || '').trim().length <= 200;

/** Whether a source file path is a breakpoint's file: the same file name,
 *  and the one path a suffix of the other (GDB gives full paths, a user may
 *  type a relative one). */
export function sameFile(a, b) {
    if (!a || !b) return false;
    return a === b || a.endsWith(`/${b}`) || b.endsWith(`/${a}`);
}

/**
 * Each board's debugger, folded from `debug` records in order (`add`): its
 * breakpoints, watches, whether attached, and its current stop or null;
 * `stops` lists every stop in the trace, for a REPLAY to show where the
 * session stopped.
 */
export class DebugState {
    constructor() {
        this.boards = new Map(); // board -> {attached, breakpoints, watches, stop}
        this.stops = []; // every stopped record, in order
    }

    board(name) {
        let b = this.boards.get(name);
        if (!b) {
            b = {
                attached: false, breakpoints: [], watches: [], stop: null,
            };
            this.boards.set(name, b);
        }
        return b;
    }

    /** One `debug` record. */
    add(rec) {
        if (rec.kind !== 'debug') return;
        if (!rec.board) { // every board: a stop of the session, or all going on
            if (rec.event === 'running') {
                this.boards.forEach((b, name) => this.boards.set(name, { ...b, stop: null }));
            }
            if (rec.event === 'detached') this.boards.clear();
            return;
        }
        const b = this.board(rec.board);
        switch (rec.event) {
            case 'attached': b.attached = true; break;
            case 'detached': this.boards.delete(rec.board); break;
            case 'breakpoints':
                b.breakpoints = rec.breakpoints || [];
                b.watches = rec.watches || [];
                break;
            case 'stopped':
                b.attached = true;
                b.stop = rec;
                this.stops.push(rec);
                break;
            case 'running': b.stop = null; break;
            default: break;
        }
    }

    /** The boards held at a stop, by name. */
    held() {
        return [...this.boards.entries()].filter(([, b]) => b.stop).map(([n]) => n);
    }

    /** The last stop at or before virtual time `t` (a REPLAY's scrubber). */
    stopAt(t) {
        let out = null;
        this.stops.some((s) => {
            if (s.t_us > t) return true;
            out = s;
            return false;
        });
        return out;
    }

    /** The breakpoint lines of `file` on `board`: Map line -> number. */
    linesOf(board, file) {
        const out = new Map();
        (this.boards.get(board)?.breakpoints || []).forEach((bp) => {
            if (bp.line && sameFile(bp.file, file)) out.set(bp.line, bp.number);
        });
        return out;
    }
}

/** The lines of a source to show around `line` (1-based): [from, to],
 *  `around` lines either side, within 1..count. */
export function sourceWindow(line, count, around = 120) {
    const at = Math.min(Math.max(1, line || 1), Math.max(1, count));
    return [Math.max(1, at - around), Math.min(count, at + around)];
}
