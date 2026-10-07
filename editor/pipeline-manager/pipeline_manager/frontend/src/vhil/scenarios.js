/*
 * vHIL: the open system's scenarios (step 10 of
 * docs/architecture/editor-workspace.md; CHANGELOG-VHIL.md): which there
 * are, the one selected for Run and the Scenario tab, its validation and its
 * firmware's CAN contract. Scenario files are
 * systems/<system>.scenarios/<name>.yaml (vhil/server/scenarios.py), read
 * and saved through the API like the system file.
 *
 * The workspace (workspace.js) binds what it knows: the open system, its
 * branch and the graph's firmware refs (bindWorkspace), so this module
 * imports nothing of it.
 */

import { reactive } from 'vue';
import { call } from './api.js';
import {
    canonical, emptyScenario, messagesByRow, resultsByRow,
} from './scenario.js';

export const NAME = /^[a-z0-9][a-z0-9-]{0,63}$/;

export const scen = reactive({
    list: [], // the system's scenarios: {name, description, expects, valid, last_run}
    name: '', // the selected scenario ('': none, a plain run)
    doc: null, // its rows as edited (scenario.js)
    saved: null, // canonical(doc) when opened or last committed
    isNew: false,
    dirty: false,
    errors: [], // the server's check of the doc as edited
    warnings: [],
    checked: false,
    contract: null, // the system's CAN contract (GET /api/systems/<id>/contract)
    contractNote: '',
    selected: '', // the row being edited ("stimuli[2]")
    results: null, // {runId, state, summary} of the last run of it seen here
    version: 0, // bumped on every edit (the timeline redraws)
});

let context = () => ({ id: null, branch: '', fw: {} });
let say = () => {};

/** What the workspace tells: ctx() -> {id, branch, fw}, and its `say`. */
export function bindWorkspace(ctx, sayFn) {
    context = ctx;
    say = sayFn;
}

const base = (id) => `/api/systems/${encodeURIComponent(id)}`;
function query({ branch, fw }, extra = {}) {
    const q = new URLSearchParams(extra);
    if (branch) q.set('branch', branch);
    Object.entries(fw || {}).forEach(([k, v]) => q.append('fw', `${k}=${v}`));
    const s = q.toString();
    return s ? `?${s}` : '';
}

/** Leaves no scenario selected (a plain run). */
export function clearScenario() {
    Object.assign(scen, {
        name: '',
        doc: null,
        saved: null,
        isNew: false,
        dirty: false,
        errors: [],
        warnings: [],
        checked: false,
        selected: '',
        results: null,
        version: scen.version + 1,
    });
}

/** The system's scenarios (on its branch) and its contract. */
export async function loadScenarios() {
    const c = context();
    if (!c.id) {
        scen.list = [];
        return;
    }
    try {
        scen.list = await call('GET', `${base(c.id)}/scenarios${query({ branch: c.branch })}`);
    } catch (e) {
        scen.list = [];
        say(`Scenarios of ${c.id}: ${e.message}`, 'warning');
    }
}

export async function loadContract() {
    const c = context();
    if (!c.id) return;
    try {
        const out = await call('GET', `${base(c.id)}/contract${query(c)}`);
        scen.contract = out;
        const unbuilt = Object.entries(out.built || {}).filter(([, b]) => !b).map(([k]) => k);
        scen.contractNote = unbuilt.length
            ? `No contract for ${unbuilt.join(', ')}: its firmware is not built here yet (a run builds it). Frames are raw hex.`
            : '';
    } catch (e) {
        scen.contract = null;
        scen.contractNote = `No contract: ${e.message}`;
    }
    scen.version += 1;
}

let timer = 0;
let checking = 0;

/** Checks the doc as edited on the server (POST .../preview): its errors
 *  and warnings go to the rows and to Problems. */
export async function checkScenario() {
    const c = context();
    if (!c.id || !scen.name || !scen.doc) return undefined;
    checking += 1;
    const mine = checking;
    try {
        const out = await call('POST', `${base(c.id)}/scenarios/${scen.name}/preview${query(c)}`, { scenario: scen.doc });
        if (mine !== checking) return undefined;
        scen.errors = out.errors;
        scen.warnings = out.warnings;
        scen.checked = true;
        return out;
    } catch (e) {
        if (mine === checking) {
            scen.errors = e.errors || [e.message];
            scen.warnings = [];
        }
        return undefined;
    }
}

/** Called on every edit of the doc: the dirty flag now, a check soon. */
export function edited() {
    if (!scen.doc) return;
    scen.dirty = scen.isNew || canonical(scen.doc) !== scen.saved;
    scen.version += 1;
    clearTimeout(timer);
    timer = setTimeout(checkScenario, 400);
}

/** Opens scenario `name` of the open system (on its branch). */
export async function selectScenario(name) {
    clearTimeout(timer);
    if (!name) {
        clearScenario();
        return;
    }
    const c = context();
    try {
        const out = await call('GET', `${base(c.id)}/scenarios/${name}${query(c)}`);
        const doc = out.scenario || emptyScenario();
        Object.assign(scen, {
            name,
            doc,
            saved: canonical(doc),
            isNew: false,
            dirty: false,
            errors: out.errors,
            warnings: out.warnings,
            checked: true,
            selected: '',
            results: null,
        });
        const last = scen.list.find((s) => s.name === name)?.last_run;
        if (last) {
            // The last run's expect results, until a run here gives newer ones.
            call('GET', `/api/runs/${last.id}`).then((run) => {
                // eslint-disable-next-line no-use-before-define
                if (scen.name === name && !scen.results) setResults(run);
            }).catch(() => {});
        }
        if (!out.scenario) say(`Scenario ${name} does not parse: see Problems`, 'error');
    } catch (e) {
        say(`Could not open scenario ${name}: ${e.message}`, 'error');
    }
    scen.version += 1;
}

/** A new scenario of the open system, empty, selected, not yet committed. */
export function newScenario(name, virtualMs) {
    if (!NAME.test(name)) {
        say(`'${name}' is not a scenario name: lowercase letters, digits and '-'`, 'warning');
        return false;
    }
    if (scen.list.some((s) => s.name === name)) {
        say(`${name} exists: select it instead`, 'warning');
        return false;
    }
    const doc = emptyScenario(virtualMs);
    Object.assign(scen, {
        name,
        doc,
        saved: null,
        isNew: true,
        dirty: true,
        errors: [],
        warnings: [],
        checked: false,
        selected: '',
        results: null,
    });
    scen.list = [...scen.list, {
        name, description: '', expects: 0, valid: true, last_run: null, unsaved: true,
    }];
    edited();
    return true;
}

/** Commits the scenario on `branch` (PUT .../scenarios/<name>). */
export async function commitScenario({
    branch, message, base: from, takeover = false,
}) {
    const c = context();
    const body = { scenario: scen.doc, branch, message };
    if (from) body.base = from;
    if (takeover) body.takeover = true;
    const out = await call('PUT', `${base(c.id)}/scenarios/${scen.name}${query({ fw: c.fw })}`, body);
    scen.isNew = false;
    scen.saved = canonical(scen.doc);
    scen.dirty = false;
    scen.warnings = out.warnings || [];
    return out;
}

/** A finished run of this scenario: its expect results on the rows. */
export function setResults(run) {
    if (!run || run.scenario?.name !== scen.name) return;
    scen.results = {
        runId: run.id, state: run.state, summary: run.summary || {},
    };
    scen.version += 1;
}

/** The scenario's problems for the Problems tab: its check's errors and
 *  warnings, and the expects its last run failed. */
export function scenarioProblems() {
    if (!scen.name) return [];
    const out = [];
    const by = messagesByRow(scen.errors);
    by.forEach((texts, key) => texts.forEach((text) => out.push({
        severity: 'error', text: `${scen.name}: ${text}`, rowKey: key,
    })));
    messagesByRow(scen.warnings).forEach((texts, key) => texts.forEach((text) => out.push({
        severity: 'warning', text: `${scen.name}: ${text}`, rowKey: key,
    })));
    resultsByRow(scen.results?.summary).forEach((r, key) => {
        if (!r.passed) {
            out.push({
                severity: 'error',
                text: `${scen.name}: expect ${r.name || r.check} failed (run ${scen.results.runId}): ${r.detail}`,
                rowKey: key,
                t: r.t_us,
            });
        }
    });
    return out;
}
