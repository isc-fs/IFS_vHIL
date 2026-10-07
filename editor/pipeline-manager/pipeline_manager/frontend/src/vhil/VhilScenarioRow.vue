<!--
vHIL: the Scenario tab's editor of one row (VhilScenario.vue; step 10 of
docs/architecture/editor-workspace.md). A frame is edited as its message's
decoded fields from the firmware's .def contract: a value table as a select,
a bit as false/true, a scalar as a number with its unit, range and step,
with the hex in sync (the shell's decode.js, one copy); with no contract,
raw hex. An expect's signal is built from its source (a frame's field, a
frame, a symbol, a pin), owner and item; its value is a select of the
field's labels where it has a value table. Every edit is the row's item,
edited in place, then checked on the server (scenarios.js edited).
-->

<template>
    <form class="vhil-scen-editor" :aria-label="`Edit ${row.action} ${row.key}`" @submit.prevent>
        <h3 class="vhil-form-title">
            {{ row.action }} <span class="muted mono">{{ row.key }}</span>
        </h3>
        <ul v-if="messages.length" class="vhil-scen-msgs">
            <li v-for="m in messages" :key="m">{{ m }}</li>
        </ul>
        <p v-if="result" class="vhil-scen-msg" :class="result.passed ? '--ok' : '--error'">
            {{ result.passed ? '✓ passed' : '✕ failed' }}: {{ result.detail }}
        </p>

        <div class="vhil-scen-grid">
            <label v-if="row.t !== null">t (ms)
                <input
                    v-model.number="it.at_ms" type="number" min="0" step="1"
                    class="vhil-input mono" @input="edited"
                />
            </label>
            <label v-if="row.action !== 'watch'">Name
                <input
                    :value="it.name || ''" class="vhil-input mono" placeholder="optional"
                    @input="(ev) => setOpt('name', ev.target.value.trim())"
                />
            </label>

            <template v-if="isFrame">
                <label>Bus
                    <select v-model="it.bus" class="vhil-input" @change="edited">
                        <option v-for="b in buses" :key="b" :value="b">{{ b }}</option>
                    </select>
                </label>
                <label>Message
                    <select class="vhil-input" :value="msgKey" @change="pickMessage">
                        <option value="">raw id…</option>
                        <option v-for="m in messagesOnBus" :key="m.id" :value="String(m.id)">
                            {{ hexId(m.id, m.ext) }} {{ m.name }}
                        </option>
                    </select>
                </label>
                <label>Id (hex)
                    <input
                        :value="hexId(it.id, it.ext)" class="vhil-input mono"
                        @change="(ev) => setId(ev.target.value)"
                    />
                </label>
                <label class="vhil-scen-check">
                    <input v-model="it.ext" type="checkbox" @change="edited" /> extended id
                </label>
                <template v-if="row.action === 'periodic'">
                    <label>Every (ms)
                        <input
                            v-model.number="it.period_ms" type="number" min="0.5" step="1"
                            class="vhil-input mono" @input="edited"
                        />
                    </label>
                    <label>Until (ms)
                        <input
                            :value="it.until_ms ?? ''" type="number" min="0" step="1"
                            class="vhil-input mono" placeholder="the end"
                            @input="(ev) => setOpt('until_ms', ev.target.value, true)"
                        />
                    </label>
                </template>
                <label class="--wide">Data (hex)
                    <input
                        :value="spaced(it.data)" class="vhil-input mono"
                        :aria-invalid="badHex ? 'true' : 'false'"
                        @change="(ev) => setData(ev.target.value)"
                    />
                </label>
                <fieldset v-if="msg" class="vhil-scen-fields --wide">
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
                            v-else-if="f.length === 1" class="vhil-input mono"
                            :value="String(f.raw ?? 0)"
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
            </template>

            <label v-if="row.action === 'stop'">Periodic
                <select v-model="it.periodic" class="vhil-input mono" @change="edited">
                    <option v-for="p in periodics" :key="p" :value="p">{{ p }}</option>
                </select>
            </label>

            <template v-if="isPinRow">
                <label v-if="row.action === 'watch'">Watch
                    <select v-model="it.kind" class="vhil-input" @change="watchKind">
                        <option value="symbol">symbol (sampled)</option>
                        <option value="pin">pin (edges)</option>
                    </select>
                </label>
                <label>Board
                    <select v-model="it.board" class="vhil-input" @change="edited">
                        <option v-for="b in boards" :key="b" :value="b">{{ b }}</option>
                    </select>
                </label>
                <label v-if="row.action !== 'watch' || it.kind === 'pin'">Pin
                    <input
                        v-model.trim="it.pin" class="vhil-input mono" placeholder="PF9"
                        @input="edited"
                    />
                </label>
                <label v-if="row.action === 'gpio'">Level
                    <select v-model="it.level" class="vhil-input mono" @change="edited">
                        <option :value="true">HIGH</option>
                        <option :value="false">LOW</option>
                    </select>
                </label>
                <label v-if="row.action === 'analog'">Volts (0..3.6)
                    <input
                        v-model.number="it.volts" type="number" min="0" max="3.6" step="0.01"
                        class="vhil-input mono" @input="edited"
                    />
                </label>
                <template v-if="row.action === 'watch' && it.kind === 'symbol'">
                    <label>Symbol
                        <input
                            v-model.trim="it.name" class="vhil-input mono"
                            placeholder="g_state_telemetry" @input="edited"
                        />
                    </label>
                    <label>Bytes
                        <select v-model.number="it.size" class="vhil-input mono" @change="edited">
                            <option :value="1">1</option>
                            <option :value="2">2</option>
                            <option :value="4">4</option>
                        </select>
                    </label>
                    <label>Every (ms)
                        <input
                            v-model.number="it.period_ms" type="number" min="1" step="1"
                            class="vhil-input mono" @input="edited"
                        />
                    </label>
                </template>
            </template>

            <template v-if="row.action === 'expect'">
                <label>Check
                    <select v-model="it.check" class="vhil-input" @change="checkKind">
                        <option v-for="c in CHECKS" :key="c" :value="c">{{ c }}</option>
                    </select>
                </label>
                <label>Until (ms)
                    <input
                        :value="it.until_ms ?? ''" type="number" min="0" step="1"
                        class="vhil-input mono" placeholder="the end"
                        @input="(ev) => setOpt('until_ms', ev.target.value, true)"
                    />
                </label>
                <label>Source
                    <select
                        class="vhil-input" :value="sig.kind"
                        @change="(ev) => setSig({ kind: ev.target.value })"
                    >
                        <option value="frame">frame{{ valueCheck ? ' field' : '' }}</option>
                        <option v-if="valueCheck" value="symbol">symbol</option>
                        <option v-if="valueCheck" value="pin">pin</option>
                    </select>
                </label>
                <label>{{ sig.kind === 'frame' ? 'Bus' : 'Board' }}
                    <select
                        class="vhil-input" :value="sig.owner"
                        @change="(ev) => setSig({ owner: ev.target.value })"
                    >
                        <option v-for="o in owners" :key="o" :value="o">{{ o }}</option>
                    </select>
                </label>
                <label v-if="sig.kind === 'frame'">Message
                    <input
                        :value="sig.item" class="vhil-input mono" :list="listId"
                        placeholder="AMS_status or 0x4A0"
                        @change="(ev) => setSig({ item: ev.target.value.trim(), field: '' })"
                    />
                    <datalist :id="listId">
                        <option v-for="m in sigMessages" :key="m.id" :value="m.name">
                            {{ hexId(m.id, m.ext) }}
                        </option>
                    </datalist>
                </label>
                <label v-if="sig.kind === 'frame' && valueCheck">Field
                    <select
                        v-if="sigMsg" class="vhil-input mono" :value="sig.field"
                        @change="(ev) => setSig({ field: ev.target.value })"
                    >
                        <option v-for="f in sigMsg.fields" :key="f.name" :value="f.name">
                            {{ f.name }}
                        </option>
                    </select>
                    <input
                        v-else :value="sig.field" class="vhil-input mono"
                        @change="(ev) => setSig({ field: ev.target.value.trim() })"
                    />
                </label>
                <label v-if="sig.kind !== 'frame'">{{ sig.kind === 'pin' ? 'Pin' : 'Symbol' }}
                    <input
                        :value="sig.item" class="vhil-input mono"
                        :placeholder="sig.kind === 'pin' ? 'PB5' : 'g_state_telemetry'"
                        @change="(ev) => setSig({ item: ev.target.value.trim() })"
                    />
                </label>
                <template v-if="valueCheck">
                    <label>Op
                        <select v-model="it.op" class="vhil-input mono" @change="edited">
                            <option v-for="o in OPS" :key="o" :value="o">{{ o }}</option>
                        </select>
                    </label>
                    <label>
                        <span>
                            Value<span class="muted">{{ sigField ? unitOf(sigField) : '' }}</span>
                        </span>
                        <select
                            v-if="sigField?.values" class="vhil-input mono"
                            :value="String(it.value)"
                            @change="(ev) => setValue(ev.target.value, true)"
                        >
                            <option
                                v-for="(label, raw) in sigField.values" :key="raw" :value="label"
                            >{{ label }} ({{ raw }})</option>
                            <option v-if="!isLabel" :value="String(it.value)">
                                {{ it.value }}
                            </option>
                        </select>
                        <select
                            v-else-if="sig.kind === 'pin'" class="vhil-input mono"
                            :value="String(it.value)"
                            @change="(ev) => setValue(ev.target.value, false)"
                        >
                            <option value="1">HIGH (1)</option>
                            <option value="0">LOW (0)</option>
                        </select>
                        <input
                            v-else type="number" step="any" class="vhil-input mono"
                            :value="it.value" :min="sigRange?.[0]" :max="sigRange?.[1]"
                            @change="(ev) => setValue(ev.target.value, false)"
                        />
                    </label>
                </template>
                <template v-else-if="it.check === 'period'">
                    <label>Min gap (ms)
                        <input
                            :value="it.min_ms ?? ''" type="number" min="0" step="any"
                            class="vhil-input mono"
                            @input="(ev) => setOpt('min_ms', ev.target.value, true)"
                        />
                    </label>
                    <label>Max gap (ms)
                        <input
                            :value="it.max_ms ?? ''" type="number" min="0" step="any"
                            class="vhil-input mono"
                            @input="(ev) => setOpt('max_ms', ev.target.value, true)"
                        />
                    </label>
                </template>
                <template v-else>
                    <label>Min frames
                        <input
                            :value="it.min ?? ''" type="number" min="0" step="1"
                            class="vhil-input mono"
                            @input="(ev) => setOpt('min', ev.target.value, true)"
                        />
                    </label>
                    <label>Max frames
                        <input
                            :value="it.max ?? ''" type="number" min="0" step="1"
                            class="vhil-input mono"
                            @input="(ev) => setOpt('max', ev.target.value, true)"
                        />
                    </label>
                </template>
                <p class="vhil-note --wide mono">{{ it.signal }}</p>
            </template>
        </div>
    </form>
</template>

<script>
import { computed, defineComponent, ref } from 'vue';
// eslint-disable-next-line import/no-unresolved, import/extensions -- copied by the build
import * as dec from './shell/decode.js';
import {
    CHECKS, OPS, VALUE_CHECKS, findMessage, messageOf, parseSignal, signalText,
} from './scenario.js';
import { edited, scen } from './scenarios.js';

const PLAIN_UNITS = ['', 'enum', 'bool', 'bit'];

export default defineComponent({
    props: {
        row: { type: Object, required: true },
        buses: { type: Array, default: () => [] },
        boards: { type: Array, default: () => [] },
        periodics: { type: Array, default: () => [] },
        messages: { type: Array, default: () => [] },
        result: { type: Object, default: null },
    },
    setup(props) {
        // The row's item, edited in place (the scenario's own object).
        const it = computed(() => props.row.item);
        const setOpt = (key, value, number = false) => {
            const item = it.value;
            if (value === '' || value === null || value === undefined) delete item[key];
            else item[key] = number ? Number(value) : value;
            edited();
        };
        const unitOf = (f) => (PLAIN_UNITS.includes(f.unit) ? '' : ` (${f.unit})`);

        // -- frames ---------------------------------------------------------
        const isFrame = computed(() => ['send', 'periodic'].includes(props.row.action));
        const isPinRow = computed(() => ['gpio', 'analog', 'watch'].includes(props.row.action));
        const hexId = (id, ext) => dec.hexId(Number(id) || 0, ext);
        const spaced = (hex) => (hex || '').match(/../g)?.join(' ') ?? '';
        const badHex = ref(false);
        const messagesOnBus = computed(() => {
            scen.version; // eslint-disable-line no-unused-expressions
            return Object.values(scen.contract?.buses?.[it.value.bus] || {});
        });
        const msg = computed(() => {
            scen.version; // eslint-disable-line no-unused-expressions
            const m = messageOf(scen.contract, it.value.bus, it.value.id);
            return m && !!m.ext === !!it.value.ext ? m : null;
        });
        const msgKey = computed(() => (msg.value ? String(msg.value.id) : ''));
        const fields = computed(() => {
            scen.version; // eslint-disable-line no-unused-expressions
            if (!msg.value) return [];
            const bytes = dec.hexToBytes(it.value.data || '');
            return msg.value.fields.map((f) => {
                const raw = dec.rawValue(bytes, f);
                const [min, max] = dec.fieldRange(f);
                return {
                    ...f, raw, value: dec.physical(raw, f), min, max, def: f,
                };
            });
        });
        const pad = (bytes, n) => {
            if (bytes.length >= n) return bytes;
            const out = new Uint8Array(n);
            out.set(bytes);
            return out;
        };
        const setField = (f, value, isRaw) => {
            const raw = isRaw ? value : dec.rawFromPhysical(value, f.def);
            const bytes = pad(dec.hexToBytes(it.value.data || ''), msg.value.dlc);
            it.value.data = dec.bytesToHex(dec.encodeField(bytes, f.def, raw));
            edited();
        };
        const setData = (text) => {
            const hex = text.replace(/\s+/g, '').toLowerCase();
            badHex.value = !/^([0-9a-f]{2})*$/.test(hex) || hex.length > 128;
            if (badHex.value) return;
            it.value.data = hex;
            edited();
        };
        const setId = (text) => {
            const id = parseInt(String(text).replace(/^0x/i, ''), 16);
            if (Number.isNaN(id)) return;
            it.value.id = id;
            if (id > 0x7ff) it.value.ext = true;
            edited();
        };
        const pickMessage = (ev) => {
            const m = messagesOnBus.value.find((x) => String(x.id) === ev.target.value);
            if (!m) return;
            Object.assign(it.value, {
                id: m.id,
                ext: !!m.ext,
                data: dec.bytesToHex(pad(dec.hexToBytes(it.value.data || ''), m.dlc)),
            });
            edited();
        };
        const watchKind = () => {
            const item = it.value;
            if (item.kind === 'pin') {
                ['name', 'size', 'period_ms'].forEach((k) => delete item[k]);
                item.pin = item.pin || '';
            } else {
                delete item.pin;
                Object.assign(item, { name: '', size: 1, period_ms: 10 });
            }
            edited();
        };

        // -- expects --------------------------------------------------------
        const valueCheck = computed(() => VALUE_CHECKS.includes(it.value.check));
        const sig = computed(() => parseSignal(it.value.signal) || {
            kind: 'frame', owner: props.buses[0] || '', item: '', field: '',
        });
        const owners = computed(() => (sig.value.kind === 'frame' ? props.buses : props.boards));
        const listId = computed(() => `vhil-scen-msgs-${props.row.key.replace(/\W/g, '')}`);
        const sigMessages = computed(() => {
            scen.version; // eslint-disable-line no-unused-expressions
            return Object.values(scen.contract?.buses?.[sig.value.owner] || {});
        });
        const sigMsg = computed(() => (sig.value.kind === 'frame'
            ? findMessage(scen.contract, sig.value.owner, sig.value.item) : null));
        const sigField = computed(() => sigMsg.value?.fields
            .find((f) => f.name === sig.value.field) || null);
        const sigRange = computed(() => (sigField.value ? dec.fieldRange(sigField.value) : null));
        const isLabel = computed(() => Object.values(sigField.value?.values || {})
            .includes(String(it.value.value)));
        const setSig = (patch) => {
            const next = { ...sig.value, ...patch };
            if (patch.kind && patch.kind !== sig.value.kind) {
                next.owner = (patch.kind === 'frame' ? props.buses : props.boards)[0] || '';
                next.item = '';
                next.field = '';
            }
            if (next.kind !== 'frame' || !valueCheck.value) next.field = '';
            if (next.kind === 'frame' && valueCheck.value && !next.field) {
                const m = findMessage(scen.contract, next.owner, next.item);
                next.field = m?.fields[0]?.name || '';
            }
            it.value.signal = signalText(next);
            edited();
        };
        const setValue = (text, label) => {
            if (label) it.value.value = text;
            else it.value.value = text === '' ? null : Number(text);
            edited();
        };
        const checkKind = () => {
            const item = it.value;
            ['op', 'value', 'min_ms', 'max_ms', 'min', 'max'].forEach((k) => delete item[k]);
            if (VALUE_CHECKS.includes(item.check)) {
                Object.assign(item, { op: '==', value: 1 });
            } else {
                const s = parseSignal(item.signal);
                item.signal = s?.kind === 'frame'
                    ? signalText({ ...s, field: '' }) : `frame:${props.buses[0] || 'can0'}.0x100`;
                if (item.check === 'period') item.max_ms = 100;
                else item.max = 0;
            }
            edited();
        };

        return {
            CHECKS,
            OPS,
            it,
            edited,
            setOpt,
            unitOf,
            isFrame,
            isPinRow,
            hexId,
            spaced,
            badHex,
            messagesOnBus,
            msg,
            msgKey,
            fields,
            setField,
            setData,
            setId,
            pickMessage,
            watchKind,
            valueCheck,
            sig,
            owners,
            listId,
            sigMessages,
            sigMsg,
            sigField,
            sigRange,
            isLabel,
            setSig,
            setValue,
            checkKind,
        };
    },
});
</script>
