<!--
vHIL: the activity rail (44 px) and its sidebar (260 px, Ctrl+B)
(CHANGELOG-VHIL.md): Palette (Pipeline Manager's node tree, teleported into
#vhil-palette-host from the canvas, whose drag-and-drop it keeps), Systems
(open one onto the canvas) and Runs (the open system's history, each linking
to its run page; REPLAY comes with step 9).
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

        <div v-if="layout.view === 'runs'" class="vhil-runs">
            <p v-if="!ws.id" class="muted">Open a system to see its runs.</p>
            <template v-else>
                <div class="vhil-runs-head">
                    <span class="muted">{{ ws.id }}: last {{ ws.runs.length }}</span>
                    <button type="button" class="vhil-btn --small" @click="loadRuns">
                        Refresh
                    </button>
                </div>
                <p v-if="!ws.runs.length" class="muted">No runs yet: Run (F5) starts one.</p>
                <ul class="vhil-list" aria-label="Runs">
                    <li v-for="r in ws.runs" :key="r.id">
                        <a
                            class="vhil-list-item vhil-run-row"
                            :href="runPage(r.id)" target="_blank" rel="noopener"
                            :title="`Run ${r.id}: open its page in a new tab`"
                        >
                            <span class="mono num">#{{ r.id }}</span>
                            <StateBadge :state="r.state" />
                            <span class="vhil-list-sub muted mono">{{ when(r) }}</span>
                        </a>
                    </li>
                </ul>
            </template>
        </div>
    </aside>
    </div>
</template>

<script>
import {
    computed, defineComponent, ref, watch,
} from 'vue';
import {
    ws, loadRuns, newSystem, open,
} from './workspace.js';
import { runPage } from './api.js';
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
            runPage,
            when,
        };
    },
});
</script>
