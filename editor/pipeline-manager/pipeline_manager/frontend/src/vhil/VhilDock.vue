<!--
vHIL: the bottom dock (Ctrl+J; Ctrl+1..8 or Alt+1..8 picks a tab;
CHANGELOG-VHIL.md). Log is Pipeline Manager's terminal (the plain log view,
src/vhil/LogView.vue), moved here from its own panel; Problems lists Check's
errors and warnings, and a click selects the node one is about (or the
scenario row: its check's messages and its run's failed expects, step 10);
Bus shows a replayed run's frames (VhilBus.vue, step 9); Scenario the
selected scenario's rows (VhilScenario.vue, step 10); State each board's
state card (VhilState.vue, step 13); Debug a board's debugger in a live
session (VhilDebug.vue, step 17); Signals the run's plots and Artifacts its
record, tests and files (VhilSignals.vue, VhilArtifacts.vue, step 18).
Collapsed, it is its tab strip, which still shows the problem count.
-->

<template>
    <section class="vhil-dock" :class="{ '--collapsed': !ws.layout.dock }" aria-label="Dock">
        <div
            v-show="ws.layout.dock"
            class="vhil-resize --y"
            role="separator"
            aria-orientation="horizontal"
            aria-label="Resize the dock"
            :aria-valuenow="ws.layout.dockH"
            aria-valuemin="120"
            tabindex="0"
            @pointerdown="startResize"
            @keydown.up.prevent="resizeBy(24)"
            @keydown.down.prevent="resizeBy(-24)"
        />
        <div class="vhil-dock-bar">
            <div class="vhil-tabs" role="tablist" aria-label="Dock tabs">
                <button
                    v-for="(tab, i) in DOCK_TABS"
                    :id="`vhil-tab-${tab.id}`"
                    :key="tab.id"
                    type="button"
                    role="tab"
                    class="vhil-tab"
                    :class="{ '--placeholder': tab.step }"
                    :aria-selected="ws.layout.dock && ws.layout.dockTab === tab.id"
                    :aria-controls="`vhil-tabpanel-${tab.id}`"
                    :tabindex="ws.layout.dockTab === tab.id ? 0 : -1"
                    :title="`${tab.label} (Ctrl+${i + 1})`"
                    @click="pick(tab.id)"
                    @keydown.right.prevent="step(1)"
                    @keydown.left.prevent="step(-1)"
                >
                    {{ tab.label }}
                    <span v-if="tab.id === 'problems' && problems.length" class="vhil-count">
                        <span v-if="errors" class="vhil-count-error">✕ {{ errors }}</span>
                        <span v-if="warnings" class="vhil-count-warn">▲ {{ warnings }}</span>
                    </span>
                </button>
            </div>
            <div class="vhil-dock-tools">
                <button
                    v-if="ws.layout.dock && ws.layout.dockTab === 'log'"
                    type="button" class="vhil-btn --small" @click="clearLog"
                >Clear log</button>
                <button
                    type="button" class="vhil-btn --small"
                    :aria-expanded="ws.layout.dock"
                    aria-keyshortcuts="Control+J"
                    :title="`${ws.layout.dock ? 'Collapse' : 'Expand'} the dock (Ctrl+J)`"
                    @click="ws.layout.dock = !ws.layout.dock"
                >{{ ws.layout.dock ? '▾ Collapse' : '▴ Expand' }}</button>
            </div>
        </div>
        <div
            v-for="tab in DOCK_TABS"
            v-show="ws.layout.dock && ws.layout.dockTab === tab.id"
            :id="`vhil-tabpanel-${tab.id}`"
            :key="tab.id"
            role="tabpanel"
            class="vhil-tabpanel"
            :aria-labelledby="`vhil-tab-${tab.id}`"
        >
            <Terminal v-if="tab.id === 'log'" :terminalInstance="logName" />
            <VhilBus v-else-if="tab.id === 'bus'" />
            <VhilState v-else-if="tab.id === 'state'" />
            <VhilScenario v-else-if="tab.id === 'scenario'" />
            <VhilDebug v-else-if="tab.id === 'debug'" />
            <VhilSignals v-else-if="tab.id === 'signals'" />
            <VhilArtifacts v-else-if="tab.id === 'artifacts'" />
            <div v-else-if="tab.id === 'problems'" class="vhil-problems">
                <p v-if="!problems.length" class="muted">
                    {{ ws.checked ? 'No problems: the system is valid.'
                        : 'Check (in the inspector) lists them here.' }}
                </p>
                <ul v-else class="vhil-list">
                    <li v-for="(p, i) in problems" :key="i">
                        <button
                            type="button"
                            class="vhil-problem"
                            :class="`--${p.severity}`"
                            :disabled="!p.nodeId && p.rowKey === undefined"
                            :title="p.rowKey !== undefined ? 'Show its scenario row'
                                : (p.nodeId ? 'Select its node' : '')"
                            @click="goTo(p)"
                        >
                            <span class="vhil-problem-sev">
                                {{ p.severity === 'error' ? '✕ error' : '▲ warning' }}
                            </span>
                            <span class="vhil-problem-text">{{ p.text }}</span>
                        </button>
                    </li>
                </ul>
            </div>
            <p v-else class="vhil-placeholder muted">
                {{ tab.label }}: comes with step {{ tab.step }} of the workspace plan.
            </p>
        </div>
    </section>
</template>

<script>
import { computed, defineComponent, onMounted } from 'vue';
import Terminal from '../components/Terminal.vue';
import VhilArtifacts from './VhilArtifacts.vue';
import VhilBus from './VhilBus.vue';
import VhilDebug from './VhilDebug.vue';
import VhilScenario from './VhilScenario.vue';
import VhilSignals from './VhilSignals.vue';
import VhilState from './VhilState.vue';
import { scen } from './scenarios.js';
import { terminalStore, MAIN_TERMINAL } from '../core/stores.js';
import {
    ws, DOCK_TABS, allProblems, problemCount, select,
} from './workspace.js';

export default defineComponent({
    components: {
        Terminal, VhilArtifacts, VhilBus, VhilDebug, VhilScenario, VhilSignals, VhilState,
    },
    setup() {
        const errors = computed(() => problemCount('error'));
        const warnings = computed(() => problemCount('warning'));
        const problems = computed(() => allProblems());
        const goTo = (p) => {
            if (p.rowKey !== undefined) {
                scen.selected = p.rowKey;
                ws.layout.dockTab = 'scenario';
            } else if (p.nodeId) {
                select(p.nodeId, { center: true });
            }
        };
        const pick = (id) => {
            if (ws.layout.dock && ws.layout.dockTab === id) {
                ws.layout.dock = false;
            } else {
                ws.layout.dockTab = id;
                ws.layout.dock = true;
            }
        };
        const step = (d) => {
            const i = DOCK_TABS.findIndex((t) => t.id === ws.layout.dockTab);
            const next = DOCK_TABS[(i + d + DOCK_TABS.length) % DOCK_TABS.length];
            ws.layout.dockTab = next.id;
            ws.layout.dock = true;
            document.getElementById(`vhil-tab-${next.id}`)?.focus();
        };
        const logName = MAIN_TERMINAL;
        const clearLog = () => terminalStore.clear(logName);

        // Pipeline Manager's terminal procedures (terminal_show, _hide,
        // _view) drive its terminal panel through this manager: here, the
        // dock's Log tab.
        onMounted(() => {
            terminalStore.manager = {
                show() {
                    ws.layout.dock = true;
                    ws.layout.dockTab = 'log';
                    terminalStore.show = true;
                },
                hide() { terminalStore.show = false; },
                view() {},
            };
        });

        const clamp = (h) => Math.max(120, Math.min(window.innerHeight - 200, Math.round(h)));
        const resizeBy = (dy) => { ws.layout.dockH = clamp(ws.layout.dockH + dy); };
        const startResize = (ev) => {
            const y0 = ev.clientY;
            const h0 = ws.layout.dockH;
            const move = (e) => { ws.layout.dockH = clamp(h0 + (y0 - e.clientY)); };
            const up = () => {
                document.removeEventListener('pointermove', move);
                document.removeEventListener('pointerup', up);
            };
            document.addEventListener('pointermove', move);
            document.addEventListener('pointerup', up);
        };

        return {
            ws,
            DOCK_TABS,
            errors,
            warnings,
            problems,
            goTo,
            pick,
            step,
            logName,
            clearLog,
            select,
            resizeBy,
            startResize,
        };
    },
});
</script>
