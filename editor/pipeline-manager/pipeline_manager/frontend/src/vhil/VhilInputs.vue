<!--
vHIL: a board's inputs in a live session (step 15 of
docs/architecture/editor-workspace.md; docs/live-session.md): each GPIO
input its role's backplane routes as a switch (role="switch", HIGH/LOW in
text), each analog input as a voltage with its unit and Set, from the
contract's `inputs` (vhil/stateview.py). Shown in the inspector and in the
board's context menu ("Inputs and analog…"). A switch reads the level the
session's last gpio op set (not set: LOW, the input's pull-down); every
change is a session op, applied at a slice boundary.
-->

<template>
    <section class="vhil-inputs" :aria-label="`${board} inputs`">
        <h4 class="vhil-inputs-title">
            Inputs <span class="muted">· {{ board }}</span>
        </h4>
        <p v-if="!pins.length" class="muted">No inputs this board's role routes.</p>
        <p v-if="pins.length && cannot" class="vhil-note">{{ cannot }}</p>
        <ul class="vhil-inputs-list">
            <li v-for="p in gpios" :key="p.pin" class="vhil-input-row">
                <span class="vhil-input-label">
                    {{ p.label }} <span class="muted mono">{{ p.pin }}</span>
                </span>
                <button
                    type="button" role="switch" class="vhil-switch"
                    :aria-checked="level(p) ? 'true' : 'false'"
                    :aria-label="`${p.label} (${board}.${p.pin})`"
                    :disabled="!!cannot"
                    @click="toggle(p)"
                >
                    <span class="vhil-switch-track" aria-hidden="true">
                        <span class="vhil-switch-knob" />
                    </span>
                    <span class="vhil-switch-text mono">{{ level(p) ? 'HIGH' : 'LOW' }}</span>
                </button>
            </li>
            <li v-for="p in analogs" :key="p.pin" class="vhil-input-row">
                <label class="vhil-input-label" :for="`vhil-in-${board}-${p.pin}`">
                    {{ p.label }} <span class="muted mono">{{ p.pin }}</span>
                </label>
                <span class="vhil-analog">
                    <input
                        :id="`vhil-in-${board}-${p.pin}`" v-model.number="draft[p.pin]"
                        type="number" min="0" max="3.6" step="0.01" class="vhil-input mono"
                        :disabled="!!cannot" :aria-describedby="`vhil-in-${board}-${p.pin}-now`"
                        @keydown.enter.prevent="setVolts(p)"
                    />
                    <span class="muted">V</span>
                    <button
                        type="button" class="vhil-btn --small" :disabled="!!cannot || !valid(p)"
                        @click="setVolts(p)"
                    >Set</button>
                    <span :id="`vhil-in-${board}-${p.pin}-now`" class="muted mono num">
                        {{ voltsNow(p) }}
                    </span>
                </span>
            </li>
        </ul>
    </section>
</template>

<script>
import { computed, defineComponent, reactive } from 'vue';
import { replay } from './replay.js';
import { inputsOf } from './live.js';
import { cannotSend, live as session, send } from './session.js';

export default defineComponent({
    props: { board: { type: String, required: true } },
    setup(props) {
        const pins = computed(() => {
            replay.version; // eslint-disable-line no-unused-expressions
            return inputsOf(replay.contract, props.board);
        });
        const gpios = computed(() => pins.value.filter((p) => p.kind === 'gpio'));
        const analogs = computed(() => pins.value.filter((p) => p.kind === 'analog'));
        const key = (p) => `${props.board}.${p.pin}`;
        const level = (p) => Boolean(session.levels[key(p)]);
        const draft = reactive({});
        const cannot = computed(() => {
            session.version; // eslint-disable-line no-unused-expressions
            return session.role ? cannotSend() : null;
        });
        const toggle = (p) => send({
            kind: 'gpio', board: props.board, pin: p.pin, level: !level(p),
        });
        const valid = (p) => {
            const v = draft[p.pin];
            return typeof v === 'number' && v >= 0 && v <= 3.6;
        };
        const setVolts = (p) => {
            if (!valid(p)) return;
            send({
                kind: 'analog', board: props.board, pin: p.pin, volts: draft[p.pin],
            });
        };
        const voltsNow = (p) => {
            const v = session.volts[key(p)];
            return v === undefined ? 'not set' : `now ${v} V`;
        };
        return {
            pins, gpios, analogs, level, draft, cannot, toggle, valid, setVolts, voltsNow,
        };
    },
});
</script>
