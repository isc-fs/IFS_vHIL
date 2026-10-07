<!--
vHIL: the Bus tab's Send panel (step 15 of docs/architecture/editor-workspace.md;
docs/live-session.md): a frame composed from the firmware's .def contract
(a message's decoded fields: a value table as a select, a bit as
false/true, a scalar as a number with its unit and range, the hex in sync,
as the Scenario tab's row editor does; the shell's decode.js) or as a raw id
and hex, sent once or every N ms as a named periodic sender (a period under
5 ms asks first). Below, the senders running, each with Stop, and Stop all.
Every send is a live session op: the worker applies it at a slice boundary
and records it in the trace.
-->

<template>
    <div class="vhil-send">
        <form class="vhil-send-form" aria-label="Send a frame" @submit.prevent="submit">
            <label>Bus
                <select v-model="bus" class="vhil-input">
                    <option v-for="b in buses" :key="b" :value="b">{{ b }}</option>
                </select>
            </label>
            <label>Message
                <select class="vhil-input" :value="msg ? String(msg.id) : ''" @change="pickMessage">
                    <option value="">raw id…</option>
                    <option v-for="m in messages" :key="m.id" :value="String(m.id)">
                        {{ hexId(m.id, m.ext) }} {{ m.name }}
                    </option>
                </select>
            </label>
            <label>Id (hex)
                <input
                    :value="hexId(id, ext)" class="vhil-input mono --id"
                    @change="(ev) => setId(ev.target.value)"
                />
            </label>
            <label class="vhil-send-check"><input v-model="ext" type="checkbox" /> ext</label>
            <label class="--grow">Data (hex)
                <input
                    :value="spaced(data)" class="vhil-input mono"
                    :aria-invalid="badHex ? 'true' : 'false'"
                    @change="(ev) => setData(ev.target.value)"
                />
            </label>
            <div class="vhil-seg" role="radiogroup" aria-label="Send once or periodically">
                <button
                    type="button" role="radio" class="vhil-seg-btn" :aria-checked="!periodic"
                    @click="periodic = false"
                >Once</button>
                <button
                    type="button" role="radio" class="vhil-seg-btn" :aria-checked="periodic"
                    @click="periodic = true"
                >Every</button>
            </div>
            <template v-if="periodic">
                <label>Period (ms)
                    <input
                        v-model.number="periodMs" type="number" min="0.5" max="60000" step="1"
                        class="vhil-input mono --num"
                    />
                </label>
                <label>Name
                    <input
                        v-model.trim="name" class="vhil-input mono --name" :placeholder="autoName"
                    />
                </label>
            </template>
            <button
                type="submit" class="vhil-btn --primary" :disabled="!!blocked"
                :title="blocked || ''"
            >
                {{ periodic ? '▶ Start' : '➤ Send' }}
            </button>
            <span v-if="blocked" class="vhil-send-why muted">{{ blocked }}</span>
        </form>
        <fieldset v-if="msg" class="vhil-send-fields">
            <legend>{{ msg.name }} · {{ msg.dlc }} bytes · from {{ msg.sender }}</legend>
            <label v-for="f in fields" :key="f.name">
                <span>{{ f.name }}<span class="muted">{{ unitOf(f) }}</span></span>
                <select
                    v-if="f.values" class="vhil-input mono" :value="String(f.raw)"
                    @change="(ev) => setField(f, Number(ev.target.value), true)"
                >
                    <option v-for="(label, raw) in f.values" :key="raw" :value="raw">
                        {{ label }} ({{ raw }})
                    </option>
                    <option v-if="!(String(f.raw) in f.values)" :value="String(f.raw)">
                        {{ f.raw }}
                    </option>
                </select>
                <select
                    v-else-if="f.length === 1" class="vhil-input mono" :value="String(f.raw ?? 0)"
                    @change="(ev) => setField(f, Number(ev.target.value), true)"
                >
                    <option value="0">0 · false</option>
                    <option value="1">1 · true</option>
                </select>
                <input
                    v-else type="number" class="vhil-input mono" :value="f.value ?? ''"
                    :min="f.min" :max="f.max" :step="f.factor"
                    :title="`${f.min} .. ${f.max}${f.unit ? ` ${f.unit}` : ''}`"
                    @change="(ev) => setField(f, ev.target.value, false)"
                />
            </label>
        </fieldset>
        <p v-else-if="!contract" class="vhil-bus-note muted">
            No contract yet: frames are raw id and hex.
        </p>

        <section class="vhil-send-running" aria-label="Periodic senders running">
            <h4>
                Periodic senders
                <span class="muted">({{ session.periodics.length }})</span>
                <button
                    v-if="session.periodics.length" type="button" class="vhil-btn --small"
                    :disabled="!!cannot" @click="stopAll"
                >■ Stop all</button>
            </h4>
            <p v-if="!session.periodics.length" class="muted">None running.</p>
            <ul v-else>
                <li v-for="p in session.periodics" :key="p.name" class="vhil-send-periodic">
                    <span class="mono">{{ p.name }}</span>
                    <span>{{ p.bus }}</span>
                    <span class="mono">{{ hexId(p.id, p.ext) }} {{ nameOf(p) }}</span>
                    <span class="mono">[{{ spaced(p.data) }}]</span>
                    <span class="mono num">every {{ p.period_ms }} ms</span>
                    <span class="mono num muted">since {{ (p.since / 1e6).toFixed(3) }} s</span>
                    <button
                        type="button" class="vhil-btn --small" :disabled="!!cannot"
                        :aria-label="`Stop ${p.name}`" @click="stop(p.name)"
                    >■ Stop</button>
                </li>
            </ul>
        </section>
    </div>
</template>

<script>
import {
    computed, defineComponent, ref, watch,
} from 'vue';
// eslint-disable-next-line import/no-unresolved, import/extensions -- copied by the build
import * as dec from './shell/decode.js';
import { messageOf } from './scenario.js';
import { replay, store } from './replay.js';
import {
    FAST_PERIOD_MS, periodicName, sendOp, sendProblem,
} from './live.js';
import {
    cannotSend, live as session, send, stopAll,
} from './session.js';

const PLAIN_UNITS = ['', 'enum', 'bool', 'bit'];

export default defineComponent({
    setup() {
        const contract = computed(() => {
            replay.version; // eslint-disable-line no-unused-expressions
            return replay.contract;
        });
        const buses = computed(() => {
            replay.version; // eslint-disable-line no-unused-expressions
            const known = Object.keys(contract.value?.buses ?? {});
            return [...new Set([...known, ...store.buses])].sort();
        });
        const bus = ref('');
        const id = ref(0x100);
        const ext = ref(false);
        const data = ref('');
        const periodic = ref(false);
        const periodMs = ref(10);
        const name = ref('');
        const badHex = ref(false);
        watch(buses, (list) => {
            if (!bus.value && list.length) [bus.value] = list;
        }, { immediate: true });

        const hexId = (n, x) => dec.hexId(Number(n) || 0, x);
        const spaced = (hex) => (hex || '').match(/../g)?.join(' ') ?? '';
        const unitOf = (f) => (PLAIN_UNITS.includes(f.unit) ? '' : ` (${f.unit})`);
        const messages = computed(() => Object.values(contract.value?.buses?.[bus.value] || {}));
        const msg = computed(() => {
            const m = messageOf(contract.value, bus.value, id.value);
            return m && !!m.ext === ext.value ? m : null;
        });
        const nameOf = (p) => messageOf(contract.value, p.bus, p.id)?.name ?? '';
        const pad = (bytes, n) => {
            if (bytes.length >= n) return bytes;
            const out = new Uint8Array(n);
            out.set(bytes);
            return out;
        };
        const fields = computed(() => {
            if (!msg.value) return [];
            const bytes = dec.hexToBytes(data.value);
            return msg.value.fields.map((f) => {
                const raw = dec.rawValue(bytes, f);
                const [min, max] = dec.fieldRange(f);
                return {
                    ...f, raw, value: dec.physical(raw, f), min, max, def: f,
                };
            });
        });
        const setField = (f, value, isRaw) => {
            const raw = isRaw ? value : dec.rawFromPhysical(value, f.def);
            const bytes = pad(dec.hexToBytes(data.value), msg.value.dlc);
            data.value = dec.bytesToHex(dec.encodeField(bytes, f.def, raw));
        };
        const setData = (text) => {
            const hex = text.replace(/\s+/g, '').toLowerCase();
            badHex.value = !/^([0-9a-f]{2})*$/.test(hex) || hex.length > 128;
            if (!badHex.value) data.value = hex;
        };
        const setId = (text) => {
            const n = parseInt(String(text).replace(/^0x/i, ''), 16);
            if (Number.isNaN(n)) return;
            id.value = n;
            if (n > 0x7ff) ext.value = true;
        };
        const pickMessage = (ev) => {
            const m = messages.value.find((x) => String(x.id) === ev.target.value);
            if (!m) return;
            id.value = m.id;
            ext.value = !!m.ext;
            data.value = dec.bytesToHex(pad(dec.hexToBytes(data.value), m.dlc).slice(0, m.dlc));
            if (m.period_ms) periodMs.value = m.period_ms;
        };

        const autoName = computed(() => periodicName(session.used, id.value));
        const cannot = computed(() => {
            session.version; // eslint-disable-line no-unused-expressions
            return cannotSend();
        });
        const frame = computed(() => ({
            bus: bus.value,
            id: id.value,
            ext: ext.value,
            data: data.value,
            periodMs: periodic.value ? Number(periodMs.value) : null,
        }));
        const blocked = computed(() => cannot.value || sendProblem(frame.value)
            || (badHex.value ? 'data must be hex' : null)
            || (periodic.value && session.used.includes(name.value || autoName.value)
                ? `'${name.value || autoName.value}' ran in this session already: name it anew` : null));
        const submit = () => {
            if (blocked.value) return;
            const f = frame.value;
            if (f.periodMs && f.periodMs < FAST_PERIOD_MS
                // eslint-disable-next-line no-alert
                && !window.confirm(`Send ${hexId(f.id, f.ext)} every ${f.periodMs} ms? That is `
                    + `${Math.round(1000 / f.periodMs)} frames a second on ${f.bus}.`)) return;
            const ok = send(sendOp({ ...f, name: name.value || autoName.value }));
            if (ok && f.periodMs) name.value = '';
        };
        const stop = (periodicNameToStop) => send({ kind: 'stop_periodic', periodic: periodicNameToStop });

        return {
            session,
            contract,
            buses,
            bus,
            id,
            ext,
            data,
            periodic,
            periodMs,
            name,
            autoName,
            badHex,
            hexId,
            spaced,
            unitOf,
            messages,
            msg,
            nameOf,
            fields,
            setField,
            setData,
            setId,
            pickMessage,
            cannot,
            blocked,
            submit,
            stop,
            stopAll,
        };
    },
});
</script>
