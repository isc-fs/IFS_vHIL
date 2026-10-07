<!--
vHIL: a board node's state pill on the canvas (step 13 of
docs/architecture/editor-workspace.md), in the slot step 8 left on the board
card: "AMS · Precharge" at the scrubber's time in REPLAY, ringed and marked
"✕ fault" while a fault is active, dashed and marked "stale" when its state
went silent. Nothing outside REPLAY yet (LIVE comes with the live session).
-->

<template>
    <span
        v-if="pill"
        class="vhil-node-pill mono"
        :class="{ '--fault': pill.faulted, '--stale': pill.stale }"
        :title="title"
    >{{ pill.text }}<span v-if="pill.faulted" class="vhil-node-pill-flag"> ✕ fault</span><span
        v-else-if="pill.stale" class="vhil-node-pill-flag"
    > stale</span></span>
</template>

<script>
import { computed, defineComponent } from 'vue';
import { replay } from './replay.js';

export default defineComponent({
    props: { board: { type: String, required: true } },
    setup(props) {
        const pill = computed(() => {
            replay.version; // eslint-disable-line no-unused-expressions
            const tr = replay.stateTrace;
            if (!tr || !tr.items.has(props.board)) return null;
            return tr.pillAt(props.board, replay.t);
        });
        const title = computed(() => {
            const p = pill.value;
            if (!p) return '';
            const parts = [`${p.text} at t=${(replay.t / 1e6).toFixed(3)} s`];
            if (p.faults.length) parts.push(`active: ${p.faults.join(', ')}`);
            if (p.stale) parts.push('stale: its state went silent');
            return parts.join('\n');
        });
        return { pill, title };
    },
});
</script>
