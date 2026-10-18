/*
 * vHIL: a system's plants in the editor (M8, docs/m8-plan.md;
 * docs/scenarios.md, docs/live-session.md). Pure, no DOM, no Vue:
 * tests/js/plant.test.mjs runs it under node.
 *
 * What a plant may be set to is the contract's `plants`
 * (vhil/server/decode.py plants: each plant's kind, its fault inputs and
 * other inputs as {name, type, default}, its parameters as {value, unit,
 * status}); a live session sets them with a `plant` op, a scenario with a
 * `plant` row, both `{plant, set: {name: value}}`. What a plant is doing is
 * the trace's `plant` records (the worker's per-slice state, TsPlant.state,
 * and the contact moves): PlantTrace keeps each (plant, name) as a time
 * series, and cardAt() is a plant's state at a virtual time.
 */

import { Series } from './state.js';

// The tractive-system plant's state keys (TsPlant.state), as the card shows
// them: values with their unit, then two-state pills. Any other key a plant
// records is shown as name=value.
export const PLANT_VALUES = [
    { key: 'v_link', label: 'V_link', unit: 'V' },
    { key: 'v_pack', label: 'V_pack', unit: 'V' },
    { key: 'i_pack', label: 'i_pack', unit: 'A' },
];
const pill = (key, label, on = 'closed', off = 'open') => ({
    key, label, on, off,
});
export const PLANT_PILLS = [
    pill('air_pos_closed', 'AIR+'),
    pill('air_neg_closed', 'AIR−'),
    pill('pre_closed', 'Precharge'),
    pill('sdc_closed', 'SDC (coil supply)'),
    pill('discharge', 'Discharge', 'on', 'off'),
];
const KNOWN = new Set([...PLANT_VALUES, ...PLANT_PILLS].map((k) => k.key));

/** A plant's value as text: none for null, a voltage to 0.1. */
export function plantValueText(v, unit = '') {
    if (v === null) return 'none';
    if (v === undefined) return '–';
    if (typeof v === 'number' && unit === 'V') return v.toFixed(1);
    return String(v);
}

/** The plants' records over a run: each (plant, name) a Series. */
export class PlantTrace {
    constructor() {
        this.series = new Map(); // plant -> Map(name -> Series)
        this.end = 0;
    }

    /** One trace record; false unless it is a `plant` record. */
    add(r) {
        if (r?.kind !== 'plant' || !r.plant || !r.name) return false;
        if (!this.series.has(r.plant)) this.series.set(r.plant, new Map());
        const names = this.series.get(r.plant);
        if (!names.has(r.name)) names.set(r.name, new Series());
        names.get(r.name).push(r.t_us, r.value);
        if (r.t_us > this.end) this.end = r.t_us;
        return true;
    }

    /** The plants that recorded anything. */
    plants() { return [...this.series.keys()].sort(); }

    /** {name: value} of a plant as of t (the last record at or before it). */
    valuesAt(plant, t) {
        const out = {};
        (this.series.get(plant) || new Map()).forEach((s, name) => {
            const i = s.indexAt(t);
            if (i >= 0) out[name] = s.v[i];
        });
        return out;
    }

    /**
     * A plant's card at t: {has, values: [{key, label, unit, has, text}],
     * pills: [{key, label, on (true | false | null), word}], extra: [{key,
     * text}]}: the known keys always (no data yet: has false / on null), the
     * others it recorded by name.
     */
    cardAt(plant, t) {
        const now = this.valuesAt(plant, t);
        const values = PLANT_VALUES.map((k) => ({
            ...k,
            has: k.key in now,
            text: plantValueText(now[k.key], k.unit),
        }));
        const pills = PLANT_PILLS.map((k) => {
            const on = k.key in now ? Boolean(Number(now[k.key])) : null;
            let word = 'no data';
            if (on !== null) word = on ? k.on : k.off;
            return {
                key: k.key, label: k.label, on, word,
            };
        });
        const extra = Object.keys(now).filter((k) => !KNOWN.has(k)).sort()
            .map((k) => ({ key: k, text: `${k}=${plantValueText(now[k])}` }));
        return {
            plant, has: Object.keys(now).length > 0, values, pills, extra,
        };
    }
}

// -- what a plant may be set to ------------------------------------------------------

/** A parameter's control: bool, number (a null with a unit: a number
 *  that may be none), text, or fixed (a list: shown, not set). */
export function paramType(p) {
    const v = p?.value;
    if (typeof v === 'boolean') return 'bool';
    if (typeof v === 'number') return 'number';
    if (v === null) return p.unit ? 'number' : 'text';
    if (typeof v === 'string') return 'text';
    return 'fixed';
}

/**
 * The contract's plants as the controls list them, by name: [{name, plant,
 * error, faults, inputs, params}], each setting {name, group (fault | input
 * | param), type, value (its default or the catalogue's), unit, status,
 * nullable}.
 */
export function plantsOf(contract) {
    return Object.entries(contract?.plants || {}).sort(([a], [b]) => (a < b ? -1 : 1))
        .map(([name, p]) => {
            const s = p.settable || {};
            const entry = (group) => (e) => ({
                name: e.name,
                group,
                type: e.type === 'bool' ? 'bool' : 'number',
                value: e.default ?? null,
                unit: null,
                status: null,
                nullable: e.default === null,
            });
            return {
                name,
                plant: p.plant || '',
                error: p.error || '',
                faults: (s.faults || []).map(entry('fault')),
                inputs: (s.inputs || []).map(entry('input')),
                params: Object.entries(s.params || {}).map(([n, v]) => ({
                    name: n,
                    group: 'param',
                    type: paramType(v),
                    value: v.value,
                    unit: v.unit ?? null,
                    status: v.status || '',
                    nullable: v.value === null,
                })),
            };
        });
}

/** One plant's settings: its faults, inputs and parameters, in that order. */
export function settingsOf(contract, plant) {
    const p = plantsOf(contract).find((x) => x.name === plant);
    return p ? [...p.faults, ...p.inputs, ...p.params] : [];
}

/** The settings a `plant` row or op may name (a fixed parameter can't). */
export const settableOf = (contract, plant) => settingsOf(contract, plant)
    .filter((e) => e.type !== 'fixed');

/** A setting's value as a row sets it first: a bool fault on (the fault),
 *  else its default or catalogue value. */
export function firstValue(e) {
    if (e.type === 'bool') return e.group === 'param' ? Boolean(e.value) : true;
    if (e.type === 'number') return e.value ?? 0;
    return e.value ?? '';
}

/** A new `plant` row's `set`: its first fault, else first setting. */
export function defaultSet(contract, plant) {
    const [e] = settableOf(contract, plant);
    return e ? { [e.name]: firstValue(e) } : {};
}

/**
 * A setting's value from a control's text (a number field, a text field),
 * or undefined when it isn't one: a number (or none for a nullable one,
 * left empty), a label (vhil/expect.py LABEL), a bool from 'true'/'false'.
 */
export function parseSetting(e, text) {
    const s = String(text ?? '').trim();
    if (e.type === 'bool') {
        if (text === true || s === 'true') return true;
        if (text === false || s === 'false') return false;
        return undefined;
    }
    if (e.type === 'number') {
        if (s === '') return e.nullable ? null : undefined;
        const n = Number(s);
        return Number.isFinite(n) ? n : undefined;
    }
    return /^[A-Za-z0-9_][A-Za-z0-9_ .+:/-]{0,63}$/.test(s) ? s : undefined;
}

/** A row's setting names a key's select offers: `names` (the settable)
 *  the row's `set` doesn't name already, and the key's own `current`
 *  (first, even when it isn't settable: the server says why). */
export function keyChoices(names, set, current) {
    const free = names.filter((n) => n === current || !Object.hasOwn(set || {}, n));
    return free.includes(current) || !current ? free : [current, ...free];
}

/** `set` with key `from` renamed `to` (and set to `value`), in place in the
 *  order, so the row's YAML keeps its order. */
export function renameKey(set, from, to, value) {
    return Object.fromEntries(Object.entries(set || {})
        .map(([k, v]) => (k === from ? [to, value] : [k, v])));
}

/** The op (or a row's body) that sets one name of a plant. */
export const plantOp = (plant, name, value) => ({ kind: 'plant', plant, set: { [name]: value } });

/** "pre_open_circuit = true, r_leak = none": a row's or an op's set. */
export const setText = (set) => Object.entries(set || {})
    .map(([k, v]) => `${k} = ${plantValueText(v)}`).join(', ');
