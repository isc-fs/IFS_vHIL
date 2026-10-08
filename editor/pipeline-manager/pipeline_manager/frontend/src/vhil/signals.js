/*
 * vHIL: the Signals tab's model (step 18 of
 * docs/architecture/editor-workspace.md; CHANGELOG-VHIL.md). Pure, no DOM,
 * no Vue: tests/js/signals.test.mjs runs it under node.
 *
 * A signal is a key, kept per system in the viewer's localStorage:
 *   frame:<bus>/<id>/<field>   a decoded field of a CAN message (id decimal,
 *                              as the contract keys it)
 *   sample:<board>.<name>      a sampled firmware global (a watch, a state
 *                              view's symbol)
 *   lane:<board>/<label>       a board's state view FSM state or relay
 *                              (state.js lanesOf): a step lane with its labels
 * seriesOf() turns each into {x: [s], y: [v]} for the shell's plot.js
 * (uPlot), from REPLAY's FrameStore, samples and StateTrace. Decoding is the
 * shell's decode.js, passed in as `rawOf` and `physOf` (one copy).
 */

export const SIGNAL_LIMIT = 16;

export const frameKey = (bus, id, field) => `frame:${bus}/${id}/${field}`;
export const sampleKey = (board, name) => `sample:${board}.${name}`;
export const laneKey = (board, label) => `lane:${board}/${label}`;

/** {kind: frame|sample|lane, ...its parts} of a key, or null. */
export function parseKey(key) {
    let m = /^frame:([^/]+)\/(\d+)\/(.+)$/.exec(key || '');
    if (m) {
        return {
            kind: 'frame', bus: m[1], id: Number(m[2]), field: m[3],
        };
    }
    m = /^sample:([^.]+)\.(.+)$/.exec(key || '');
    if (m) return { kind: 'sample', board: m[1], name: m[2] };
    m = /^lane:([^/]+)\/(.+)$/.exec(key || '');
    if (m) return { kind: 'lane', board: m[1], label: m[2] };
    return null;
}

/** The message and field a frame key names in the contract, or {}. */
function fieldOf(contract, k) {
    const msg = contract?.buses?.[k.bus]?.[k.id];
    return { msg, field: msg?.fields?.find((f) => f.name === k.field) };
}

/** "can_acu AMS_status.fsm_state", "ams.g_state_telemetry", "ams · AIR+". */
export function labelOf(key, contract) {
    const k = parseKey(key);
    if (!k) return key;
    if (k.kind === 'sample') return `${k.board}.${k.name}`;
    if (k.kind === 'lane') return `${k.board} · ${k.label}`;
    const { msg } = fieldOf(contract, k);
    const name = msg ? msg.name : `0x${k.id.toString(16).toUpperCase().padStart(3, '0')}`;
    return `${k.bus} ${name}.${k.field}`;
}

/** A value table as a formatter, for an enum or a bit: raw -> its label. */
const tableFormat = (values) => (v) => (values && values[v] !== undefined ? `${values[v]}` : `${v}`);

/**
 * Every frame key's series in one pass over `store` (a FrameStore):
 * key -> {x, y, unit, format?, levels?}. A field with a value table and no
 * scaling (an enum, a bit) plots its raw values with their labels.
 */
export function frameSeries(store, contract, keys, rawOf, physOf) {
    const out = new Map();
    const want = new Map(); // "bus/id" -> [[key, field]]
    keys.forEach((key) => {
        const k = parseKey(key);
        if (k?.kind !== 'frame') return;
        const { msg, field } = fieldOf(contract, k);
        const s = { x: [], y: [], unit: field?.unit || '' };
        if (field?.values && field.factor === 1 && field.offset === 0) {
            s.format = tableFormat(field.values);
            s.levels = Object.keys(field.values).map(Number).sort((a, b) => a - b);
            s.unit = '';
        }
        out.set(key, s);
        if (!field) return;
        const id = `${k.bus}/${k.id}`;
        if (!want.has(id)) want.set(id, []);
        want.get(id).push([key, field, Boolean(msg.ext)]);
    });
    if (!want.size) return out;
    for (let i = 0; i < store.length; i += 1) {
        const fields = want.get(`${store.busName(i)}/${store.idAt(i)}`);
        if (fields) {
            const bytes = store.data(i);
            fields.forEach(([key, field, ext]) => {
                if (store.isExt(i) !== ext) return;
                const raw = rawOf(bytes, field);
                if (raw === null) return;
                const s = out.get(key);
                s.x.push(store.tAt(i) / 1e6);
                s.y.push(s.format ? raw : physOf(raw, field));
            });
        }
    }
    return out;
}

/** A sample key's series from the trace's sample records. */
export function sampleSeries(samples, key) {
    const k = parseKey(key);
    const x = [];
    const y = [];
    if (k?.kind === 'sample') {
        samples.forEach((s) => {
            if (s.board === k.board && s.name === k.name) {
                x.push(s.t_us / 1e6);
                y.push(s.value);
            }
        });
    }
    return { x, y, unit: '' };
}

/**
 * A lane key's series from the run's StateTrace: a step per segment of the
 * board's FSM state or relay, held to `endUs`; its values read as their
 * labels ("Precharge", "closed"). Empty with no such lane.
 */
export function laneSeries(stateTrace, key, endUs) {
    const k = parseKey(key);
    const out = {
        x: [], y: [], unit: '', levels: [], format: (v) => `${v}`,
    };
    if (k?.kind !== 'lane' || !stateTrace) return out;
    const lane = stateTrace.lanesOf(k.board, endUs).find((l) => l.label === k.label);
    if (!lane || !lane.segments.length) return out;
    const text = new Map();
    lane.segments.forEach((s) => {
        out.x.push(s.t0 / 1e6);
        out.y.push(s.raw);
        text.set(s.raw, s.text);
    });
    const last = lane.segments[lane.segments.length - 1];
    if (last.t1 > last.t0) {
        out.x.push(last.t1 / 1e6);
        out.y.push(last.raw);
    }
    out.kind = lane.kind;
    out.levels = [...text.keys()].filter((v) => typeof v === 'number').sort((a, b) => a - b);
    out.format = (v) => (text.has(v) ? text.get(v) : `${v}`);
    return out;
}

/** Every board's lanes (its FSM state, then its relays): the tab's default. */
export function laneKeys(stateTrace) {
    if (!stateTrace) return [];
    return stateTrace.boards.flatMap((b) => stateTrace.lanesOf(b, stateTrace.end)
        .map((l) => laneKey(b, l.label)));
}

/** What can be added: lanes, sampled globals, decoded messages seen. */
export function choices(store, contract, samples, stateTrace) {
    const names = new Set();
    samples.forEach((s) => names.add(sampleKey(s.board, s.name)));
    const seen = new Map();
    for (let i = 0; i < store.length; i += 1) {
        const id = `${store.busName(i)}/${store.idAt(i)}`;
        if (!seen.has(id)) {
            const msg = contract?.buses?.[store.busName(i)]?.[store.idAt(i)];
            if (msg && Boolean(msg.ext) === store.isExt(i)) seen.set(id, msg);
        }
    }
    const messages = [...seen].sort(([a], [b]) => a.localeCompare(b, undefined, { numeric: true }))
        .map(([id, msg]) => ({
            id, bus: id.split('/')[0], msg, fields: msg.fields.map((f) => f.name),
        }));
    return { lanes: laneKeys(stateTrace), samples: [...names].sort(), messages };
}

/** `keys` with `key` added (the oldest dropped past SIGNAL_LIMIT). */
export function addKey(keys, key) {
    if (!key || keys.includes(key)) return keys;
    return [...keys, key].slice(-SIGNAL_LIMIT);
}

/** Every key's series: {key, label, x, y, unit, stepped, format?, levels?}. */
export function seriesOf(keys, {
    store, contract, samples, stateTrace, endUs, rawOf, physOf, stepped = true,
}) {
    const frames = frameSeries(store, contract, keys, rawOf, physOf);
    return keys.map((key) => {
        const k = parseKey(key);
        let s;
        if (k?.kind === 'frame') s = frames.get(key);
        else if (k?.kind === 'sample') s = sampleSeries(samples, key);
        else s = laneSeries(stateTrace, key, endUs);
        // A lane and a labelled field are steps whatever the toggle says.
        return {
            key, label: labelOf(key, contract), ...s, stepped: stepped || Boolean(s.format),
        };
    });
}
