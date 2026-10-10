/*
 * vHIL: a scenario as the Scenario tab edits it (step 10 of
 * docs/architecture/editor-workspace.md; schema in docs/scenarios.md). Pure,
 * no DOM, no Vue: tests/js/scenario.test.mjs runs it under node.
 *
 * A scenario is the rows of a run (vhil/server/runs.py RunScenario): its
 * `stimuli` (can_send, can_periodic, stop_periodic, gpio, analog, and
 * watch: a watch that starts mid-run), `watch` (symbol, pin, from power-on)
 * and `expect` lists, plus its description and virtual time.
 * The table shows them as one list of rows sorted by time, each pointing at
 * its item; editing a row edits the item in place, so the lists stay what
 * the server checks and the file holds.
 */

/* eslint-disable camelcase -- the schema's own names (at_ms, until_ms…) */
/* eslint-disable no-param-reassign -- rows are edited in place */

// action -> what a row of it is (the table's action column).
export const ACTIONS = [
    {
        action: 'send', list: 'stimuli', kind: 'can_send', label: 'send',
    },
    {
        action: 'periodic', list: 'stimuli', kind: 'can_periodic', label: 'periodic',
    },
    {
        action: 'stop', list: 'stimuli', kind: 'stop_periodic', label: 'stop',
    },
    {
        action: 'gpio', list: 'stimuli', kind: 'gpio', label: 'gpio',
    },
    {
        action: 'analog', list: 'stimuli', kind: 'analog', label: 'analog',
    },
    {
        action: 'watch', list: 'watch', kind: null, label: 'watch',
    },
    {
        action: 'expect', list: 'expect', kind: null, label: 'expect',
    },
];
const BY_KIND = Object.fromEntries(ACTIONS.filter((a) => a.kind).map((a) => [a.kind, a]));

export const CHECKS = ['eventually', 'always', 'never', 'period', 'count'];
export const VALUE_CHECKS = ['eventually', 'always', 'never'];
export const OPS = ['==', '!=', '<', '<=', '>', '>='];
export const SNAPS = [1, 5, 10];

export const emptyScenario = (virtualMs = 3000) => ({
    description: '', virtual_ms: virtualMs, slice_ms: 100, stimuli: [], watch: [], expect: [],
});

/** The top bar's "New scenario…" entry: not a scenario name (NAME refuses
 *  '+'), so it can't collide with one. */
export const NEW_SCENARIO = '+new';

/** The top bar's scenario choices for system `id` with scenarios `list`:
 *  none, each scenario, then "New scenario…". A system with none says so in
 *  a disabled entry, so an empty list reads as "none yet", not as broken. */
export function scenarioChoices(list, id) {
    const out = [{ value: '', label: 'no scenario' }];
    if (!list.length) {
        out.push({ value: '-none', label: `no scenarios for ${id || 'this system'} yet`, disabled: true });
    }
    list.forEach((s) => out.push({ value: s.name, label: `▸ ${s.name}${s.unsaved ? ' (new)' : ''}` }));
    out.push({ value: NEW_SCENARIO, label: 'New scenario…' });
    return out;
}

/** The Scenario tab's text with no scenario open: how to pick one, or, for
 *  a system with none, how to make one (here, or from a live session). */
export function emptyScenarioText(id, count) {
    if (count) {
        return 'Pick a scenario, or make a new one: its stimuli, watches and expects run '
            + 'with Run (F5), and its expects make it a test.';
    }
    return `${id} has no scenarios yet. Name one above and + New makes it: add stimuli, `
        + 'watches and expects, then Commit… saves it as '
        + `systems/${id}.scenarios/<name>.yaml. Or start a session with ● Live, drive its `
        + 'buses and pins, and "Save as scenario…" in the top bar turns what you did into one.';
}

/** The action of a list item. */
export function actionOf(list, item) {
    if (list === 'stimuli') return BY_KIND[item.kind]?.action ?? item.kind;
    return list === 'watch' ? 'watch' : 'expect';
}

/**
 * When a stimulus asked for at `tMs` takes effect: the first sync point at or
 * after it (every `quantumUs` of virtual time from power-on; the contract's
 * `sync_quantum_us`). The worker moves a time between two there and says so
 * (docs/scenarios.md, "Times").
 */
export function appliedAt(tMs, quantumUs) {
    const q = Number(quantumUs);
    if (!(q > 0) || !Number.isFinite(Number(tMs))) return Number(tMs);
    const us = Math.round(Number(tMs) * 1000);
    return (Math.ceil(us / q) * q) / 1000;
}

/** A row's key, as the server's messages name it: "stimuli[2]". */
export const keyOf = (list, index) => `${list}[${index}]`;

/**
 * The table's rows: every item of the three lists, sorted by time (watches
 * first: they have none), each {key, list, index, action, t, item}.
 */
export function rowsOf(doc) {
    const rows = [];
    ['stimuli', 'watch', 'expect'].forEach((list) => (doc[list] || []).forEach((item, index) => {
        rows.push({
            key: keyOf(list, index),
            list,
            index,
            action: actionOf(list, item),
            t: list === 'watch' ? null : Number(item.at_ms || 0),
            item,
        });
    }));
    const order = { watch: 0, stimuli: 1, expect: 2 };
    return rows.sort((a, b) => ((a.t ?? -1) - (b.t ?? -1)) || (order[a.list] - order[b.list])
        || (a.index - b.index));
}

export function uniqueName(stem, taken) {
    for (let i = 1; ; i += 1) if (!taken.includes(`${stem}${i}`)) return `${stem}${i}`;
}

/** A new item for an action, at time t, on the first bus and board given. */
export function newItem(action, {
    t = 0, buses = [], boards = [], periodics = [],
} = {}) {
    const bus = buses[0] || 'can0';
    const board = boards[0] || 'board0';
    const at_ms = Math.max(0, Math.round(t));
    switch (action) {
        case 'send': return {
            kind: 'can_send', at_ms, bus, id: 0x100, data: '',
        };
        case 'periodic': return {
            kind: 'can_periodic', name: uniqueName('p', periodics), at_ms, bus, id: 0x100, data: '', period_ms: 10,
        };
        case 'stop': return { kind: 'stop_periodic', at_ms, periodic: periodics[0] || '' };
        case 'gpio': return {
            kind: 'gpio', at_ms, board, pin: '', level: true,
        };
        case 'analog': return {
            kind: 'analog', at_ms, board, pin: '', volts: 0,
        };
        case 'watch': return {
            kind: 'symbol', board, name: '', size: 1, period_ms: 10,
        };
        case 'expect': return {
            check: 'eventually', at_ms, signal: `pin:${board}.`, op: '==', value: 1,
        };
        default: throw new Error(`unknown action ${action}`);
    }
}

/** Adds a row's item to its list; returns its key. */
export function addRow(doc, action, ctx) {
    const { list } = ACTIONS.find((a) => a.action === action);
    doc[list].push(newItem(action, ctx));
    return keyOf(list, doc[list].length - 1);
}

/** Removes the row `key`; a stop that named a removed periodic goes too. */
export function removeRow(doc, key) {
    const m = /^(\w+)\[(\d+)\]$/.exec(key);
    if (!m) return;
    const [removed] = doc[m[1]].splice(Number(m[2]), 1);
    if (removed?.kind === 'can_periodic' && removed.name) {
        doc.stimuli = doc.stimuli.filter((s) => !(s.kind === 'stop_periodic' && s.periodic === removed.name));
    }
}

/** Moves an item to time t (ms): its window or its stop moves with it. */
export function moveItem(item, t) {
    const to = Math.max(0, Math.round(t * 1000) / 1000);
    const by = to - Number(item.at_ms || 0);
    item.at_ms = to;
    if (item.until_ms !== undefined && item.until_ms !== null) {
        item.until_ms = Math.max(to, Math.round((Number(item.until_ms) + by) * 1000) / 1000);
    }
}

export const snap = (t, step) => Math.max(0, Math.round(t / step) * step);

/** The named periodics, for a stop's select. */
export const periodicNames = (doc) => (doc.stimuli || [])
    .filter((s) => s.kind === 'can_periodic' && s.name).map((s) => s.name);

// -- what a row shows -------------------------------------------------------------

const hex = (id) => `0x${Number(id).toString(16).toUpperCase().padStart(3, '0')}`;

/** The contract's message for a bus and id, or undefined. */
export const messageOf = (contract, bus, id) => contract?.buses?.[bus]?.[id];

/** The contract's message for a bus and a name or 0x id. */
export function findMessage(contract, bus, item) {
    const msgs = contract?.buses?.[bus] || {};
    if (/^0x/i.test(item || '')) return msgs[parseInt(item, 16)];
    return Object.values(msgs).find((m) => m.name === item);
}

/** "frame:can_acu.AMS_status.fsm_state" -> {kind, owner, item, field}, or null. */
export function parseSignal(text) {
    const m = /^(frame|symbol|pin):([A-Za-z_]\w*)\.([A-Za-z0-9_]+)(?:\.([A-Za-z_]\w*))?$/.exec(text || '');
    if (!m) return null;
    if (m[1] !== 'frame' && m[4]) return null;
    return {
        kind: m[1], owner: m[2], item: m[3], field: m[4] || '',
    };
}

/** A signal as typed so far ("pin:ams.", "frame:can_acu.AMS_status."), or null. */
export function looseSignal(text) {
    const m = /^(frame|symbol|pin):([^.]*)(?:\.([^.]*))?(?:\.(.*))?$/.exec(text || '');
    return m ? {
        kind: m[1], owner: m[2], item: m[3] || '', field: m[4] || '',
    } : null;
}

export function signalText({
    kind, owner, item, field,
}) {
    return `${kind}:${owner}.${item}${field ? `.${field}` : ''}`;
}

/** What a row acts on: "can_acu 0x100 VCU_heartbeat", "ams.PF9". */
export function targetOf(row, contract) {
    const it = row.item;
    switch (row.action) {
        case 'send':
        case 'periodic': {
            const name = messageOf(contract, it.bus, it.id)?.name;
            return `${it.bus} ${hex(it.id)}${it.ext ? 'x' : ''}${name ? ` ${name}` : ''}`;
        }
        case 'stop': return it.periodic || '–';
        case 'gpio':
        case 'analog': return `${it.board}.${it.pin}`;
        case 'watch': return it.pin !== undefined ? `pin ${it.board}.${it.pin}`
            : `${it.board}.${it.name ?? it.symbol}`;
        case 'expect': return it.signal;
        default: return '';
    }
}

const ms = (v) => `${Number(v)} ms`;

/** "eventually == Precharge by 800 ms", "period ≤ 120 ms until 5000 ms". */
export function expectText(e) {
    const until = e.until_ms != null ? ` by ${ms(e.until_ms)}` : '';
    if (VALUE_CHECKS.includes(e.check)) return `${e.check} ${e.op || '=='} ${e.value}${until}`;
    if (e.check === 'period') {
        const parts = [];
        if (e.min_ms != null) parts.push(`≥ ${ms(e.min_ms)}`);
        if (e.max_ms != null) parts.push(`≤ ${ms(e.max_ms)}`);
        return `period ${parts.join(', ')}${until}`;
    }
    const parts = [];
    if (e.min != null) parts.push(`≥ ${e.min}`);
    if (e.max != null) parts.push(`≤ ${e.max}`);
    return `count ${parts.join(', ')}${until}`;
}

/** What a row sets or checks: "[00 00 02] every 10 ms", "HIGH", "== 1 by 800 ms". */
export function valueOf(row) {
    const it = row.item;
    const bytes = (it.data || '').match(/../g)?.join(' ') ?? '';
    switch (row.action) {
        case 'send': return `[${bytes}]`;
        case 'periodic': return `[${bytes}] every ${ms(it.period_ms)}`
            + `${it.until_ms != null ? ` until ${ms(it.until_ms)}` : ''}${it.name ? ` · ${it.name}` : ''}`;
        case 'stop': return 'stop';
        case 'gpio': return it.level ? 'HIGH' : 'LOW';
        case 'analog': return `${it.volts} V`;
        case 'watch': return it.pin !== undefined ? 'edges'
            : `${it.size ? `${it.size} B ` : ''}every ${ms(it.period_ms ?? 10)}`;
        case 'expect': return expectText(it);
        default: return '';
    }
}

/**
 * The server's messages by row: "stimuli[2]: no bus 'x'" and
 * "stimuli[2].bus: …" go to stimuli[2]; others (the scenario's own) to "".
 */
export function messagesByRow(messages) {
    const out = new Map();
    (messages || []).forEach((text) => {
        const m = /^((?:stimuli|watch|expect)\[\d+\])/.exec(text);
        const key = m ? m[1] : '';
        if (!out.has(key)) out.set(key, []);
        out.get(key).push(text);
    });
    return out;
}

/** The expect results of a run summary by row key ("expect[0]"). */
export function resultsByRow(summary) {
    const out = new Map();
    (summary?.expects || []).forEach((r) => out.set(keyOf('expect', r.index), r));
    return out;
}

/** A doc's text to compare (the dirty flag). */
export function canonical(doc) {
    return JSON.stringify(doc);
}
