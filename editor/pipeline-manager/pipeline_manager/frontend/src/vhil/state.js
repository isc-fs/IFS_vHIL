/*
 * vHIL: the state panel's model (step 13 of
 * docs/architecture/editor-workspace.md; docs/state-view.md). Pure, no DOM,
 * no Vue: tests/js/state.test.mjs runs it under node.
 *
 * A board's state view (the contract's `state`, vhil/stateview.py) names
 * signals in the trace's own terms: `symbol:<board>.<name>` (samples),
 * `pin:<board>.<pin>` (edges) and `frame:<bus>.<message>.<field>` (frames,
 * decoded here). StateTrace keeps each one's observations as a time series
 * of raw values, fed record by record: a replayed run's whole trace now, a
 * live session's records as they stream later (feature 3), with no other
 * change. cardAt() is what a board shows at a virtual time: its FSM state,
 * how long it has held it and the one before, its relays, faults, key values,
 * and whether its sources went stale (silent for three periods).
 *
 * Decoding a field is the shell's decode.js (one copy), passed in as `rawOf`.
 */

/* eslint-disable no-bitwise -- index halving */
/* eslint-disable max-classes-per-file -- a series, and the trace that holds them */

const STALE_PERIODS = 3;
// Every MainLite boots through its CAN bootloader, whose auto-jump window
// is 2 s from power-on (CLAUDE.md invariant 5): until then the app's RAM
// reads 0, which a state enum would show as its first state (the AMS's
// "Start"), so a card says "in bootloader" instead.
export const BOOT_WINDOW_US = 2e6;

/** One signal's observations: times (virtual µs) and raw values, in order. */
export class Series {
    constructor() {
        this.t = [];
        this.v = [];
        this.changes = null; // [index of each change], computed on demand
    }

    push(t, v) {
        const n = this.t.length;
        if (n && t < this.t[n - 1]) {
            // A slice's records come bus by bus: keep time order.
            let i = n;
            while (i > 0 && this.t[i - 1] > t) i -= 1;
            this.t.splice(i, 0, t);
            this.v.splice(i, 0, v);
        } else {
            this.t.push(t);
            this.v.push(v);
        }
        this.changes = null;
    }

    get length() { return this.t.length; }

    /** The index of the last observation at or before t, or -1. */
    indexAt(t) {
        let lo = 0;
        let hi = this.t.length;
        while (lo < hi) {
            const mid = (lo + hi) >> 1;
            if (this.t[mid] <= t) lo = mid + 1; else hi = mid;
        }
        return lo - 1;
    }

    /** Indices where the value changes (the first observation included). */
    transitions() {
        if (!this.changes) {
            this.changes = [];
            for (let i = 0; i < this.v.length; i += 1) {
                if (i === 0 || this.v[i] !== this.v[i - 1]) this.changes.push(i);
            }
        }
        return this.changes;
    }

    /** The position in transitions() of the change in force at t, or -1. */
    changeAt(t) {
        const ch = this.transitions();
        let lo = 0;
        let hi = ch.length;
        while (lo < hi) {
            const mid = (lo + hi) >> 1;
            if (this.t[ch[mid]] <= t) lo = mid + 1; else hi = mid;
        }
        return lo - 1;
    }
}

/** "frame:can_acu.AMS_status.fsm_state" -> {kind, owner, item, field}. */
export function parseSignal(text) {
    const m = /^(frame|symbol|pin):([A-Za-z_]\w*)\.([A-Za-z0-9_]+)(?:\.([A-Za-z_]\w*))?$/.exec(text || '');
    return m ? {
        kind: m[1], owner: m[2], item: m[3], field: m[4] || '',
    } : null;
}

function findMessage(contract, bus, item) {
    const msgs = contract?.buses?.[bus] || {};
    if (/^0x/i.test(item)) return msgs[parseInt(item, 16)];
    return Object.values(msgs).find((m) => m.name === item);
}

/** A number as the card shows it: a field's resolution, else as is. */
export function formatNumber(v, factor = 1) {
    if (typeof v !== 'number' || !Number.isFinite(v)) return String(v);
    const f = Math.abs(factor);
    const places = f > 0 && f < 1 ? Math.min(6, Math.ceil(-Math.log10(f) - 1e-9)) : 0;
    return Number.isInteger(v) && !places ? String(v) : v.toFixed(places);
}

/** How often an item's source updates (µs), or 0: never stale (a pin's
 *  level holds; an event-only frame). */
export function periodOf(item) {
    if (item.sig?.kind === 'symbol') return (item.period_ms || 10) * 1000;
    if (item.sig?.kind === 'frame') return (item.message?.period_ms || 0) * 1000;
    return 0;
}

/** "312 ms", "4.2 s", "1 min 3 s". */
export function duration(us) {
    const ms = us / 1000;
    if (ms < 1000) return `${Math.round(ms)} ms`;
    if (ms < 60000) return `${(ms / 1000).toFixed(ms < 10000 ? 2 : 1)} s`;
    const s = Math.round(ms / 1000);
    return `${Math.floor(s / 60)} min ${s % 60} s`;
}

export class StateTrace {
    /**
     * `contract`: {buses, state: {board: {firmware, items, errors}}, labels};
     * `rawOf(bytes, field)`: a field's raw value (decode.js rawValue).
     */
    constructor(contract, { rawOf, bootUs = BOOT_WINDOW_US } = {}) {
        this.contract = contract || {};
        this.rawOf = rawOf;
        this.bootUs = bootUs;
        this.series = new Map(); // signal -> Series
        this.frameItems = new Map(); // "bus:id" -> [{signal, field}]
        this.items = new Map(); // board -> [item, with signal parsed and field/message]
        this.end = 0; // the last record's time
        Object.entries(this.contract.state || {}).forEach(([board, view]) => {
            const items = (view.items || []).map((it) => {
                const sig = parseSignal(it.signal);
                const out = { ...it, sig };
                if (sig?.kind === 'frame') {
                    const msg = findMessage(this.contract, sig.owner, sig.item);
                    const field = msg?.fields?.find((f) => f.name === sig.field);
                    if (msg && field) {
                        out.message = msg;
                        out.field = field;
                        const key = `${sig.owner}:${msg.id}`;
                        if (!this.frameItems.has(key)) this.frameItems.set(key, []);
                        this.frameItems.get(key).push({ signal: it.signal, field });
                    }
                }
                return out;
            });
            this.items.set(board, items);
        });
    }

    get boards() { return [...this.items.keys()]; }

    seriesOf(signal) {
        let s = this.series.get(signal);
        if (!s) {
            s = new Series();
            this.series.set(signal, s);
        }
        return s;
    }

    /** One trace record (sample, edge, frame); others are ignored. */
    add(r) {
        if (r.t_us > this.end) this.end = r.t_us;
        if (r.kind === 'sample') {
            this.seriesOf(`symbol:${r.board}.${r.name}`).push(r.t_us, Number(r.value));
        } else if (r.kind === 'edge') {
            this.seriesOf(`pin:${r.board}.${r.pin}`).push(r.t_us, Number(r.level));
        } else if (r.kind === 'frame' || r.bytes) {
            this.addFrame(r);
        }
    }

    /** A frame ({t_us, bus, id, src, bytes or data hex}): the view fields it
     *  carries. The scenario's own frames are not the boards' state. */
    addFrame(r) {
        if (r.src) return;
        const items = this.frameItems.get(`${r.bus}:${r.id}`);
        if (!items || !this.rawOf) return;
        const bytes = r.bytes || Uint8Array.from((r.data || '').match(/../g) || [], (h) => parseInt(h, 16));
        items.forEach(({ signal, field }) => {
            const raw = this.rawOf(bytes, field);
            if (raw !== null && raw !== undefined) this.seriesOf(signal).push(r.t_us, Number(raw));
        });
    }

    /** Every frame of a FrameStore (frames.js). */
    addFrames(store) {
        for (let i = 0; i < store.length; i += 1) {
            const key = `${store.busName(i)}:${store.idAt(i)}`;
            if (this.frameItems.has(key) && !store.isSent(i)) {
                const t = store.tAt(i);
                if (t > this.end) this.end = t;
                this.addFrame({
                    t_us: t, bus: store.busName(i), id: store.idAt(i), bytes: store.data(i),
                });
            }
        }
    }

    /** An item's labels by raw value (its own, else the contract's). */
    labelsOf(item) {
        return item.values || this.contract.labels?.[item.signal] || null;
    }

    /** {raw, text, t, has, stale, labelled} of an item at t. */
    valueAt(item, t) {
        const s = this.series.get(item.signal);
        const i = s ? s.indexAt(t) : -1;
        if (i < 0) {
            return {
                has: false, raw: null, text: 'no data', t: null, stale: false, labelled: false,
            };
        }
        const raw = s.v[i];
        const period = periodOf(item);
        const stale = period > 0 && t - s.t[i] > STALE_PERIODS * period;
        return {
            has: true, raw, t: s.t[i], stale, ...this.textOf(item, raw),
        };
    }

    /** What a raw value reads as: its label, else its physical value. */
    textOf(item, raw) {
        const label = this.labelsOf(item)?.[String(raw)];
        if (label !== undefined) return { text: label, labelled: true };
        const f = item.field;
        if (f) {
            const v = raw * (f.factor ?? 1) + (f.offset ?? 0);
            return { text: formatNumber(v, f.factor ?? 1), labelled: false };
        }
        return { text: formatNumber(raw), labelled: false };
    }

    /** The FSM state at t: {label, raw, since, prev, history, stale, has}. */
    stateAt(item, t) {
        const s = this.series.get(item.signal);
        const now = this.valueAt(item, t);
        const out = {
            ...now, since: null, prev: null, history: [], noEnum: false,
        };
        out.noEnum = now.has && !this.labelsOf(item);
        if (!s || !now.has) return out;
        const ch = s.transitions();
        const k = s.changeAt(t);
        out.history = ch.map((i, j) => ({
            t: s.t[i],
            raw: s.v[i],
            text: this.textOf(item, s.v[i]).text,
            current: j === k,
            future: j > k,
        }));
        if (k >= 0) out.since = s.t[ch[k]];
        if (k > 0) out.prev = { raw: s.v[ch[k - 1]], text: this.textOf(item, s.v[ch[k - 1]]).text };
        return out;
    }

    /** A fault at t: active (non-zero) since when, or cleared after being
     *  active (latched-cleared), with its value's label as the reason. */
    faultAt(item, t) {
        const now = this.valueAt(item, t);
        const s = this.series.get(item.signal);
        const out = {
            ...now, active: now.has && now.raw !== 0, since: null, cleared: false, clearedAt: null, reason: '',
        };
        if (!s || !now.has) return out;
        // A flag (1) needs no reason; a code or a label says which fault.
        const reasonOf = (raw) => {
            const x = this.textOf(item, raw);
            return x.labelled || raw !== 1 ? x.text : '';
        };
        const ch = s.transitions();
        const k = s.changeAt(t);
        if (out.active) {
            let j = k;
            while (j > 0 && s.v[ch[j - 1]] !== 0) j -= 1;
            out.since = s.t[ch[j]];
            out.reason = reasonOf(now.raw);
        } else {
            for (let j = k - 1; j >= 0; j -= 1) {
                if (s.v[ch[j]] !== 0) {
                    out.cleared = true;
                    out.since = s.t[ch[j]];
                    out.clearedAt = s.t[ch[j + 1]];
                    out.reason = reasonOf(s.v[ch[j]]);
                    break;
                }
            }
        }
        return out;
    }

    /** What board `board` shows at virtual time t (µs). */
    cardAt(board, t) {
        const items = this.items.get(board) || [];
        const view = this.contract.state?.[board] || {};
        const card = {
            board,
            firmware: view.firmware || '',
            errors: view.errors || [],
            state: null,
            relays: [],
            faults: [],
            values: [],
        };
        items.forEach((item) => {
            if (item.kind === 'state' && !card.state) {
                card.state = { label: item.label, note: item.note || '', ...this.stateAt(item, t) };
            } else if (item.kind === 'relay') {
                const v = this.valueAt(item, t);
                card.relays.push({
                    label: item.label, has: v.has, on: v.has ? v.raw !== 0 : null, stale: v.stale,
                });
            } else if (item.kind === 'fault') {
                card.faults.push({ label: item.label, ...this.faultAt(item, t) });
            } else if (item.kind !== 'state') {
                const v = this.valueAt(item, t);
                card.values.push({
                    label: item.label,
                    unit: v.labelled ? '' : item.unit || '',
                    note: item.note || '',
                    ...v,
                });
            }
        });
        // In the bootloader's window the app hasn't started: its state is
        // not the app's yet.
        card.boot = t < this.bootUs;
        card.active = card.faults.filter((f) => f.active);
        card.cleared = card.faults.filter((f) => f.cleared);
        card.faulted = card.active.length > 0;
        card.has = Boolean(card.state?.has);
        // Not stale in the bootloader's window: the app isn't running yet.
        card.stale = !card.boot && Boolean(card.state && (!card.state.has || card.state.stale));
        return card;
    }

    /** The canvas pill: "AMS · Precharge", with the fault and stale flags. */
    pillAt(board, t) {
        const card = this.cardAt(board, t);
        const name = (card.firmware || board).toUpperCase();
        let state = '';
        if (card.boot) state = 'bootloader';
        else if (card.state) state = card.state.has ? card.state.text : '—';
        return {
            text: state ? `${name} · ${state}` : name,
            boot: card.boot,
            faulted: card.faulted,
            faults: card.active.map((f) => (f.reason ? `${f.label}: ${f.reason}` : f.label)),
            stale: card.stale,
        };
    }

    /**
     * The history lanes of a board over [0, end]: its FSM state's segments,
     * then each relay's, as {label, kind, segments: [{t0, t1, raw, text}]}.
     */
    lanesOf(board, end = this.end) {
        const items = (this.items.get(board) || []).filter((i) => i.kind === 'state'
            || i.kind === 'relay');
        return items.map((item) => {
            const s = this.series.get(item.signal);
            const segments = [];
            if (s) {
                const ch = s.transitions();
                ch.forEach((i, j) => {
                    const t1 = j + 1 < ch.length ? s.t[ch[j + 1]] : end;
                    segments.push({
                        t0: s.t[i],
                        t1: Math.max(s.t[i], t1),
                        raw: s.v[i],
                        text: this.textOf(item, s.v[i]).text,
                    });
                });
            }
            return {
                label: item.label, kind: item.kind, segments,
            };
        });
    }
}
