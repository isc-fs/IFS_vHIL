<!--
vHIL: the dock's Signals tab (step 18 of docs/architecture/editor-workspace.md;
CHANGELOG-VHIL.md). Plots over the virtual time of the run on the workspace
(REPLAY, or a live session as it streams), one per signal, stacked and
sharing the time axis, with the shell's plot.js (uPlot; one copy, copied
into shell/ by the image build): each board's state-view lanes (its FSM
state and relays, with their labels: the default), decoded fields of the
CAN messages the run carried, and sampled firmware globals. The scrubber's
time is a line on every plot, and a click on a plot moves the scrubber.
What is plotted is kept per system for this viewer (localStorage).
-->

<template>
    <div class="vhil-signals">
        <div class="vhil-bus-bar vhil-signals-bar">
            <label class="vhil-bus-field">Lane
                <select class="vhil-input" :disabled="!ready" @change="pick">
                    <option value="">state view lane…</option>
                    <option v-for="k in options.lanes" :key="k" :value="k">{{ label(k) }}</option>
                </select>
            </label>
            <label class="vhil-bus-field">Symbol
                <select class="vhil-input" :disabled="!ready" @change="pick">
                    <option value="">
                        {{ options.samples.length ? 'sampled global…' : 'no samples' }}
                    </option>
                    <option v-for="k in options.samples" :key="k" :value="k">{{ label(k) }}</option>
                </select>
            </label>
            <label class="vhil-bus-field">Message
                <select v-model="msgId" class="vhil-input" :disabled="!ready">
                    <option value="">decoded message…</option>
                    <option v-for="m in options.messages" :key="m.id" :value="m.id">
                        {{ m.bus }} · {{ hex(m.msg) }} {{ m.msg.name }}
                    </option>
                </select>
            </label>
            <label class="vhil-bus-field">Field
                <select v-model="field" class="vhil-input" :disabled="!msgFields.length">
                    <option v-for="f in msgFields" :key="f" :value="f">{{ f }}</option>
                </select>
            </label>
            <button
                type="button" class="vhil-btn --small" :disabled="!msgId || !field"
                @click="addField"
            >+ Plot</button>
            <label class="vhil-bus-check" title="Draw values as steps (lanes always are)">
                <input v-model="stepped" type="checkbox" /> steps
            </label>
            <button
                type="button" class="vhil-btn --small" :disabled="!ready"
                title="Back to every board's state-view lanes" @click="reset"
            >Reset</button>
            <span class="vhil-bus-summary muted mono" role="status">{{ summary }}</span>
        </div>
        <p v-if="replay.state === 'idle'" class="vhil-placeholder muted">
            Open a run from Runs in the sidebar, or start a live session (● Live): its
            boards' state lanes, decoded CAN fields and sampled globals plot here over
            virtual time.
        </p>
        <p v-else-if="replay.state === 'loading'" class="vhil-placeholder muted">
            Loading run {{ replay.id }}: {{ replay.loaded.toLocaleString() }} frames…
        </p>
        <p v-else-if="replay.state === 'error'" class="vhil-placeholder vhil-warn">
            Run {{ replay.id }}: {{ replay.error }}
        </p>
        <template v-else>
            <ul v-if="keys.length" class="vhil-signal-chips" aria-label="Plotted signals">
                <li v-for="k in keys" :key="k" class="vhil-signal-chip">
                    <span class="mono">{{ label(k) }}</span>
                    <button
                        type="button" class="vhil-btn --small" :aria-label="`Remove ${label(k)}`"
                        @click="remove(k)"
                    >✕</button>
                </li>
            </ul>
            <p class="vhil-bus-note muted">
                Drag to zoom · Ctrl+wheel zooms · Shift+drag pans · double-click shows the whole
                run · a click moves the scrubber.
                <template v-if="!replay.stateTrace"> Waiting for the run's contract (its state
                    views and messages)…</template>
            </p>
        </template>
        <div v-show="ready" ref="plotsEl" class="vhil-signal-plots" />
        <p v-if="ready && !keys.length" class="vhil-placeholder muted">
            Nothing plotted: pick a lane, a symbol or a message's field above.
        </p>
    </div>
</template>

<script>
import {
    computed, defineComponent, onBeforeUnmount, onMounted, ref, watch,
} from 'vue';
import { replay, store } from './replay.js';
import { ws } from './workspace.js';
import {
    addKey, choices, frameKey, labelOf, laneKeys, seriesOf,
} from './signals.js';
/* eslint-disable import/no-unresolved, import/extensions -- copied by the build */
import { physical, rawValue } from './shell/decode.js';
import { PlotGroup, cssProvided, palette } from './shell/plot.js';
import './shell/vendor/uplot/uPlot.min.css';
/* eslint-enable import/no-unresolved, import/extensions */
import './signals.css';

cssProvided();

const KEY = (system) => `vhil.signals.${system}`;
function stored(system) {
    try { return JSON.parse(window.localStorage.getItem(KEY(system))); } catch { return null; }
}
function keep(system, keys) {
    try {
        if (keys === null) window.localStorage.removeItem(KEY(system));
        else window.localStorage.setItem(KEY(system), JSON.stringify(keys));
    } catch { /* not kept */ }
}

const LIVE_REDRAW_MS = 1000; // a live session's plots rebuild at most this often

export default defineComponent({
    setup() {
        const plotsEl = ref(null);
        const chosen = ref(null); // the viewer's keys, or null: the default lanes
        const stepped = ref(true);
        const msgId = ref('');
        const field = ref('');
        let group = null;
        let drawn = 0;
        let pending = null;

        const ready = computed(() => replay.state === 'ready');
        const tr = computed(() => {
            replay.version; // eslint-disable-line no-unused-expressions
            return replay.stateTrace;
        });
        const keys = computed(() => chosen.value ?? laneKeys(tr.value));
        // Rebuilt with the plots (at most once a second while live), not per
        // record batch: listing the messages walks every frame.
        const drawCount = ref(0);
        const options = computed(() => {
            drawCount.value; // eslint-disable-line no-unused-expressions
            if (!ready.value) return { lanes: [], samples: [], messages: [] };
            return choices(store, replay.contract, replay.samples, tr.value);
        });
        const msgFields = computed(() => options.value.messages
            .find((m) => m.id === msgId.value)?.fields || []);
        watch(msgFields, (f) => { field.value = f[0] || ''; });
        const label = (k) => labelOf(k, replay.contract);
        const hex = (m) => `0x${m.id.toString(16).toUpperCase().padStart(m.ext ? 8 : 3, '0')}`;
        const summary = computed(() => (ready.value
            ? `${keys.value.length} signal(s) · ${store.length.toLocaleString()} frames` : ''));

        const setKeys = (next) => {
            chosen.value = next;
            if (ws.id) keep(ws.id, next);
        };
        const add = (k) => setKeys(addKey(keys.value, k));
        // A one-shot select: back to its placeholder once a signal is added.
        const pick = (ev) => {
            const select = ev.target;
            const k = select.value;
            select.selectedIndex = 0;
            add(k);
        };
        const addField = () => {
            const [bus, id] = msgId.value.split('/');
            add(frameKey(bus, id, field.value));
        };
        const remove = (k) => setKeys(keys.value.filter((x) => x !== k));
        const reset = () => setKeys(null);

        const visible = () => ws.layout.dock && ws.layout.dockTab === 'signals';
        function draw() {
            pending = null;
            drawn = Date.now();
            drawCount.value += 1;
            const el = plotsEl.value;
            if (!el) return;
            const range = group?.range ?? null;
            group?.destroy();
            el.replaceChildren();
            group = null;
            if (!ready.value || !keys.value.length) return;
            group = new PlotGroup({
                onPick: (t) => { replay.t = Math.max(0, Math.min(replay.end, t)); },
            });
            group.range = range;
            const colors = palette();
            const series = seriesOf(keys.value, {
                store,
                contract: replay.contract,
                samples: replay.samples,
                stateTrace: tr.value,
                endUs: replay.end,
                rawOf: rawValue,
                physOf: physical,
                stepped: stepped.value,
            });
            series.forEach((s, n) => {
                const div = document.createElement('div');
                div.className = 'vhil-signal-plot';
                el.appendChild(div);
                if (!s.x.length) {
                    const p = document.createElement('p');
                    p.className = 'muted';
                    p.textContent = `${s.label}: no data in this run${replay.live ? ' yet' : ''}`;
                    div.appendChild(p);
                    return;
                }
                group.add(div, {
                    label: s.label,
                    unit: s.unit,
                    x: s.x,
                    y: s.y,
                    stepped: s.stepped,
                    color: colors[n % colors.length],
                    height: s.format ? 110 : 140,
                    format: s.format || null,
                    levels: s.levels || null,
                });
            });
            group.setRange(range);
            group.setMarker(replay.t);
        }
        // Whole rebuilds (uPlot is fast); while live, once a second at most;
        // none while the tab is hidden (it draws when shown).
        let stale = true;
        function schedule() {
            if (!visible()) {
                stale = true;
                return;
            }
            stale = false;
            if (pending) return;
            const wait = replay.live ? Math.max(0, LIVE_REDRAW_MS - (Date.now() - drawn)) : 0;
            pending = setTimeout(() => requestAnimationFrame(draw), wait);
        }

        watch(() => ws.id, (id) => { chosen.value = id ? stored(id) : null; }, { immediate: true });
        watch(() => [keys.value.join('|'), stepped.value, replay.version, replay.state,
            replay.id, ws.theme], schedule);
        watch(() => replay.tick, schedule);
        watch(() => replay.t, (t) => group?.setMarker(t));
        // Shown again (another dock tab, a collapsed dock): draw what changed.
        watch(visible, (shown) => {
            if (shown && stale) schedule();
        });
        const media = window.matchMedia('(prefers-color-scheme: dark)');
        onMounted(() => {
            media.addEventListener('change', schedule);
            schedule();
        });
        onBeforeUnmount(() => {
            media.removeEventListener('change', schedule);
            clearTimeout(pending);
            group?.destroy();
        });

        return {
            replay,
            plotsEl,
            ready,
            keys,
            options,
            msgId,
            field,
            msgFields,
            stepped,
            summary,
            label,
            hex,
            pick,
            addField,
            remove,
            reset,
        };
    },
});
</script>
