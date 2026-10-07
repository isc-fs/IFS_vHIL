/*
 * vHIL: a live session in the workspace (step 15 of
 * docs/architecture/editor-workspace.md; docs/live-session.md): the two
 * WebSockets of a live run and what they feed.
 *
 *   /api/runs/<id>/live      the run's trace as it is written: frames,
 *                            samples, edges and logs into the REPLAY store
 *                            and model (replay.js feedLive), `op` records
 *                            into the session's state (live.js), `clock`
 *                            records into the clock
 *   /api/runs/<id>/session   ops out, acks and control in
 *
 * Every tab on the run, the one that controls it and the ones that watch,
 * reads the same trace, so they show the same thing; only the controller's
 * ops are taken. Records are batched: one flush every 50 ms at most, not one
 * render per message.
 */

import { reactive } from 'vue';
import { csrfToken, wsUrl } from './api.js';
import {
    BusRates, SessionState, opText,
} from './live.js';
import { feedLive, replay } from './replay.js';

const KINDS = 'frame,log,edge,sample,bus_load,op,clock';
const FLUSH_MS = 50;
const RATES_MS = 250; // the wires' frames/s and dash march: 4 Hz

export const live = reactive({
    id: null, // the live run on the workspace, or null
    role: 'none', // control | view | none (not connected)
    holder: null, // who controls it
    mayControl: false, // this user may (owner or admin), when it is free
    connected: false,
    rtf: null,
    paused: false,
    idleLeft: null,
    limits: {},
    periodics: [], // the senders running: {name, bus, id, ext, data, period_ms, since}
    levels: {}, // "board.pin" -> the last level a gpio op set
    volts: {}, // "board.pin" -> the last voltage an analog op set
    used: [], // periodic names this session has used
    rates: {}, // bus -> frames/s over the last second of virtual time
    pending: 0, // ops sent, not yet applied or refused
    ended: null, // the run's final state
    menu: null, // a board's inputs opened from its context menu: {board, x, y}
    version: 0,
});

let feed = null;
let channel = null;
let state = new SessionState();
let rates = new BusRates();
let queue = [];
let flushTimer = 0;
let ratesTimer = 0;
let cid = 0;
const waiting = new Map(); // cid or op id -> the op, until acked
let hooks = { log: () => {}, say: () => {}, onEnd: () => {} };

function publish() {
    live.periodics = state.running();
    live.levels = Object.fromEntries(state.levels);
    live.volts = Object.fromEntries(state.volts);
    live.used = [...state.used];
    live.version += 1;
}

function flush() {
    flushTimer = 0;
    const batch = queue;
    queue = [];
    let ops = false;
    let clock = null;
    batch.forEach((r) => {
        if (r.kind === 'frame' && !r.src) rates.add(r.bus, r.t_us);
        else if (r.kind === 'op') {
            state.add(r);
            ops = true;
            hooks.log(`  ${(r.t_us / 1e6).toFixed(3)} s  op ${r.op_id} ${r.status}: ${opText(r.op)}`
                + `${r.detail ? ` (${r.detail})` : ''}${r.login ? ` · ${r.login}` : ''}`);
        } else if (r.kind === 'clock') clock = r;
        else if (r.kind === 'log') hooks.log(`  ${(r.t_us / 1e6).toFixed(3)} s  ${r.text}`);
    });
    feedLive(batch);
    if (ops) publish();
    if (clock) {
        live.rtf = clock.rtf;
        live.paused = Boolean(clock.paused);
        live.idleLeft = clock.idle_left_s ?? null;
    }
    const last = batch.length ? batch[batch.length - 1].t_us : 0;
    const end = Math.max(replay.end, clock ? clock.t_us : 0, last || 0);
    if (end !== replay.end) {
        replay.end = end;
        replay.t = end; // LIVE follows the clock
    }
}

function onRecord(text) {
    const rec = JSON.parse(text);
    if (rec.kind === 'end') {
        flush();
        live.ended = rec.state || 'unknown';
        live.menu = null;
        // eslint-disable-next-line no-use-before-define
        disconnect();
        hooks.onEnd(live.ended);
        return;
    }
    queue.push(rec);
    if (!flushTimer) flushTimer = setTimeout(flush, FLUSH_MS);
}

function onChannel(msg) {
    switch (msg.kind) {
        case 'hello':
            Object.assign(live, {
                role: msg.role,
                holder: msg.holder,
                mayControl: msg.may_control,
                limits: msg.limits || {},
            });
            if (msg.role === 'view') {
                hooks.say(msg.may_control
                    ? `Run ${live.id}: ${msg.holder || 'another tab'} controls this session; this tab watches`
                    : `Run ${live.id}: watching (only its owner or an admin controls it)`);
            }
            break;
        case 'control':
            Object.assign(live, { role: msg.role, holder: msg.holder });
            hooks.say(msg.role === 'control' ? 'This tab controls the session'
                : `${msg.holder || 'Nobody'} controls the session`);
            break;
        case 'queued': {
            const op = waiting.get(msg.cid);
            waiting.delete(msg.cid);
            if (op) waiting.set(`op${msg.op_id}`, op);
            break;
        }
        case 'refused':
            waiting.delete(msg.cid);
            live.pending = Math.max(0, live.pending - 1);
            hooks.say(`Refused: ${msg.detail}`, 'warning');
            break;
        case 'ack':
            if (waiting.delete(`op${msg.op_id}`)) live.pending = Math.max(0, live.pending - 1);
            if (msg.status === 'refused') hooks.say(`Op ${msg.op_id} refused: ${msg.detail}`, 'warning');
            break;
        default:
            break;
    }
}

/** Follows live run `run`; `control`: ask for the session's control (the
 *  server gives it to the owner or an admin when nobody holds it). */
export function connect(run, {
    control = true, log, say, onEnd,
} = {}) {
    // eslint-disable-next-line no-use-before-define
    disconnect();
    hooks = { log: log || hooks.log, say: say || hooks.say, onEnd: onEnd || hooks.onEnd };
    state = new SessionState();
    rates = new BusRates();
    queue = [];
    waiting.clear();
    Object.assign(live, {
        id: run.id,
        role: 'none',
        holder: null,
        mayControl: false,
        connected: false,
        rtf: null,
        paused: false,
        idleLeft: null,
        rates: {},
        pending: 0,
        ended: null,
    });
    publish();
    feed = new WebSocket(wsUrl(`/api/runs/${run.id}/live?kinds=${KINDS}`));
    feed.onmessage = (ev) => onRecord(ev.data);
    feed.onclose = () => {
        if (live.id === run.id && !live.ended) {
            live.connected = false;
            hooks.say(`Run ${run.id}: the live view was lost`, 'warning');
        }
    };
    channel = new WebSocket(wsUrl(`/api/runs/${run.id}/session`));
    channel.onopen = () => {
        live.connected = true;
        channel.send(JSON.stringify({ kind: 'hello', csrf: csrfToken(), control }));
    };
    channel.onmessage = (ev) => onChannel(JSON.parse(ev.data));
    channel.onclose = () => {
        live.connected = false;
        if (live.role !== 'none' && !live.ended) live.role = 'none';
    };
    ratesTimer = setInterval(() => {
        const r = rates.rates(replay.end);
        if (JSON.stringify(r) !== JSON.stringify(live.rates)) live.rates = r;
    }, RATES_MS);
}

/** Stops following (the session goes on: its idle timeout ends it). */
export function disconnect() {
    clearTimeout(flushTimer);
    clearInterval(ratesTimer);
    flushTimer = 0;
    [feed, channel].filter(Boolean).forEach((s) => {
        Object.assign(s, { onclose: null, onmessage: null });
        try { s.close(); } catch { /* closed */ }
    });
    feed = null;
    channel = null;
    live.connected = false;
    if (!live.ended) live.role = 'none';
    live.rates = {};
}

/** Leaves the session altogether: nothing live on the workspace. */
export function leave() {
    disconnect();
    live.id = null;
    live.ended = null;
    live.menu = null;
}

/** Why an op can't be sent from this tab, or null. */
export function cannotSend() {
    if (!live.id || live.ended) return 'no live session';
    if (!live.connected) return 'not connected to the session';
    if (live.role !== 'control') {
        return live.mayControl ? `${live.holder || 'another tab'} controls the session`
            : 'only the run\'s owner or an admin controls its session';
    }
    return null;
}

/** Sends an op (a stimulus or pause/resume/stop/keepalive). */
export function send(op) {
    const why = cannotSend();
    if (why) {
        hooks.say(`Not sent: ${why}`, 'warning');
        return false;
    }
    cid += 1;
    waiting.set(`c${cid}`, op);
    live.pending += 1;
    channel.send(JSON.stringify({ kind: 'op', cid: `c${cid}`, op }));
    return true;
}

/** Takes the session's control, if its holder let it go. */
export function take() {
    if (channel && live.connected) channel.send(JSON.stringify({ kind: 'take' }));
}

/** Stops every periodic sender running. */
export function stopAll() {
    live.periodics.forEach((p) => send({ kind: 'stop_periodic', periodic: p.name }));
}
