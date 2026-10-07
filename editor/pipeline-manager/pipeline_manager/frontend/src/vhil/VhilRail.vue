<!--
vHIL: the activity rail (44 px) and its sidebar (260 px, Ctrl+B)
(CHANGELOG-VHIL.md): Palette (Pipeline Manager's node tree, teleported into
#vhil-palette-host from the canvas, whose drag-and-drop it keeps), Systems
(open one onto the canvas) and Runs (the open system's history, or every
system's with none open: opening one enters REPLAY, step 9; its shell page,
with the signals, log and artifacts REPLAY doesn't show yet, is a link) and
Tests (step 11: the open system's scenarios, or every system's, with their
last run's state and expects; opening one selects it and replays that run on
the Scenario tab, ▶ runs it).
-->

<template>
    <div class="vhil-rail-wrap">
    <nav class="vhil-rail" aria-label="Views">
        <button
            v-for="view in VIEWS"
            :key="view.id"
            type="button"
            class="vhil-rail-item"
            :class="{ '--active': layout.sidebar && layout.view === view.id }"
            :aria-pressed="layout.sidebar && layout.view === view.id"
            :aria-label="view.label"
            :title="`${view.label} (Ctrl+B toggles the sidebar)`"
            @click="toggle(view.id)"
        >
            <span class="vhil-rail-glyph" aria-hidden="true">{{ view.glyph }}</span>
            <span class="vhil-rail-label" aria-hidden="true">{{ view.short }}</span>
        </button>
        <span class="vhil-rail-spacer" />
        <button
            type="button"
            class="vhil-rail-item"
            :class="{ '--active': layout.inspector }"
            :aria-pressed="layout.inspector"
            aria-label="Inspector"
            title="Show or hide the inspector"
            @click="layout.inspector = !layout.inspector"
        >
            <span class="vhil-rail-glyph" aria-hidden="true">◧</span>
            <span class="vhil-rail-label" aria-hidden="true">Inspect</span>
        </button>
    </nav>
    <aside
        v-show="layout.sidebar"
        class="vhil-sidebar"
        :aria-label="currentLabel"
    >
        <h2 class="vhil-panel-title">{{ currentLabel }}</h2>
        <div v-show="layout.view === 'palette'" id="vhil-palette-host" class="vhil-palette-host" />

        <div v-if="layout.view === 'systems'" class="vhil-systems">
            <ul class="vhil-list" aria-label="Systems">
                <li v-for="s in ws.systems" :key="s.id">
                    <button
                        type="button"
                        class="vhil-list-item"
                        :class="{ '--current': s.id === ws.id }"
                        :aria-current="s.id === ws.id ? 'true' : undefined"
                        :title="s.description"
                        @click="openSystem(s.id)"
                    >
                        <span class="vhil-list-main">{{ s.id }}</span>
                        <span class="vhil-list-sub muted">{{ s.boards.join(', ') }}</span>
                    </button>
                </li>
            </ul>
            <form class="vhil-form" @submit.prevent="openFrom">
                <h3 class="vhil-form-title">Open from a branch</h3>
                <label>System
                    <select v-model="fromId" class="vhil-input">
                        <option v-for="s in ws.systems" :key="s.id" :value="s.id">
                            {{ s.id }}
                        </option>
                    </select>
                </label>
                <label>Branch
                    <input
                        v-model="fromBranch" class="vhil-input mono" placeholder="checked-out tree"
                    />
                </label>
                <button type="submit" class="vhil-btn">Open</button>
            </form>
            <form class="vhil-form" @submit.prevent="create">
                <h3 class="vhil-form-title">New system</h3>
                <label>Id
                    <input
                        v-model="newId" class="vhil-input mono" required
                        pattern="[a-z0-9][a-z0-9-]*" placeholder="my-system"
                        title="lowercase letters, digits and '-'"
                    />
                </label>
                <button type="submit" class="vhil-btn">Create</button>
            </form>
        </div>

        <div v-if="layout.view === 'tests'" class="vhil-runs vhil-tests">
            <div class="vhil-runs-head">
                <span class="muted">
                    {{ ws.id || 'every system' }}: {{ tests.length }} scenario{{
                        tests.length === 1 ? '' : 's' }}
                </span>
                <button type="button" class="vhil-btn --small" @click="loadTests">
                    Refresh
                </button>
            </div>
            <p v-if="!tests.length" class="muted">
                No scenarios yet: the Scenario tab's + New makes one, and its expects make it
                a test.
            </p>
            <ul class="vhil-list" aria-label="Scenario tests">
                <li v-for="t in tests" :key="`${t.system}/${t.name}`" class="vhil-run-item">
                    <button
                        type="button" class="vhil-list-item vhil-run-row"
                        :class="{ '--current': t.system === ws.id && t.name === scen.name }"
                        :title="t.description || t.name"
                        @click="openTest(t)"
                    >
                        <span class="mono vhil-test-name">
                            {{ ws.id ? '' : `${t.system}/` }}{{ t.name }}
                        </span>
                        <StateBadge v-if="t.last_run" :state="t.last_run.state" />
                        <span v-else class="vhil-list-sub muted">not run</span>
                        <span class="vhil-list-sub muted mono">{{ expectsText(t) }}</span>
                    </button>
                    <button
                        type="button" class="vhil-run-page"
                        :aria-label="`Run ${t.name}`" :title="`Run ${t.name} (F5)`"
                        @click="runTest(t)"
                    >▶</button>
                </li>
            </ul>
        </div>

        <div v-if="layout.view === 'runs'" class="vhil-runs">
            <div class="vhil-runs-head">
                <span class="muted">{{ ws.id || 'every system' }}: last {{ ws.runs.length }}</span>
                <button type="button" class="vhil-btn --small" @click="loadRuns">
                    Refresh
                </button>
            </div>
            <p v-if="!ws.runs.length" class="muted">No runs yet: Run (F5) starts one.</p>
            <ul class="vhil-list" aria-label="Runs">
                <li v-for="r in ws.runs" :key="r.id" class="vhil-run-item">
                    <button
                        type="button"
                        class="vhil-list-item vhil-run-row"
                        :class="{ '--current': r.id === replay.id }"
                        :aria-current="r.id === replay.id ? 'true' : undefined"
                        :title="`Replay run ${r.id}: its frames in the Bus tab`"
                        @click="openRun(r.id)"
                    >
                        <span class="mono num">#{{ r.id }}</span>
                        <StateBadge :state="r.state" />
                        <span class="vhil-list-sub muted mono">
                            {{ ws.id ? '' : `${r.system} · ` }}{{ when(r) }}
                        </span>
                    </button>
                    <a
                        class="vhil-run-page" :href="runPage(r.id)" target="_blank" rel="noopener"
                        :aria-label="`Run ${r.id}'s page: signals, log and artifacts`"
                        :title="`Run ${r.id}'s page: signals, log and artifacts (new tab)`"
                    >↗</a>
                </li>
            </ul>
        </div>
    </aside>
    </div>
</template>

<script>
import {
    computed, defineComponent, ref, watch,
} from 'vue';
import {
    ws, loadRuns, newSystem, open, openRun, pickScenario, runNow,
} from './workspace.js';
import { call, runPage } from './api.js';
import { loadScenarios, scen } from './scenarios.js';
import { replay } from './replay.js';
import StateBadge from './VhilStateBadge.vue';

const VIEWS = [
    {
        id: 'palette', label: 'Palette', short: 'Nodes', glyph: '▦',
    },
    {
        id: 'systems', label: 'Systems', short: 'Systems', glyph: '☰',
    },
    {
        id: 'runs', label: 'Runs', short: 'Runs', glyph: '↻',
    },
    {
        id: 'tests', label: 'Tests', short: 'Tests', glyph: '✓',
    },
];

export default defineComponent({
    components: { StateBadge },
    setup() {
        const layout = computed(() => ws.layout);
        const currentLabel = computed(
            () => VIEWS.find((v) => v.id === ws.layout.view)?.label ?? '',
        );
        const toggle = (id) => {
            if (ws.layout.sidebar && ws.layout.view === id) ws.layout.sidebar = false;
            else Object.assign(ws.layout, { sidebar: true, view: id });
            if (id === 'runs') loadRuns();
            // eslint-disable-next-line no-use-before-define
            if (id === 'tests') loadTests();
        };
        const fromId = ref('');
        // The open system, else the first, once they are known.
        watch(() => [ws.id, ws.systems.length], () => {
            if (!fromId.value) fromId.value = ws.id || ws.systems[0]?.id || '';
        }, { immediate: true });
        const fromBranch = ref('');
        const newId = ref('');
        const openSystem = (id, branch = '') => (id ? open(id, { branch }) : undefined);
        const openFrom = () => openSystem(fromId.value || ws.id, fromBranch.value.trim());
        const create = () => { if (newId.value) newSystem(newId.value.trim()); };
        // Tests: the open system's scenarios (scenarios.js keeps them), or
        // every system's.
        const every = ref([]);
        const tests = computed(() => (ws.id ? scen.list.filter((s) => !s.unsaved)
            .map((s) => ({ ...s, system: ws.id })) : every.value));
        const loadTests = async () => {
            if (ws.id) {
                await loadScenarios();
                return;
            }
            try { every.value = await call('GET', '/api/scenarios'); } catch { every.value = []; }
        };
        const expectsText = (t) => {
            const r = t.last_run;
            if (r && r.expects_passed !== null && r.expects_passed !== undefined) {
                return `✓ ${r.expects_passed} ✕ ${r.expects_failed} · run ${r.id}`;
            }
            return `${t.expects} expect${t.expects === 1 ? '' : 's'}`;
        };
        const select = async (t) => {
            if (t.system !== ws.id) await open(t.system, { scenario: t.name });
            else if (scen.name !== t.name) await pickScenario(t.name);
        };
        const openTest = async (t) => {
            await select(t);
            if (t.last_run) {
                await openRun(t.last_run.id, { tab: 'scenario' });
            } else {
                ws.layout.dock = true;
                ws.layout.dockTab = 'scenario';
            }
        };
        const runTest = async (t) => {
            await select(t);
            ws.layout.dock = true;
            ws.layout.dockTab = 'scenario';
            runNow();
        };
        const when = (r) => {
            const at = (r.created || '').replace('T', ' ').slice(5, 16);
            return r.ref_name ? `${at} @ ${r.ref_name}` : at;
        };
        return {
            VIEWS,
            ws,
            layout,
            currentLabel,
            toggle,
            fromId,
            fromBranch,
            newId,
            openSystem,
            openFrom,
            create,
            loadRuns,
            openRun,
            replay,
            runPage,
            when,
            scen,
            tests,
            loadTests,
            expectsText,
            openTest,
            runTest,
        };
    },
});
</script>
