/*
 * vHIL: the Scenario tab's timeline, laid out (step 11 of
 * docs/architecture/editor-workspace.md). Pure, no DOM, no Vue:
 * tests/js/timeline.test.mjs runs it under node; VhilTimeline.vue draws it.
 *
 * One lane per bus (CAN rows, frame expects) and per board (gpio, analog,
 * symbol and pin expects), on the virtual-time axis REPLAY's scrubber uses:
 * a send is a diamond, a periodic a hatched bar to its stop (or the run's
 * end), a gpio or analog set a step, an expect a bracket over its window.
 * Watches are the gutter below the lanes.
 */

import { parseSignal } from './scenario.js';

export const LANE_H = 22;
export const AXIS_H = 18;
export const LABEL_W = 92;

const laneId = (kind, name) => `${kind}:${name}`;

/** The lane an item sits on, or null (a watch: the gutter). */
export function laneOf(list, item) {
    if (list === 'watch') return null;
    if (list === 'expect') {
        const s = parseSignal(item.signal);
        if (!s) return null;
        return laneId(s.kind === 'frame' ? 'bus' : 'board', s.owner);
    }
    if (['gpio', 'analog', 'watch'].includes(item.kind)) return laneId('board', item.board);
    return item.bus ? laneId('bus', item.bus) : null;
}

/** The lanes: the system's buses then boards, and any a row names that the
 *  system lacks (so a bad row still shows). */
export function lanesOf(doc, { buses = [], boards = [] } = {}) {
    const lanes = [
        ...buses.map((b) => ({ id: laneId('bus', b), kind: 'bus', name: b })),
        ...boards.map((b) => ({ id: laneId('board', b), kind: 'board', name: b })),
    ];
    const known = new Set(lanes.map((l) => l.id));
    ['stimuli', 'expect'].forEach((list) => (doc?.[list] || []).forEach((item) => {
        const id = laneOf(list, item);
        if (id && !known.has(id)) {
            known.add(id);
            const [kind, name] = id.split(':');
            lanes.push({ id, kind, name });
        }
    }));
    return lanes;
}

/**
 * The marks: {key, list, index, lane, shape, t0, t1, label, item}. t1 is
 * where a bar or a window ends (ms): a periodic's until_ms, else the stop
 * that names it, else the run's end; an expect's until_ms, else the end.
 */
export function marksOf(doc, endMs) {
    if (!doc) return [];
    const stops = {};
    (doc.stimuli || []).forEach((s) => {
        if (s.kind === 'stop_periodic') stops[s.periodic] = Number(s.at_ms || 0);
    });
    const out = [];
    (doc.stimuli || []).forEach((item, index) => {
        const t0 = Number(item.at_ms || 0);
        const base = {
            key: `stimuli[${index}]`, list: 'stimuli', index, t0, t1: t0, item,
        };
        if (item.kind === 'stop_periodic') return; // drawn as its periodic's end
        const lane = laneOf('stimuli', item);
        const id = `0x${Number(item.id ?? 0).toString(16).toUpperCase()}`;
        if (item.kind === 'can_send') {
            out.push({
                ...base, lane, shape: 'diamond', label: id,
            });
        } else if (item.kind === 'can_periodic') {
            let t1 = endMs;
            if (item.until_ms !== undefined && item.until_ms !== null) t1 = Number(item.until_ms);
            else if (item.name && stops[item.name] !== undefined) t1 = stops[item.name];
            out.push({
                ...base, lane, shape: 'bar', t1: Math.max(t0, t1), label: `${id} /${item.period_ms} ms`,
            });
        } else if (item.kind === 'gpio') {
            out.push({
                ...base, lane, shape: 'step', label: `${item.pin} ${item.level ? '↑' : '↓'}`, up: !!item.level,
            });
        } else if (item.kind === 'analog') {
            out.push({
                ...base, lane, shape: 'step', label: `${item.pin} ${item.volts} V`, up: true,
            });
        } else if (item.kind === 'watch') {
            out.push({
                ...base, lane, shape: 'diamond', label: `watch ${item.symbol ?? item.pin}`,
            });
        }
    });
    (doc.expect || []).forEach((item, index) => {
        const t0 = Number(item.at_ms || 0);
        const t1 = item.until_ms !== undefined && item.until_ms !== null
            ? Number(item.until_ms) : endMs;
        out.push({
            key: `expect[${index}]`,
            list: 'expect',
            index,
            lane: laneOf('expect', item),
            shape: 'expect',
            t0,
            t1: Math.max(t0, t1),
            label: item.name || item.check,
            item,
        });
    });
    return out;
}

/** A tick step (ms) that keeps ticks at least `minPx` apart. */
export function tickStep(pxPerMs, minPx = 70) {
    const steps = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 60000];
    return steps.find((s) => s * pxPerMs >= minPx) ?? steps[steps.length - 1];
}

export function ticks(endMs, pxPerMs, minPx = 70) {
    const step = tickStep(pxPerMs, minPx);
    const out = [];
    for (let t = 0; t <= endMs; t += step) out.push(t);
    return out;
}

/** px/ms after zooming by `factor` (clamped), and the scroll that keeps
 *  time `anchorMs` under the pointer at `px` from the strip's left. */
export function zoomAround(pxPerMs, factor, anchorMs, px, { min = 0.01, max = 20 } = {}) {
    const next = Math.min(max, Math.max(min, pxPerMs * factor));
    return { pxPerMs: next, scrollLeft: Math.max(0, anchorMs * next - px) };
}

/** The px/ms that fits `endMs` in `width` px. */
export const fitScale = (endMs, width) => Math.max(0.01, width / Math.max(1, endMs));

/** "1.5 s" / "250 ms". */
export function tickLabel(t) {
    return t >= 1000 && t % 100 === 0 ? `${t / 1000} s` : `${t} ms`;
}
