<!--
vHIL: the dock's Scenario tab (step 10 of docs/architecture/editor-workspace.md;
CHANGELOG-VHIL.md): the selected scenario of the open system as a table of
rows, t | action | target | value | result, and an editor for the selected
row (VhilScenarioRow.vue), under its timeline (VhilTimeline.vue, step 11).
Every edit is checked on the server (scenarios.js checkScenario), and its
messages mark the rows; after a run each expect row shows its result and
evidence, and the timeline lays them over what the run did.
-->

<template>
    <div class="vhil-scen">
        <div class="vhil-scen-bar">
            <label class="vhil-bus-field">Scenario
                <select
                    class="vhil-input" :value="scen.name" :disabled="!ws.id"
                    @change="(ev) => pickScenario(ev.target.value)"
                >
                    <option value="">none (a plain run)</option>
                    <option v-for="s in scen.list" :key="s.name" :value="s.name">
                        {{ s.name }}{{ s.unsaved ? ' (new)' : '' }}
                    </option>
                </select>
            </label>
            <form class="vhil-scen-new" @submit.prevent="create">
                <input
                    v-model="newName" class="vhil-input mono" placeholder="new-scenario"
                    aria-label="New scenario name" pattern="[a-z0-9][a-z0-9\-]*"
                    title="lowercase letters, digits and '-'" :disabled="!ws.id"
                />
                <button type="submit" class="vhil-btn --small" :disabled="!ws.id || !newName">
                    + New
                </button>
            </form>
            <template v-if="scen.doc">
                <label class="vhil-bus-field --grow">Description
                    <input
                        v-model="scen.doc.description" class="vhil-input"
                        maxlength="2000" @input="edited"
                    />
                </label>
                <label class="vhil-bus-field">Run for
                    <input
                        v-model.number="scen.doc.virtual_ms" type="number" min="1"
                        max="600000" step="100" class="vhil-input mono vhil-scen-num"
                        @input="durationEdited"
                    />
                    ms
                </label>
                <label class="vhil-bus-field">
                    <span class="vhil-visually-hidden">Add a row</span>
                    <select class="vhil-input" @change="add">
                        <option value="" disabled>+ Add row…</option>
                        <option v-for="a in ACTIONS" :key="a.action" :value="a.action">
                            {{ a.label }}
                        </option>
                    </select>
                </label>
                <span class="vhil-scen-state mono" role="status">
                    <span v-if="scen.errors.length" class="vhil-count-error">
                        ✕ {{ scen.errors.length }} error{{ scen.errors.length > 1 ? 's' : '' }}
                    </span>
                    <span v-else-if="scen.checked" class="vhil-scen-ok">✓ valid</span>
                    <span v-if="scen.warnings.length" class="vhil-count-warn">
                        ▲ {{ scen.warnings.length }}
                    </span>
                    <span
                        v-if="scen.dirty" class="vhil-dirty" title="Unsaved: Commit… saves it"
                    >● unsaved</span>
                    <span v-if="scen.results" :class="`vhil-badge --${scen.results.state}`">
                        run {{ scen.results.runId }}: {{ passed }}/{{ total }} expects
                    </span>
                </span>
            </template>
        </div>

        <p v-if="!ws.id" class="vhil-placeholder muted">
            Open a system to edit its scenarios.
        </p>
        <p v-else-if="!scen.doc" class="vhil-placeholder muted">{{ emptyText }}</p>
        <template v-else>
            <p v-if="scen.contractNote" class="vhil-bus-note muted">{{ scen.contractNote }}</p>
            <p v-for="m in docMessages" :key="m" class="vhil-scen-msg --error">✕ {{ m }}</p>
            <VhilTimeline :buses="buses" :boards="boardNames" />
            <div class="vhil-scen-body">
                <div class="vhil-scen-table" role="table" aria-label="Scenario rows">
                    <div class="vhil-scen-row vhil-rt-head" role="row">
                        <span role="columnheader" class="vhil-c-num">t ms</span>
                        <span role="columnheader">action</span>
                        <span role="columnheader">target</span>
                        <span role="columnheader">value</span>
                        <span role="columnheader">result</span>
                        <span role="columnheader">
                            <span class="vhil-visually-hidden">remove</span>
                        </span>
                    </div>
                    <p v-if="!rows.length" class="vhil-placeholder muted">
                        No rows: + Add row… adds a stimulus, a watch or an expect.
                    </p>
                    <div
                        v-for="row in rows" :key="row.key" role="row" tabindex="0"
                        class="vhil-scen-row" :class="rowClass(row)"
                        :aria-selected="row.key === scen.selected"
                        :title="rowTitle(row)"
                        @click="scen.selected = row.key"
                        @keydown.enter.prevent="scen.selected = row.key"
                    >
                        <span role="cell" class="vhil-c-num mono">
                            {{ row.t === null ? '–' : row.t }}
                        </span>
                        <span role="cell" class="vhil-scen-action" :class="`--${row.action}`">
                            {{ row.action }}
                        </span>
                        <span role="cell" class="mono">{{ targetOf(row, scen.contract) }}</span>
                        <span role="cell" class="mono">{{ valueOf(row) }}</span>
                        <span role="cell" class="vhil-scen-result">
                            <span v-if="byRow.errors.has(row.key)" class="vhil-count-error">
                                ✕ invalid
                            </span>
                            <template v-else-if="results.has(row.key)">
                                <span :class="resultClass(row)">{{ resultText(row) }}</span>
                                <span class="muted mono">{{ evidence(results.get(row.key)) }}</span>
                            </template>
                            <span v-else-if="byRow.warnings.has(row.key)" class="vhil-count-warn">
                                ▲
                            </span>
                        </span>
                        <span role="cell">
                            <button
                                type="button" class="vhil-btn --small"
                                :aria-label="`Remove ${row.action} row ${row.key}`"
                                @click.stop="remove(row.key)"
                            >✕</button>
                        </span>
                    </div>
                </div>
                <VhilScenarioRow
                    v-if="sel" :key="sel.key" :row="sel" :buses="buses" :boards="boardNames"
                    :periodics="periodics" :messages="selMessages"
                    :result="results.get(sel.key) || null"
                />
                <p v-else class="vhil-scen-editor vhil-placeholder muted">
                    Select a row to edit it.
                </p>
            </div>
        </template>
    </div>
</template>

<script>
import { computed, defineComponent, ref } from 'vue';
import {
    ACTIONS, addRow, messagesByRow, periodicNames, removeRow, resultsByRow, rowsOf, targetOf,
    valueOf,
} from './scenario.js';
import { edited, scen } from './scenarios.js';
import { ws, createScenario, pickScenario } from './workspace.js';
import {
    boards, liveNodes, nodeName, vhilKind,
} from './graph.js';
import VhilScenarioRow from './VhilScenarioRow.vue';
import VhilTimeline from './VhilTimeline.vue';
import './scenario.css';

export default defineComponent({
    components: { VhilScenarioRow, VhilTimeline },
    setup() {
        const newName = ref('');
        const create = () => {
            createScenario(newName.value.trim());
            newName.value = '';
        };
        const emptyText = computed(() => (scen.list.length
            ? 'Pick a scenario, or make a new one: its stimuli, watches and expects run '
                + 'with Run (F5), and its expects make it a test.'
            : `${ws.id} has no scenarios yet: + New makes one.`));
        const rows = computed(() => {
            scen.version; // eslint-disable-line no-unused-expressions
            return scen.doc ? rowsOf(scen.doc) : [];
        });
        const byRow = computed(() => ({
            errors: messagesByRow(scen.errors),
            warnings: messagesByRow(scen.warnings),
        }));
        const docMessages = computed(() => byRow.value.errors.get('') || []);
        const results = computed(() => resultsByRow(scen.results?.summary));
        const passed = computed(() => scen.results?.summary?.expects_passed ?? 0);
        const total = computed(() => (scen.results?.summary?.expects || []).length);

        // What the system has: its boards and buses (the graph's nodes).
        const boardNames = computed(() => {
            scen.version; // eslint-disable-line no-unused-expressions
            try { return boards().map(nodeName); } catch { return []; }
        });
        const buses = computed(() => {
            scen.version; // eslint-disable-line no-unused-expressions
            let names = [];
            try {
                names = liveNodes().filter((n) => vhilKind(n.type) === 'bus').map(nodeName);
            } catch { /* mid-load */ }
            return names.length ? names : Object.keys(scen.contract?.buses || {});
        });
        const periodics = computed(() => {
            scen.version; // eslint-disable-line no-unused-expressions
            return scen.doc ? periodicNames(scen.doc) : [];
        });

        const sel = computed(() => rows.value.find((r) => r.key === scen.selected) || null);
        const selMessages = computed(() => (sel.value
            ? [...(byRow.value.errors.get(sel.value.key) || []),
                ...(byRow.value.warnings.get(sel.value.key) || [])] : []));

        // A one-shot select: back to its placeholder once a row is added.
        const add = (ev) => {
            const select = ev.target;
            const action = select.value;
            select.selectedIndex = 0;
            if (!action) return;
            scen.selected = addRow(scen.doc, action, {
                t: sel.value?.t ?? 0,
                buses: buses.value,
                boards: boardNames.value,
                periodics: periodics.value,
            });
            edited();
        };
        const remove = (key) => {
            removeRow(scen.doc, key);
            scen.selected = '';
            edited();
        };
        const durationEdited = () => {
            ws.virtualMs = Number(scen.doc.virtual_ms);
            edited();
        };

        const rowClass = (row) => ({
            '--selected': row.key === scen.selected,
            '--error': byRow.value.errors.has(row.key),
            '--warning': byRow.value.warnings.has(row.key),
        });
        const resultClass = (row) => (results.value.get(row.key).passed
            ? 'vhil-scen-ok' : 'vhil-count-error');
        const resultText = (row) => (results.value.get(row.key).passed ? '✓ pass' : '✕ fail');
        const evidence = (r) => {
            if (!r) return '';
            const at = r.t_us === null || r.t_us === undefined
                ? '' : `@${(r.t_us / 1000).toFixed(1)} ms`;
            return `${at}${r.value !== null && r.value !== undefined ? ` = ${r.value}` : ''}`;
        };
        const rowTitle = (row) => {
            const msgs = [...(byRow.value.errors.get(row.key) || []),
                ...(byRow.value.warnings.get(row.key) || [])];
            const r = results.value.get(row.key);
            return [...msgs, ...(r ? [r.detail] : [])].join('\n');
        };

        return {
            ACTIONS,
            ws,
            scen,
            newName,
            create,
            emptyText,
            pickScenario,
            edited,
            durationEdited,
            rows,
            byRow,
            docMessages,
            results,
            passed,
            total,
            boardNames,
            buses,
            periodics,
            sel,
            selMessages,
            add,
            remove,
            targetOf,
            valueOf,
            rowClass,
            resultClass,
            resultText,
            evidence,
            rowTitle,
        };
    },
});
</script>
