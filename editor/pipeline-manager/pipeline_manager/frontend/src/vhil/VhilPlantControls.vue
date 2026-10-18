<!--
vHIL: a plant's controls in a live session (M8; docs/live-session.md), as a
board's inputs are (VhilInputs.vue): what the contract's `plants` says it
may set (plant.js plantsOf). A boolean fault input is a switch
(role="switch", ON/OFF in text); a number (a fault input such as c_scale or
r_leak, a parameter) is a field with its unit and Set, and a parameter
carries its status (cited, uncommissioned, or override once set); a list
parameter (cells_per_chip) is shown, not set. Each shows the value the
session's last `plant` op set (live.js SessionState), else its default or
the catalogue's; every change is a `plant` op, applied at a slice boundary.
No <select>: nothing here re-patches an open popup (CHANGELOG-VHIL.md 26).
-->

<template>
    <section class="vhil-inputs vhil-plant-controls" :aria-label="`plant ${plant} controls`">
        <h4 class="vhil-inputs-title">
            Set <span class="muted">· plant {{ plant }}</span>
        </h4>
        <p v-if="!settings.length" class="muted">Nothing this plant may set.</p>
        <p v-if="settings.length && cannot" class="vhil-note">{{ cannot }}</p>
        <ul class="vhil-inputs-list">
            <li v-for="e in switches" :key="e.name" class="vhil-input-row">
                <span class="vhil-input-label">
                    <span class="mono">{{ e.name }}</span>
                    <span v-if="e.status" class="vhil-plant-badge" :class="`--${e.status}`">
                        {{ e.status }}
                    </span>
                </span>
                <button
                    type="button" role="switch" class="vhil-switch"
                    :aria-checked="on(e) ? 'true' : 'false'"
                    :aria-label="`${e.name} (plant ${plant})`"
                    :disabled="!!cannot"
                    @click="setValue(e, !on(e))"
                >
                    <span class="vhil-switch-track" aria-hidden="true">
                        <span class="vhil-switch-knob" />
                    </span>
                    <span class="vhil-switch-text mono">{{ on(e) ? 'ON' : 'OFF' }}</span>
                </button>
            </li>
            <li v-for="e in fields" :key="e.name" class="vhil-input-row">
                <label class="vhil-input-label" :for="idOf(e)">
                    <span class="mono">{{ e.name }}</span>
                    <span v-if="e.status" class="vhil-plant-badge" :class="`--${e.status}`">
                        {{ e.status }}
                    </span>
                </label>
                <span class="vhil-analog">
                    <input
                        :id="idOf(e)" v-model="draft[e.name]"
                        :type="e.type === 'number' ? 'number' : 'text'" step="any"
                        class="vhil-input mono" :placeholder="e.nullable ? 'none' : ''"
                        :disabled="!!cannot" :aria-describedby="`${idOf(e)}-now`"
                        @keydown.enter.prevent="setField(e)"
                    />
                    <span v-if="e.unit" class="muted">{{ e.unit }}</span>
                    <button
                        type="button" class="vhil-btn --small" :disabled="!!cannot || !valid(e)"
                        @click="setField(e)"
                    >Set</button>
                    <span :id="`${idOf(e)}-now`" class="muted mono num">now {{ nowText(e) }}</span>
                </span>
            </li>
            <li v-for="e in fixed" :key="e.name" class="vhil-input-row">
                <span class="vhil-input-label">
                    <span class="mono">{{ e.name }}</span>
                    <span v-if="e.status" class="vhil-plant-badge" :class="`--${e.status}`">
                        {{ e.status }}
                    </span>
                </span>
                <span class="muted mono num">{{ nowText(e) }}</span>
            </li>
        </ul>
    </section>
</template>

<script>
import { computed, defineComponent, reactive } from 'vue';
import { replay } from './replay.js';
import {
    parseSetting, plantOp, plantValueText, settingsOf,
} from './plant.js';
import { keep } from './stable.js';
import { cannotSend, live as session, send } from './session.js';

export default defineComponent({
    props: { plant: { type: String, required: true } },
    setup(props) {
        const settings = computed((old) => {
            replay.version; // eslint-disable-line no-unused-expressions
            return keep(old, settingsOf(replay.contract, props.plant));
        });
        const switches = computed(() => settings.value.filter((e) => e.type === 'bool'));
        const fields = computed(() => settings.value
            .filter((e) => e.type === 'number' || e.type === 'text'));
        const fixed = computed(() => settings.value.filter((e) => e.type === 'fixed'));
        // What the session's last plant op set, else the default (a fault
        // input) or the catalogue's value (a parameter).
        const current = (e) => {
            const set = session.plants[props.plant] || {};
            return Object.hasOwn(set, e.name) ? set[e.name] : e.value;
        };
        const on = (e) => Boolean(current(e));
        const nowText = (e) => {
            const v = current(e);
            const text = Array.isArray(v) ? v.join(', ') : plantValueText(v);
            return `${text}${e.unit && v !== null ? ` ${e.unit}` : ''}`;
        };
        const idOf = (e) => `vhil-plant-${props.plant}-${e.name}`;
        const draft = reactive({});
        const cannot = computed(() => {
            session.version; // eslint-disable-line no-unused-expressions
            return session.role ? cannotSend() : null;
        });
        const setValue = (e, value) => send(plantOp(props.plant, e.name, value));
        const valid = (e) => draft[e.name] !== undefined
            && parseSetting(e, draft[e.name]) !== undefined;
        const setField = (e) => {
            if (!valid(e)) return;
            setValue(e, parseSetting(e, draft[e.name]));
        };
        return {
            settings,
            switches,
            fields,
            fixed,
            on,
            nowText,
            idOf,
            draft,
            cannot,
            setValue,
            valid,
            setField,
        };
    },
});
</script>
