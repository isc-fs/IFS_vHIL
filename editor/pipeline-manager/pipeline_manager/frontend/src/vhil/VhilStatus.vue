<!--
vHIL: the status strip, 24 px (CHANGELOG-VHIL.md): the editor backend's
connection, the mode, the run in progress, the last thing the workspace did
(announced politely), the problem count and the selection. In LIVE (step
15): the session's virtual time and real-time factor, each bus's frames a
second, and the periodic senders running with Stop all.
-->

<template>
    <footer class="vhil-status" aria-label="Status">
        <span class="vhil-status-item" :class="`--backend-${ws.backend}`" :title="backendTitle">
            <span aria-hidden="true">{{ backendGlyph }}</span> backend {{ ws.backend }}
        </span>
        <span class="vhil-status-item">{{ ws.mode }}</span>
        <span v-if="ws.run" class="vhil-status-item">
            <a :href="runPage(ws.run.id)" target="_blank" rel="noopener">run {{ ws.run.id }}</a>
            <StateBadge :state="ws.run.state" />
            <span class="mono muted">{{ frames }}</span>
        </span>
        <template v-if="live">
            <span class="vhil-status-item mono num" :title="'virtual time · real-time factor'">
                {{ clock }}
            </span>
            <span v-if="rates" class="vhil-status-item mono num" title="frames a second, per bus">
                {{ rates }}
            </span>
            <span v-if="session.periodics.length" class="vhil-status-item">
                ▶ {{ session.periodics.length }} periodic
                <button
                    type="button" class="vhil-btn --small" :disabled="!!cannot" @click="stopAll"
                >Stop all</button>
            </span>
        </template>
        <span class="vhil-status-msg" role="status" aria-live="polite">{{ ws.status }}</span>
        <button
            v-if="errors || warnings"
            type="button"
            class="vhil-status-item vhil-status-problems"
            title="Show Problems"
            @click="showProblems"
        >
            <span v-if="errors" class="vhil-count-error">
                ✕ {{ errors }} error{{ errors === 1 ? '' : 's' }}
            </span>
            <span v-if="warnings" class="vhil-count-warn">
                ▲ {{ warnings }} warning{{ warnings === 1 ? '' : 's' }}
            </span>
        </button>
        <span class="vhil-status-item muted mono">{{ selection }}</span>
    </footer>
</template>

<script>
import { computed, defineComponent } from 'vue';
import { ws, isLive, problemCount } from './workspace.js';
import { cannotSend, live as session, stopAll } from './session.js';
import { clockText } from './live.js';
import { replay } from './replay.js';
import { runPage } from './api.js';
import { nodeById, nodeName } from './graph.js';
// eslint-disable-next-line import/no-unresolved, import/extensions -- copied by the build
import { frameCounts } from './shell/editor-run.js';
import StateBadge from './VhilStateBadge.vue';

export default defineComponent({
    components: { StateBadge },
    props: {
        tick: { type: Number, default: 0 },
    },
    setup(props) {
        const errors = computed(() => problemCount('error'));
        const warnings = computed(() => problemCount('warning'));
        const frames = computed(() => (ws.run ? frameCounts(ws.run.counts) : ''));
        const backendGlyph = computed(() => ({ connected: '●', disconnected: '✕' })[ws.backend] ?? '○');
        const backendTitle = computed(() => (ws.backend === 'connected'
            ? 'The editor backend (vhil.editor) is connected: specification, role changes'
            : 'The editor backend (vhil.editor) is not connected'));
        const selection = computed(() => {
            props.tick; // eslint-disable-line no-unused-expressions
            const n = ws.selectedId ? nodeById(ws.selectedId) : null;
            return n ? `selected: ${nodeName(n)}` : '';
        });
        const showProblems = () => { ws.layout.dock = true; ws.layout.dockTab = 'problems'; };
        const live = computed(() => isLive());
        // Once a second at most (aria-live polite is the message's, not these).
        const clock = computed(() => clockText(Math.floor(replay.end / 1e5) * 1e5,
            session.rtf,
            session.paused));
        const rates = computed(() => Object.entries(session.rates)
            .map(([bus, n]) => `${bus} ${n}/s`).join(' · '));
        const cannot = computed(() => {
            session.version; // eslint-disable-line no-unused-expressions
            return cannotSend();
        });
        return {
            live,
            clock,
            rates,
            session,
            cannot,
            stopAll,
            ws,
            errors,
            warnings,
            frames,
            backendGlyph,
            backendTitle,
            selection,
            showProblems,
            runPage,
        };
    },
});
</script>
