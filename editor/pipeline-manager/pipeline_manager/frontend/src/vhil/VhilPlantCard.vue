<!--
vHIL: one plant's state card (M8; docs/scenarios.md, "Signals"), at a
virtual time, from the trace's `plant` records (plant.js PlantTrace): the
DC link and the pack voltage, the pack current, AIR+, AIR-, the precharge
relay, the coil supply (SDC) and the bleed as square pills with text
(filled = closed or on), and any other key the plant records as
name=value. Laid out as a board's card (state.css), status always text and
a glyph, never colour alone.
-->

<template>
    <article
        class="vhil-state-card vhil-plant-card" :class="{ '--compact': compact }"
        :aria-label="`plant ${plant} state`"
    >
        <header class="vhil-state-head">
            <h3 class="vhil-state-board">plant {{ plant }}</h3>
            <span v-if="kind" class="vhil-state-fw mono">{{ kind }}</span>
        </header>
        <p v-if="!card.has" class="muted">No state recorded for plant {{ plant }} yet.</p>
        <ul class="vhil-state-pills" aria-label="Contacts, coil supply and bleed">
            <li
                v-for="p in pills" :key="p.key"
                class="vhil-relay" :class="`--${p.look.cls}`" :title="`${p.label}: ${p.word}`"
            >
                <span aria-hidden="true">{{ p.look.glyph }}</span>
                {{ p.label }}
                <span class="vhil-visually-hidden">{{ p.word }}</span>
            </li>
        </ul>
        <dl class="vhil-state-values">
            <div
                v-for="v in card.values" :key="v.key" class="vhil-state-value"
                :class="{ '--none': !v.has }"
            >
                <dt>{{ v.label }}</dt>
                <dd class="mono num">
                    {{ v.text }}<span
                        v-if="v.has && v.unit" class="vhil-state-unit"
                    >{{ v.unit }}</span>
                </dd>
            </div>
        </dl>
        <p v-if="card.extra.length" class="vhil-plant-extra mono muted">
            {{ card.extra.map((e) => e.text).join(' · ') }}
        </p>
    </article>
</template>

<script>
import { computed, defineComponent } from 'vue';
import './state.css';

// A pill: closed or on (filled ■), open or off (□), or not yet recorded.
const LOOKS = {
    on: { cls: 'on', glyph: '■' },
    off: { cls: 'off', glyph: '□' },
    none: { cls: 'unknown', glyph: '?' },
};

export default defineComponent({
    props: {
        trace: { type: Object, required: true },
        plant: { type: String, required: true },
        kind: { type: String, default: '' },
        t: { type: Number, required: true },
        version: { type: Number, default: 0 },
        compact: { type: Boolean, default: false },
    },
    setup(props) {
        const card = computed(() => {
            props.version; // eslint-disable-line no-unused-expressions
            return props.trace.cardAt(props.plant, props.t);
        });
        const pills = computed(() => card.value.pills.map((p) => {
            let look = LOOKS.none;
            if (p.on !== null) look = p.on ? LOOKS.on : LOOKS.off;
            return { ...p, look };
        }));
        return { card, pills };
    },
});
</script>
