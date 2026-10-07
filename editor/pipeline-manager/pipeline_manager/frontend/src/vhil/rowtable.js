/*
 * vHIL: a virtualised table that reuses its rows (the Bus tab's Monitor and
 * Trace; CHANGELOG-VHIL.md). The window maths is the shell's own
 * (vhil/server/static/vtable.js windowFor/scrollFor, copied into shell/ by
 * the image build); what differs from its VirtualTable is the DOM: a fixed
 * pool of row elements, each with its cells made once, filled through
 * textContent and classes by `fill(i, row)`, never parsed HTML, and drawn at
 * most once per animation frame however often it is told to.
 *
 * Positions are set through the CSSOM (style.height, style.transform),
 * which the editor's CSP (style-src 'self') allows.
 */

// eslint-disable-next-line import/no-unresolved, import/extensions -- copied by the build
import { scrollFor, windowFor } from './shell/vtable.js';

const make = (tag, cls, parent) => {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (parent) parent.appendChild(el);
    return el;
};

export default class RowTable {
    /**
     * columns: [{label, cls, bytes}] (`bytes`: the cell holds that many
     * byte spans, `row.bytes[c]`); fill(i, row): fills a pooled row (its
     * `cells`, `bytes`, `el`) with row i; onPick(i): a row was clicked.
     */
    constructor(parent, {
        columns, fill, onPick = null, rowHeight = 22, cls = '', label = '',
    }) {
        this.columns = columns;
        this.fill = fill;
        this.onPick = onPick;
        this.rowHeight = rowHeight;
        this.count = 0;
        this.pool = [];
        this.raf = 0;
        this.stale = false;
        this.root = make('div', `vhil-rt ${cls}`, parent);
        this.root.setAttribute('role', 'table');
        if (label) this.root.setAttribute('aria-label', label);
        const head = make('div', 'vhil-rt-row vhil-rt-head', this.root);
        head.setAttribute('role', 'row');
        columns.forEach((c) => {
            const h = make('span', c.cls || '', head);
            h.setAttribute('role', 'columnheader');
            h.textContent = c.label;
        });
        this.body = make('div', 'vhil-rt-body', this.root);
        this.body.tabIndex = 0;
        this.spacer = make('div', 'vhil-rt-spacer', this.body);
        this.rows = make('div', 'vhil-rt-rows', this.body);
        this.rows.setAttribute('role', 'rowgroup');
        this.rows.style.setProperty('--vhil-rt-row-h', `${rowHeight}px`);
        this.body.addEventListener('scroll', () => this.schedule());
        this.rows.addEventListener('click', (ev) => {
            const row = ev.target.closest('.vhil-rt-row');
            const i = row ? Number(row.dataset.i) : -1;
            if (i >= 0 && this.onPick) this.onPick(i);
        });
        this.resize = new ResizeObserver(() => this.schedule(true));
        this.resize.observe(this.body);
    }

    /** n rows from now on; every row is filled again. */
    setCount(n) {
        this.count = n;
        this.spacer.style.height = `${n * this.rowHeight}px`;
        this.schedule(true);
    }

    /** Draws once in the next animation frame (`refill`: every row again,
     *  not only those whose index changed). */
    schedule(refill = false) {
        if (refill) this.stale = true;
        if (this.raf) return;
        this.raf = requestAnimationFrame(() => {
            this.raf = 0;
            this.draw();
        });
    }

    /** Brings row i into view (`center`: in the middle). */
    reveal(i, center = false) {
        if (i < 0 || i >= this.count) return;
        const { scrollTop, clientHeight } = this.body;
        const top = scrollFor(i, scrollTop, clientHeight, this.rowHeight, center);
        if (top !== this.body.scrollTop) this.body.scrollTop = top;
        this.schedule(true);
    }

    atEnd() {
        return this.body.scrollTop + this.body.clientHeight >= this.count * this.rowHeight - 2;
    }

    row() {
        const el = make('div', 'vhil-rt-row', this.rows);
        el.setAttribute('role', 'row');
        const cells = [];
        const bytes = [];
        this.columns.forEach((c, k) => {
            const cell = make('span', c.cls || '', el);
            cell.setAttribute('role', 'cell');
            cells.push(cell);
            bytes[k] = c.bytes
                ? Array.from({ length: c.bytes }, () => make('span', 'vhil-byte', cell)) : null;
        });
        return {
            el, cells, bytes, i: -1,
        };
    }

    draw() {
        const h = this.body.clientHeight || 400;
        const { start, end } = windowFor(this.body.scrollTop, h, this.rowHeight, this.count, 6);
        this.rows.style.transform = `translateY(${start * this.rowHeight}px)`;
        while (this.pool.length < end - start) this.pool.push(this.row());
        const refill = this.stale;
        this.stale = false;
        for (let k = 0; k < this.pool.length; k += 1) {
            const row = this.pool[k];
            const i = start + k;
            row.el.hidden = i >= end;
            if (i >= end) {
                row.i = -1;
            } else if (refill || row.i !== i) {
                row.i = i;
                row.el.dataset.i = String(i);
                this.fill(i, row);
            }
        }
    }

    destroy() {
        cancelAnimationFrame(this.raf);
        this.resize.disconnect();
        this.root.remove();
    }
}
