/*
 * vHIL: the Bus tab's Monitor (step 9; CHANGELOG-VHIL.md): one row per
 * (bus, id) as of a time, folded from a FrameStore (frames.js). Pure:
 * tests/js/frames.test.mjs runs it under node.
 *
 * update(t) folds only the frames since the last call while t moves
 * forward (a scrub or a live run costs what is new); going back in time,
 * frames leaving the ring or a cleared store fold again from the start.
 */

/* eslint-disable no-bitwise -- a bit per changed byte */

const MASK_BYTES = 32;

/** The key of a (bus, id): one row. */
export const rowKey = (bus, id, ext) => `${bus}/${ext ? 'x' : ''}${id}`;

/** A row's mean period in µs (null under two frames). */
export const meanPeriod = (row) => (row.count > 1
    ? (row.last - row.first) / (row.count - 1) : null);

const byBusThenId = (a, b) => {
    if (a.bus !== b.bus) return a.bus < b.bus ? -1 : 1;
    return a.id - b.id;
};

export class Monitor {
    constructor(store) {
        this.store = store;
        this.reset();
    }

    reset() {
        this.rows = new Map();
        this.order = []; // rows sorted by bus, then id
        this.upTo = 0; // frames folded in
        this.dropped = this.store.dropped;
        this.generation = this.store.generation;
        this.sorted = true;
    }

    /** The rows as of time t (virtual µs), sorted by bus, then id. */
    update(t) {
        const s = this.store;
        const n = s.countUpTo(t);
        if (n < this.upTo || this.dropped !== s.dropped || this.generation !== s.generation) {
            this.reset();
        }
        for (let i = this.upTo; i < n; i += 1) this.fold(i);
        this.upTo = n;
        if (!this.sorted) {
            this.order.sort(byBusThenId);
            this.sorted = true;
        }
        return this.order;
    }

    fold(i) {
        const s = this.store;
        const bus = s.busName(i);
        const id = s.idAt(i);
        const ext = s.isExt(i);
        const t = s.tAt(i);
        const data = s.data(i);
        const key = rowKey(bus, id, ext);
        let row = this.rows.get(key);
        if (!row) {
            row = {
                key,
                bus,
                id,
                ext,
                count: 0,
                first: t,
                last: t,
                index: i,
                data: new Uint8Array(0),
                changed: 0,
                changedAt: t,
                src: false,
            };
            this.rows.set(key, row);
            this.order.push(row);
            this.sorted = false;
        }
        // Which bytes differ from the previous frame (a bit per byte).
        let changed = 0;
        const width = Math.min(Math.max(data.length, row.data.length), MASK_BYTES);
        for (let k = 0; k < width; k += 1) if (data[k] !== row.data[k]) changed |= (1 << k);
        if (row.count === 0) changed = 0;
        if (changed || data.length !== row.data.length) row.changedAt = t;
        row.changed = changed;
        row.last = t;
        row.count += 1;
        row.index = i;
        row.src = s.isSent(i);
        row.data = data.slice();
    }
}
