<!--
vHIL: a system's plants in REPLAY and LIVE (M8; docs/live-session.md): each
plant the run's contract describes (its `plants`) or its trace recorded, as
a state card at the scrubber's time (VhilPlantCard.vue, following the clock
as the boards' cards do) and, in a live session, its controls
(VhilPlantControls.vue). In the State tab, and in the inspector (`compact`)
with nothing selected. Nothing for a system without plants.
-->

<template>
    <section
        v-if="list.length" class="vhil-plants" :class="{ '--compact': compact }"
        aria-label="Plants"
    >
        <div v-for="p in list" :key="p.name" class="vhil-plant">
            <VhilPlantCard
                :trace="replay.plants" :plant="p.name" :kind="p.plant" :t="replay.t"
                :version="replay.version + replay.tick" :compact="compact"
            />
            <VhilPlantControls v-if="live" :plant="p.name" />
        </div>
    </section>
</template>

<script>
import { computed, defineComponent } from 'vue';
import VhilPlantCard from './VhilPlantCard.vue';
import VhilPlantControls from './VhilPlantControls.vue';
import { plantsOf } from './plant.js';
import { replay } from './replay.js';
import { keep } from './stable.js';
import { ws, isLive } from './workspace.js';

export default defineComponent({
    components: { VhilPlantCard, VhilPlantControls },
    props: { compact: { type: Boolean, default: false } },
    setup() {
        const live = computed(() => isLive());
        // The contract's plants, and any the trace recorded that it lacks.
        const list = computed((old) => {
            replay.version; // eslint-disable-line no-unused-expressions
            replay.tick; // eslint-disable-line no-unused-expressions
            if (replay.state !== 'ready' || !(ws.mode === 'REPLAY' || live.value)) {
                return keep(old, []);
            }
            const out = plantsOf(replay.contract).map((p) => ({ name: p.name, plant: p.plant }));
            replay.plants.plants().forEach((name) => {
                if (!out.some((p) => p.name === name)) out.push({ name, plant: '' });
            });
            return keep(old, out);
        });
        return { replay, live, list };
    },
});
</script>
