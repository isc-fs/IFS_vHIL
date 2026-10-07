<!--
vHIL: the dock's Bus tab (step 9 of docs/architecture/editor-workspace.md;
CHANGELOG-VHIL.md), over a run in REPLAY (LIVE comes with step 15).
Monitor: one row per (bus, id) as of the scrubber's time, with its last
data (the bytes that changed in it marked), its fields decoded from the
firmware's .def contracts, how many came, the mean period and the age.
Trace: every frame in time order, filtered by bus and id, the last one at
the scrubber's time marked and kept in view (follow); a click moves the
scrubber there.

Both tables are rowtable.js (the shell's vtable.js window, rows reused) and
decode with the shell's decode.js, copied into shell/ by the image build:
one source. Only the rows in view are decoded, and a change of time,
filter or frames draws once per animation frame.
-->

<template>
    <div class="vhil-bus">
        <div class="vhil-bus-bar">
            <div class="vhil-seg" role="group" aria-label="Bus view">
                <button
                    v-for="v in VIEWS" :key="v.id" type="button" class="vhil-seg-btn"
                    :aria-pressed="view === v.id" @click="view = v.id"
                >{{ v.label }}</button>
            </div>
            <label class="vhil-bus-field">Bus
                <select v-model="bus" class="vhil-input">
                    <option value="">all</option>
                    <option v-for="b in buses" :key="b" :value="b">{{ b }}</option>
                </select>
            </label>
            <label class="vhil-bus-field --grow">IDs
                <input
                    v-model="ids" class="vhil-input mono" placeholder="0x100, 700-70D, status"
                    :aria-invalid="idsError ? 'true' : 'false'" :title="idsError || idHelp"
                    autocomplete="off" spellcheck="false"
                />
            </label>
            <label v-if="view === 'trace'" class="vhil-bus-check">
                <input v-model="follow" type="checkbox" /> follow
            </label>
            <span class="vhil-bus-summary muted mono" role="status">{{ summary }}</span>
        </div>
        <p v-if="replay.state === 'idle'" class="vhil-placeholder muted">
            Open a run from Runs in the sidebar to replay its frames here (LIVE comes with
            step 15).
        </p>
        <p v-else-if="replay.state === 'loading'" class="vhil-placeholder muted">
            Loading run {{ replay.id }}: {{ replay.loaded.toLocaleString() }} frames…
        </p>
        <p v-else-if="replay.state === 'error'" class="vhil-placeholder vhil-warn">
            Run {{ replay.id }}: {{ replay.error }}
        </p>
        <p v-if="replay.contractNote" class="vhil-bus-note muted">{{ replay.contractNote }}</p>
        <div
            v-show="ready && view === 'monitor'" ref="monitorEl" class="vhil-bus-table --monitor"
        />
        <div v-show="ready && view === 'trace'" ref="traceEl" class="vhil-bus-table --trace" />
    </div>
</template>

<script>
import {
    computed, defineComponent, onBeforeUnmount, onMounted, ref, watch,
} from 'vue';
// eslint-disable-next-line import/no-unresolved, import/extensions -- copied by the build
import * as dec from './shell/decode.js';
import RowTable from './rowtable.js';
import { filterIndices, lastAtOrBefore } from './frames.js';
import { Monitor, meanPeriod } from './monitor.js';
import { replay, store } from './replay.js';
import './bus.css';

const VIEWS = [{ id: 'monitor', label: 'Monitor' }, { id: 'trace', label: 'Trace' }];
// A byte that changed in a frame less than this long before the scrubber
// is marked (virtual µs).
const FRESH_US = 250000;
const BYTES = 8;
const ms = (us) => (us / 1000).toFixed(3);

export default defineComponent({
    setup() {
        const view = ref('monitor');
        const bus = ref('');
        const ids = ref('');
        const idsError = ref('');
        const follow = ref(true);
        const idHelp = 'Hex ids (0x optional), ranges (700-70D) or a part of a message name, '
            + 'comma-separated';
        const monitorEl = ref(null);
        const traceEl = ref(null);
        const ready = computed(() => replay.state === 'ready');
        const buses = computed(() => {
            replay.version; // eslint-disable-line no-unused-expressions
            const known = Object.keys(replay.contract?.buses ?? {});
            return [...new Set([...known, ...store.buses])].sort();
        });

        // -- what the views show (outside Vue: plain arrays and the store) ----
        let monitor = new Monitor(store);
        let rows = []; // Monitor: the rows that pass the filters
        let traceIdx = []; // Trace: frame indices that pass the filters
        let traceFor = ''; // what traceIdx was built for
        let cursor = -1; // Trace: the position of the last frame at the time
        let keep = () => true;
        const decoded = new Map(); // frame index -> decoded text (rows in view only)

        const nameOf = (f) => dec.lookup(replay.contract, f)?.name ?? '';
        const decode = (i) => {
            let text = decoded.get(i);
            if (text === undefined) {
                const f = store.frame(i);
                const msg = dec.lookup(replay.contract, f);
                text = msg ? dec.decodeFrame(msg, f.bytes)
                    .map((x) => `${x.name}=${dec.formatValue(x)}`).join('  ') : '';
                if (decoded.size > 4000) decoded.clear();
                decoded.set(i, text);
            }
            return text;
        };
        const fillBytes = (spans, bytes, changed, fresh) => {
            for (let k = 0; k < spans.length; k += 1) {
                const span = spans[k];
                const has = k < bytes.length;
                const text = has ? bytes[k].toString(16).padStart(2, '0') : '';
                if (span.textContent !== text) span.textContent = text;
                // eslint-disable-next-line no-bitwise -- a bit per changed byte (monitor.js)
                span.classList.toggle('--changed', has && fresh && (changed & (1 << k)) !== 0);
            }
        };
        const idText = (id, ext) => `${dec.hexId(id, ext)}${ext ? 'x' : ''}`;

        const monitorColumns = [
            { label: 'bus', cls: 'vhil-c-bus' },
            { label: 'id', cls: 'vhil-c-id mono' },
            { label: 'message', cls: 'vhil-c-name' },
            { label: 'dlc', cls: 'vhil-c-num mono' },
            { label: 'data', cls: 'vhil-c-data mono', bytes: BYTES },
            { label: 'decoded', cls: 'vhil-c-dec mono' },
            { label: 'count', cls: 'vhil-c-num mono' },
            { label: 'period ms', cls: 'vhil-c-num mono' },
            { label: 'age ms', cls: 'vhil-c-num mono' },
        ];
        const fillMonitor = (i, r) => {
            const row = rows[i];
            if (!row) return;
            const f = store.frame(row.index);
            const fresh = replay.t - row.changedAt < FRESH_US && row.changed !== 0;
            const c = r.cells;
            c[0].textContent = row.bus;
            c[1].textContent = idText(row.id, row.ext);
            c[2].textContent = nameOf(f);
            c[3].textContent = String(f.bytes.length);
            fillBytes(r.bytes[4], f.bytes, row.changed, fresh);
            const text = decode(row.index);
            c[5].textContent = text;
            c[5].title = text;
            c[6].textContent = row.count.toLocaleString();
            const p = meanPeriod(row);
            c[7].textContent = p === null ? '–' : ms(p);
            c[8].textContent = ms(replay.t - row.last);
            r.el.classList.toggle('--fresh', fresh);
            r.el.classList.toggle('--stimulus', row.src);
        };

        const traceColumns = [
            { label: 't ms', cls: 'vhil-c-num mono' },
            { label: 'bus', cls: 'vhil-c-bus' },
            { label: 'id', cls: 'vhil-c-id mono' },
            { label: 'message', cls: 'vhil-c-name' },
            { label: 'dlc', cls: 'vhil-c-num mono' },
            { label: 'data', cls: 'vhil-c-data mono', bytes: BYTES },
            { label: 'decoded', cls: 'vhil-c-dec mono' },
        ];
        const fillTrace = (i, r) => {
            const fi = traceIdx[i];
            if (fi === undefined) return;
            const f = store.frame(fi);
            const c = r.cells;
            c[0].textContent = ms(f.t_us);
            c[1].textContent = f.src ? `⇢ ${f.bus}` : f.bus;
            c[1].title = f.src ? 'sent by the run\'s scenario' : '';
            c[2].textContent = idText(f.id, f.ext);
            c[3].textContent = nameOf(f);
            c[4].textContent = String(f.bytes.length);
            fillBytes(r.bytes[5], f.bytes, 0, false);
            const text = decode(fi);
            c[6].textContent = text;
            c[6].title = text;
            r.el.classList.toggle('--cursor', i === cursor);
            r.el.classList.toggle('--after', i > cursor);
            r.el.setAttribute('aria-current', i === cursor ? 'true' : 'false');
        };

        let monitorTable = null;
        let traceTable = null;

        // -- drawing: at most once per animation frame -------------------------
        let raf = 0;
        let lastVersion = -1;
        const timings = []; // ms per draw, the last 600 (window.vhilBusTimings)

        const draw = () => {
            raf = 0;
            if (!monitorTable || replay.state !== 'ready') return;
            const t0 = performance.now();
            if (lastVersion !== replay.version) {
                // New frames or a new contract: every decode and index again.
                lastVersion = replay.version;
                decoded.clear();
                monitor = new Monitor(store);
                traceFor = '';
            }
            if (view.value === 'monitor') {
                rows = monitor.update(replay.t)
                    .filter((row) => (!bus.value || row.bus === bus.value)
                        && keep(row.id, dec.lookup(replay.contract, row)?.name));
                monitorTable.setCount(rows.length);
            } else {
                const want = `${bus.value}|${ids.value}|${store.version}`;
                if (traceFor !== want) {
                    traceFor = want;
                    traceIdx = filterIndices(store, (i) => {
                        const f = store.frame(i);
                        return (!bus.value || f.bus === bus.value)
                            && keep(f.id, dec.lookup(replay.contract, f)?.name);
                    });
                    traceTable.setCount(traceIdx.length);
                }
                cursor = lastAtOrBefore(store, traceIdx, replay.t);
                if (follow.value && cursor >= 0) traceTable.reveal(cursor, true);
                else traceTable.schedule(true);
            }
            timings.push(performance.now() - t0);
            if (timings.length > 600) timings.shift();
        };
        const schedule = () => { if (!raf) raf = requestAnimationFrame(draw); };

        // The filters: a bad id filter keeps the last good one.
        watch(ids, (text) => {
            try {
                keep = dec.parseIdFilter(text);
                idsError.value = '';
            } catch (e) {
                idsError.value = e.message;
            }
            traceFor = '';
            schedule();
        });
        watch(bus, () => { traceFor = ''; schedule(); });
        watch(view, schedule, { flush: 'post' });
        watch(() => [replay.t, replay.version, replay.state], schedule);

        const pickTime = (i) => {
            const fi = traceIdx[i];
            if (fi === undefined) return;
            follow.value = false;
            replay.t = store.tAt(fi);
        };

        onMounted(() => {
            monitorTable = new RowTable(monitorEl.value, {
                columns: monitorColumns, fill: fillMonitor, cls: '--monitor', label: 'Bus monitor',
            });
            traceTable = new RowTable(traceEl.value, {
                columns: traceColumns,
                fill: fillTrace,
                cls: '--trace',
                label: 'Bus trace',
                onPick: pickTime,
            });
            window.vhilBusTimings = timings;
            schedule();
        });
        onBeforeUnmount(() => {
            cancelAnimationFrame(raf);
            monitorTable?.destroy();
            traceTable?.destroy();
        });

        const summary = computed(() => {
            replay.version; // eslint-disable-line no-unused-expressions
            if (replay.state !== 'ready') return '';
            const kept = store.dropped ? ` (${store.dropped.toLocaleString()} dropped)` : '';
            return `run ${replay.id} · ${store.length.toLocaleString()} frames${kept}`;
        });

        return {
            VIEWS,
            replay,
            view,
            bus,
            buses,
            ids,
            idsError,
            idHelp,
            follow,
            monitorEl,
            traceEl,
            ready,
            summary,
        };
    },
});
</script>
