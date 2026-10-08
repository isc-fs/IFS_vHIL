<!--
vHIL: the dock's State tab (step 13 of docs/architecture/editor-workspace.md;
docs/state-view.md): a card per board of the run in REPLAY at the
scrubber's time (VhilStateCard.vue), with its transitions (a click moves the
scrubber); its FSM and relay lanes are plotted in the Signals tab. The data
is the run's own trace (frames, samples, edges), as the worker recorded it
for each board's state view; a live session feeds the same model (state.js)
as its records stream.
In a live session, each board's debugger watch list too (VhilWatches.vue,
step 17): the same list as the Debug tab's, its values at the board's stop.
-->

<template>
    <div class="vhil-state">
        <p v-if="replay.state === 'idle'" class="vhil-placeholder muted">
            Open a run from Runs in the sidebar, or start a live session (● Live): each
            board's state shows here at the scrubber's time, or as it happens.
        </p>
        <p v-else-if="replay.state === 'loading'" class="vhil-placeholder muted">
            Loading run {{ replay.id }}…
        </p>
        <p v-else-if="replay.state === 'error'" class="vhil-placeholder vhil-warn">
            Run {{ replay.id }}: {{ replay.error }}
        </p>
        <p v-else-if="!tr" class="vhil-placeholder muted">
            Waiting for run {{ replay.id }}'s contract (its boards' state views)…
        </p>
        <p v-else-if="!tr.boards.length" class="vhil-placeholder muted">
            No board of this system has a state view in the catalogue.
        </p>
        <template v-else>
            <p v-if="notes.length" class="vhil-bus-note muted">{{ notes.join(' · ') }}</p>
            <div class="vhil-state-cards">
                <VhilStateCard
                    v-for="b in tr.boards" :key="b"
                    :trace="tr" :board="b" :t="replay.t" :end="replay.end"
                    :version="replay.version" history
                    @seek="seek" @lanes="toSignals"
                />
            </div>
            <section v-if="replay.live" class="vhil-state-watches" aria-label="Debugger watches">
                <h4>Debugger watches <span class="muted">· read at each stop (Debug tab)</span></h4>
                <div v-for="b in tr.boards" :key="b" class="vhil-state-watch">
                    <h5 class="mono">{{ b }}</h5>
                    <VhilWatches :board="b" />
                </div>
            </section>
        </template>
    </div>
</template>

<script>
import { computed, defineComponent } from 'vue';
import VhilStateCard from './VhilStateCard.vue';
import VhilWatches from './VhilWatches.vue';
import { replay } from './replay.js';
import { ws } from './workspace.js';
import './debug.css';

export default defineComponent({
    components: { VhilStateCard, VhilWatches },
    setup() {
        const tr = computed(() => {
            replay.version; // eslint-disable-line no-unused-expressions
            return replay.stateTrace;
        });
        // What the views couldn't resolve, and a run recorded before the
        // worker kept the state views (no samples at all).
        const notes = computed(() => {
            const c = replay.contract || {};
            const out = Object.values(c.state || {}).flatMap((v) => v.errors || []);
            if (tr.value && !replay.live && !replay.samples.length) {
                out.push('this run recorded no symbol samples: its states show "no data"');
            }
            return out;
        });
        const seek = (t) => { replay.t = Math.max(0, Math.min(replay.end, t)); };
        const toSignals = () => {
            ws.layout.dock = true;
            ws.layout.dockTab = 'signals';
        };
        return {
            replay, tr, notes, seek, toSignals,
        };
    },
});
</script>
