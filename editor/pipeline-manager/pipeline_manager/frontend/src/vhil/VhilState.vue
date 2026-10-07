<!--
vHIL: the dock's State tab (step 13 of docs/architecture/editor-workspace.md;
docs/state-view.md): a card per board of the run in REPLAY at the
scrubber's time (VhilStateCard.vue), with its history; a click on a lane or
a transition moves the scrubber. The data is the run's own trace (frames,
samples, edges), as the worker recorded it for each board's state view; a
live session will feed the same model (state.js) as its records stream.
-->

<template>
    <div class="vhil-state">
        <p v-if="replay.state === 'idle'" class="vhil-placeholder muted">
            Open a run from Runs in the sidebar: each board's state shows here at the
            scrubber's time (LIVE comes with the live session).
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
                    @seek="seek"
                />
            </div>
        </template>
    </div>
</template>

<script>
import { computed, defineComponent } from 'vue';
import VhilStateCard from './VhilStateCard.vue';
import { replay } from './replay.js';

export default defineComponent({
    components: { VhilStateCard },
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
            if (tr.value && !replay.samples.length) {
                out.push('this run recorded no symbol samples: its states show "no data"');
            }
            return out;
        });
        const seek = (t) => { replay.t = Math.max(0, Math.min(replay.end, t)); };
        return {
            replay, tr, notes, seek,
        };
    },
});
</script>
