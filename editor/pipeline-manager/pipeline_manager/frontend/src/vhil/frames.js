/*
 * vHIL: a run's CAN frames for the Bus tab (step 9 of
 * docs/architecture/editor-workspace.md; CHANGELOG-VHIL.md). Pure, no DOM,
 * no Vue: tests/js/frames.test.mjs runs it under node.
 *
 * FrameStore holds frames in typed arrays, a ring of `capacity` frames (the
 * oldest go first, so a long or live run never grows the page without
 * bound), in virtual-time order. monitor.js folds them into one row per
 * (bus, id). Decoding is not here: the Bus tab decodes only the rows in
 * view, with the shell's decode.js.
 */

/* eslint-disable no-bitwise -- flag bits and index halving */

const STRIDE = 8; // classic CAN; longer (FD) payloads are kept aside
const EXT = 1; // flags: an extended id
const SENT = 2; // flags: sent by the run's scenario

const byteAt = (hex, k) => parseInt(hex.substr(2 * k, 2), 16);

export class FrameStore {
    constructor(capacity = 2 ** 20) {
        this.capacity = capacity;
        this.t = new Float64Array(capacity); // virtual µs
        this.id = new Uint32Array(capacity);
        this.bus = new Uint8Array(capacity); // index into this.buses
        this.flags = new Uint8Array(capacity);
        this.dlc = new Uint8Array(capacity);
        this.bytes = new Uint8Array(capacity * STRIDE);
        this.long = new Map(); // slot -> Uint8Array, payloads over 8 bytes
        this.buses = [];
        this.busIndex = new Map();
        this.head = 0; // the slot of frame 0
        this.length = 0;
        this.dropped = 0; // frames pushed out of the ring
        this.version = 0; // bumped on every change
        this.generation = 0; // bumped by clear()
    }

    busOf(name) {
        let i = this.busIndex.get(name);
        if (i === undefined) {
            i = this.buses.length;
            this.buses.push(name);
            this.busIndex.set(name, i);
        }
        return i;
    }

    slot(i) { return (this.head + i) % this.capacity; }

    /** Appends trace frame records ({t_us, bus, id, ext, data: hex, src}),
     *  sorted by time among themselves, after the frames held. */
    append(records) {
        const recs = records.filter((r) => r.kind === undefined || r.kind === 'frame');
        recs.sort((a, b) => a.t_us - b.t_us); // stable: per-bus order within a slice
        recs.forEach((r) => this.push(r));
        if (recs.length) this.version += 1;
        return recs.length;
    }

    push(r) {
        let s;
        if (this.length < this.capacity) {
            s = this.slot(this.length);
            this.length += 1;
        } else {
            s = this.head; // the oldest goes
            this.long.delete(s);
            this.head = (this.head + 1) % this.capacity;
            this.dropped += 1;
        }
        this.t[s] = r.t_us;
        this.id[s] = r.id;
        this.bus[s] = this.busOf(r.bus);
        this.flags[s] = (r.ext ? EXT : 0) | (r.src ? SENT : 0);
        const hex = r.data || '';
        const n = hex.length >> 1;
        this.dlc[s] = Math.min(n, 255);
        const base = s * STRIDE;
        for (let k = 0; k < STRIDE; k += 1) this.bytes[base + k] = k < n ? byteAt(hex, k) : 0;
        if (n > STRIDE) this.long.set(s, Uint8Array.from({ length: n }, (_, k) => byteAt(hex, k)));
    }

    /** Frame i's payload (a view; copy it to keep it). */
    data(i) {
        const s = this.slot(i);
        const long = this.long.get(s);
        if (long) return long;
        return this.bytes.subarray(s * STRIDE, s * STRIDE + Math.min(this.dlc[s], STRIDE));
    }

    busName(i) { return this.buses[this.bus[this.slot(i)]]; }

    isExt(i) { return (this.flags[this.slot(i)] & EXT) !== 0; }

    isSent(i) { return (this.flags[this.slot(i)] & SENT) !== 0; }

    tAt(i) { return this.t[this.slot(i)]; }

    idAt(i) { return this.id[this.slot(i)]; }

    /** Frame i as a decode.js record: {t_us, bus, id, ext, src, bytes}. */
    frame(i) {
        return {
            t_us: this.tAt(i),
            bus: this.busName(i),
            id: this.idAt(i),
            ext: this.isExt(i),
            src: this.isSent(i),
            bytes: this.data(i),
        };
    }

    /** How many frames are at or before t: the index past the last of them. */
    countUpTo(t) {
        let lo = 0;
        let hi = this.length;
        while (lo < hi) {
            const mid = (lo + hi) >> 1;
            if (this.tAt(mid) <= t) lo = mid + 1; else hi = mid;
        }
        return lo;
    }

    clear() {
        this.head = 0;
        this.length = 0;
        this.dropped = 0;
        this.long.clear();
        this.version += 1;
        this.generation += 1;
    }
}

/** Indices of the frames that pass `keep(i)`, in time order. */
export function filterIndices(store, keep) {
    const out = [];
    for (let i = 0; i < store.length; i += 1) if (keep(i)) out.push(i);
    return out;
}

/** The position in `indices` (frame indices, time order) of the last frame
 *  at or before t, or -1. */
export function lastAtOrBefore(store, indices, t) {
    let lo = 0;
    let hi = indices.length;
    while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (store.tAt(indices[mid]) <= t) lo = mid + 1; else hi = mid;
    }
    return lo - 1;
}

/** "a1 00 ff". */
export const hexBytes = (bytes) => Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join(' ');
