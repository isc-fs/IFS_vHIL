/*
 * vHIL: a live session's model (step 15 of
 * docs/architecture/editor-workspace.md; docs/live-session.md). Pure, no
 * DOM, no Vue: tests/js/live.test.mjs runs it under node.
 *
 * What a session has done is in its trace, as `op` records (the worker's,
 * at the virtual time each took effect), so every tab on the run, the one
 * that controls it and the ones that watch, folds the same records into the
 * same state: the periodic senders running, each input's last level or
 * voltage. BusRates counts each bus's frames over the last second of virtual
 * time, for the wires' frames/s. The rest is how an op reads, and the op a
 * Send panel or a pin switch makes.
 */

/* eslint-disable max-classes-per-file -- a session's state, and its buses' rates */

export const STIMULI = ['can_send', 'can_periodic', 'stop_periodic', 'gpio', 'analog', 'watch'];

const hex = (n) => `0x${Number(n).toString(16).toUpperCase()}`;
const spaced = (data) => (data || '').match(/../g)?.join(' ') ?? '';

/** How an op reads in the log and the lists: "gpio ams.PF9 = HIGH". */
export function opText(op) {
    switch (op?.kind) {
        case 'can_send':
            return `send ${op.bus} ${hex(op.id)}${op.ext ? 'x' : ''} [${spaced(op.data)}]`;
        case 'can_periodic':
            return `periodic ${op.name} ${op.bus} ${hex(op.id)}${op.ext ? 'x' : ''} [${spaced(op.data)}] every ${op.period_ms} ms`;
        case 'stop_periodic': return `stop periodic ${op.periodic}`;
        case 'gpio': return `gpio ${op.board}.${op.pin} = ${op.level ? 'HIGH' : 'LOW'}`;
        case 'analog': return `analog ${op.board}.${op.pin} = ${op.volts} V`;
        case 'watch': return op.symbol ? `watch ${op.board}.${op.symbol}` : `watch pin ${op.board}.${op.pin}`;
        default: return op?.kind ?? '?';
    }
}

/**
 * What a session's applied ops leave in force, folded from its trace's
 * `op` records in order: the periodic senders running (by name), each
 * input's last level or voltage ("ams.PF9"), whether it is paused, and the
 * periodic names used (a name runs once a session: docs/live-session.md).
 */
export class SessionState {
    constructor() {
        this.periodics = new Map(); // name -> {name, bus, id, ext, data, period_ms, since}
        this.levels = new Map(); // "board.pin" -> true | false
        this.volts = new Map(); // "board.pin" -> V
        this.used = new Set();
        this.paused = false;
        this.applied = 0;
        this.refused = 0;
    }

    /** One `op` record ({t_us, op_id, op, status}). */
    add(rec) {
        if (rec.status !== 'applied') {
            this.refused += 1;
            return;
        }
        this.applied += 1;
        const { op } = rec;
        switch (op.kind) {
            case 'can_periodic':
                this.used.add(op.name);
                this.periodics.set(op.name, {
                    name: op.name,
                    bus: op.bus,
                    id: op.id,
                    ext: !!op.ext,
                    data: op.data || '',
                    period_ms: op.period_ms,
                    since: rec.t_us,
                });
                break;
            case 'stop_periodic': this.periodics.delete(op.periodic); break;
            case 'gpio': this.levels.set(`${op.board}.${op.pin}`, !!op.level); break;
            case 'analog': this.volts.set(`${op.board}.${op.pin}`, op.volts); break;
            case 'pause': this.paused = true; break;
            case 'resume': this.paused = false; break;
            default: break;
        }
    }

    /** The periodic senders running, by bus then id. */
    running() {
        return [...this.periodics.values()].sort((a, b) => {
            if (a.bus !== b.bus) return a.bus < b.bus ? -1 : 1;
            return a.id - b.id;
        });
    }
}

/** A name for a new periodic of `id` none has had this session: "p100",
 *  then "p100-2", … (`taken`: a Set or an array of names). */
export function periodicName(taken, id) {
    const has = (n) => (taken instanceof Set ? taken.has(n) : taken.includes(n));
    const stem = `p${Number(id).toString(16)}`;
    if (!has(stem)) return stem;
    for (let i = 2; ; i += 1) if (!has(`${stem}-${i}`)) return `${stem}-${i}`;
}

/** The op a Send panel makes: a frame once, or every `period_ms` as `name`. */
export function sendOp({
    bus, id, ext = false, data = '', periodMs = null, name = '',
}) {
    const op = {
        kind: periodMs ? 'can_periodic' : 'can_send', bus, id: Number(id), data,
    };
    if (ext) op.ext = true;
    if (periodMs) Object.assign(op, { name, period_ms: Number(periodMs) });
    return op;
}

/** Why a Send panel's frame can't go yet, or null. A period under 5 ms
 *  asks to confirm (`confirm`), as the plan says a 1 ms periodic should. */
export function sendProblem({
    bus, id, ext = false, data = '', periodMs = null,
}) {
    if (!bus) return 'pick a bus';
    if (!Number.isInteger(id) || id < 0) return 'the id is not a number';
    if (!ext && id > 0x7ff) return `id ${hex(id)} needs an extended id`;
    if (id > 0x1fffffff) return 'an id is at most 29 bits';
    if (!/^([0-9a-f]{2})*$/.test(data) || data.length > 128) return 'data must be hex, at most 64 bytes';
    if (periodMs !== null && !(periodMs > 0 && periodMs <= 60000)) return 'the period is 0..60000 ms';
    return null;
}
export const FAST_PERIOD_MS = 5;

/**
 * Frames per second of each bus over the last `windowUs` of virtual time
 * (the wires' labels, the dash march on a bus carrying traffic).
 */
export class BusRates {
    constructor(windowUs = 1e6) {
        this.windowUs = windowUs;
        this.times = new Map(); // bus -> {t: [], head}
    }

    add(bus, t) {
        let q = this.times.get(bus);
        if (!q) {
            q = { t: [], head: 0 };
            this.times.set(bus, q);
        }
        q.t.push(t);
    }

    /** {bus: frames/s} as of virtual time `now`. */
    rates(now) {
        const out = {};
        const from = now - this.windowUs;
        this.times.forEach((q, bus) => {
            let { head } = q;
            while (head < q.t.length && q.t[head] <= from) head += 1;
            if (head > 4096 && head > q.t.length / 2) { // compact now and then
                this.times.set(bus, { t: q.t.slice(head), head: 0 });
                head = 0;
            } else {
                this.times.set(bus, { t: q.t, head });
            }
            out[bus] = Math.round(((this.times.get(bus).t.length - head) * 1e6) / this.windowUs);
        });
        return out;
    }

    clear() { this.times.clear(); }
}

/** "t=12.345 s · RTF 0.98×" (no RTF before the first clock). */
export function clockText(tUs, rtf = null, paused = false) {
    const t = `t=${(tUs / 1e6).toFixed(3)} s`;
    if (paused) return `${t} · paused`;
    return rtf === null ? t : `${t} · RTF ${rtf.toFixed(2)}×`;
}

/** "29 min 59 s" of an idle countdown, "" when not close (over 5 min). */
export function idleText(seconds) {
    if (seconds === null || seconds === undefined || seconds > 300) return '';
    const s = Math.max(0, Math.round(seconds));
    return s >= 60 ? `idle: stops in ${Math.floor(s / 60)} min ${s % 60} s` : `idle: stops in ${s} s`;
}

/** A board's inputs from the contract (vhil/stateview.py inputs): the pin
 *  switches and analog inputs a live session drives. */
export const inputsOf = (contract, board) => contract?.inputs?.[board] ?? [];

/** The session's data as a scenario file's (the API's recording), as the
 *  Scenario tab edits it: no kind or system (scenarios.js). */
export function recordedDoc(scenario) {
    const {
        kind, system, ...doc // eslint-disable-line no-unused-vars
    } = scenario;
    return {
        description: '', watch: [], expect: [], ...doc,
    };
}
