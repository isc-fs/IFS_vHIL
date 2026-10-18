<!--
vHIL: the Scenario tab's editor of one row (VhilScenario.vue; step 10 of
docs/architecture/editor-workspace.md). A frame is edited as its message's
decoded fields from the firmware's .def contract: a value table as a select,
a bit as false/true, a scalar as a number with its unit, range and step,
with the hex in sync (the shell's decode.js, one copy); with no contract,
raw hex. An expect's signal is built from its source (a frame's field, a
frame, a symbol, a pin), owner and item; its value is a select of the
field's labels where it has a value table, or of the symbol's enum (the
contract's `labels`: DWARF or the state view's table). A `plant` row (M8)
picks one of the system's plants and edits its `set` as name/value pairs from
what the plant may set (the contract's `plants`, plant.js), a parameter with
its unit and status; its selects keep a pick (v-pick, CHANGELOG-VHIL.md 26).
Every edit is the row's item, edited in place, then checked on the server
(scenarios.js edited).
-->

<template>
    <form class="vhil-scen-editor" :aria-label="`Edit ${row.action} ${row.key}`" @submit.prevent>
        <h3 class="vhil-form-title">
            {{ row.action }} <span class="muted mono">{{ row.key }}</span>
        </h3>

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

            <template v-if="row.action === 'plant'">
                <label>Plant
                    <select
                        v-pick="it.plant" class="vhil-input mono"
                        @change="(ev) => setPlant(ev.target.value)"
                    >
                        <option v-for="p in plantChoices" :key="p" v-memo="[p]" :value="p">
                            {{ p }}
                        </option>
                    </select>
                </label>
                <fieldset class="vhil-scen-fields vhil-scen-plant --wide">
                    <legend>
                        sets · plant {{ it.plant }}{{ plantKind ? ` (${plantKind})` : '' }}
                    </legend>
                    <div v-for="(pair, i) in pairs" :key="pair.name" class="vhil-scen-pair">
                        <select
                            v-pick="pair.name" class="vhil-input mono"
                            :aria-label="`Setting ${i + 1}`"
                            @change="(ev) => renameSetting(pair.name, ev.target.value)"
                        >
                            <option
                                v-for="n in pair.choices" :key="n" v-memo="[n]" :value="n"
                            >{{ n }}</option>
                        </select>
                        <input
                            v-if="pair.entry.type === 'bool'" type="checkbox"
                            :checked="Boolean(pair.value)" :aria-label="`${pair.name} value`"
                            @change="(ev) => setSetting(pair.name, ev.target.checked)"
                        />
                        <input
                            v-else :type="pair.entry.type === 'number' ? 'number' : 'text'"
                            step="any" class="vhil-input mono" :value="pair.value ?? ''"
                            :placeholder="pair.entry.nullable ? 'none' : ''"
                            :aria-label="`${pair.name} value`"
                            @change="(ev) => setSettingText(pair, ev.target.value)"
                        />
                        <span class="muted">{{ pair.entry.unit || '' }}</span>
                        <span
                            v-if="pair.entry.status" class="vhil-plant-badge"
                            :class="`--${pair.entry.status}`"
                        >{{ pair.entry.status }}</span>
                        <button
                            type="button" class="vhil-btn --small"
                            :aria-label="`Remove ${pair.name}`" @click="removeSetting(pair.name)"
                        >✕</button>
                    </div>
                    <button
                        type="button" class="vhil-btn --small" :disabled="!unusedSettings.length"
                        @click="addSetting"
                    >+ Add setting</button>
                </fieldset>
            </template>

            <label v-if="row.action === 'stop'">Periodic
                <select v-model="it.periodic" class="vhil-input mono" @change="edited">
                    <option v-for="p in periodics" :key="p" :value="p">{{ p }}</option>
                </select>
            </label>

            <template v-if="isPinRow">
                <label v-if="row.action === 'watch'">Watch
                    <select :value="watchWhat" class="vhil-input" @change="watchKind">
                        <option value="symbol">symbol (sampled)</option>
                        <option value="pin">pin (edges)</option>
                    </select>
                </label>
                <label>Board
                    <select v-model="it.board" class="vhil-input" @change="edited">
                        <option v-for="b in boards" :key="b" :value="b">{{ b }}</option>
                    </select>
                </label>
                <label v-if="row.action !== 'watch' || watchWhat === 'pin'">Pin
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
                <template v-if="row.action === 'watch' && watchWhat === 'symbol'">
                    <label>Symbol
                        <input
                            v-model.trim="it[symbolKey]" class="vhil-input mono"
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
                            v-if="sigLabels" class="vhil-input mono"
                            :value="String(it.value)"
                            @change="(ev) => setValue(ev.target.value, true)"
                        >
                            <option
                                v-for="(label, raw) in sigLabels" :key="raw" :value="label"
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
        <p v-if="result" class="vhil-scen-msg" :class="result.passed ? '--ok' : '--error'">
            {{ result.passed ? '✓ passed' : '✕ failed' }}: {{ result.detail }}
        </p>
        <ul v-if="messages.length" class="vhil-scen-msgs" aria-live="polite">
            <li v-for="m in messages" :key="m">{{ m }}</li>
        </ul>
    </form>
</template>

<script>
import { computed, defineComponent, ref } from 'vue';
// eslint-disable-next-line import/no-unresolved, import/extensions -- copied by the build
import * as dec from './shell/decode.js';
import {
    CHECKS, OPS, VALUE_CHECKS, findMessage, looseSignal, messageOf, parseSignal, signalText,
} from './scenario.js';
import { edited, scen } from './scenarios.js';
import {
    firstValue, keyChoices, parseSetting, plantsOf, renameKey, settableOf,
} from './plant.js';
import { keep, vPick } from './stable.js';

const PLAIN_UNITS = ['', 'enum', 'bool', 'bit'];

export default defineComponent({
    directives: { pick: vPick },
    props: {
        row: { type: Object, required: true },
        buses: { type: Array, default: () => [] },
        boards: { type: Array, default: () => [] },
        plants: { type: Array, default: () => [] },
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
        // A `watch` row (a symbol or a pin, from power-on), or a mid-run
        // watch op among the stimuli (kind watch, with a symbol or a pin).
        const isWatchOp = computed(() => props.row.list === 'stimuli');
        const watchWhat = computed(() => {
            if (!isWatchOp.value) return it.value.kind;
            return it.value.pin !== undefined ? 'pin' : 'symbol';
        });
        const symbolKey = computed(() => (isWatchOp.value ? 'symbol' : 'name'));
        const watchKind = (ev) => {
            const item = it.value;
            const what = ev.target.value;
            if (!isWatchOp.value) item.kind = what;
            if (what === 'pin') {
                ['name', 'symbol', 'size', 'period_ms'].forEach((k) => delete item[k]);
                item.pin = item.pin || '';
            } else {
                delete item.pin;
                Object.assign(item, { [symbolKey.value]: '', size: 1, period_ms: 10 });
            }
            edited();
        };

        // -- plant rows (M8) -------------------------------------------------
        // The plant's settings (the contract's `plants`), and the row's set
        // as pairs, each with the names its select offers.
        const plantChoices = computed((old) => keep(
            old, keyChoices(props.plants, {}, it.value.plant),
        ));
        const plantKind = computed(() => {
            scen.version; // eslint-disable-line no-unused-expressions
            return plantsOf(scen.contract).find((p) => p.name === it.value.plant)?.plant || '';
        });
        const settings = computed((old) => {
            scen.version; // eslint-disable-line no-unused-expressions
            return keep(old, settableOf(scen.contract, it.value.plant));
        });
        const settingNames = computed(() => settings.value.map((e) => e.name));
        const TEXT = { type: 'text', nullable: false };
        const pairs = computed((old) => {
            scen.version; // eslint-disable-line no-unused-expressions
            const set = it.value.set || {};
            return keep(old, Object.entries(set).map(([name, value]) => ({
                name,
                value,
                entry: settings.value.find((e) => e.name === name) || TEXT,
                choices: keyChoices(settingNames.value, set, name),
            })));
        });
        const unusedSettings = computed(() => {
            scen.version; // eslint-disable-line no-unused-expressions
            return settings.value.filter((e) => !Object.hasOwn(it.value.set || {}, e.name));
        });
        const setPlant = (plant) => {
            it.value.plant = plant;
            // What one plant may set, another may not: start its set afresh.
            const [first] = settableOf(scen.contract, plant);
            it.value.set = first ? { [first.name]: firstValue(first) } : {};
            edited();
        };
        const setSetting = (name, value) => {
            it.value.set = { ...(it.value.set || {}), [name]: value };
            edited();
        };
        const setSettingText = (pair, text) => {
            const v = parseSetting(pair.entry, text);
            if (v !== undefined) setSetting(pair.name, v);
        };
        const renameSetting = (from, to) => {
            const e = settings.value.find((x) => x.name === to);
            it.value.set = renameKey(it.value.set, from, to, e ? firstValue(e) : null);
            edited();
        };
        const removeSetting = (name) => {
            const rest = { ...(it.value.set || {}) };
            delete rest[name];
            it.value.set = rest;
            edited();
        };
        const addSetting = () => {
            const [e] = unusedSettings.value;
            if (e) setSetting(e.name, firstValue(e));
        };

        // -- expects --------------------------------------------------------
        const valueCheck = computed(() => VALUE_CHECKS.includes(it.value.check));
        // Loosely: a signal being built ("pin:ams.") keeps its source.
        const sig = computed(() => looseSignal(it.value.signal) || {
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
        // The value's labels: a field's value table (CAN_VAL), or a symbol's
        // enum (the contract's `labels`: its DWARF enum or its state view's
        // table, vhil/stateview.py), the field's own over a view's.
        const sigLabels = computed(() => {
            scen.version; // eslint-disable-line no-unused-expressions
            if (sig.value.kind === 'pin') return null;
            const out = {
                ...(scen.contract?.labels?.[signalText(sig.value)] || {}),
                ...(sigField.value?.values || {}),
            };
            return Object.keys(out).length ? out : null;
        });
        const isLabel = computed(() => Object.values(sigLabels.value || {})
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
            watchWhat,
            symbolKey,
            valueCheck,
            sig,
            owners,
            listId,
            sigMessages,
            sigMsg,
            sigField,
            sigRange,
            sigLabels,
            isLabel,
            setSig,
            setValue,
            checkKind,
            plantChoices,
            plantKind,
            pairs,
            unusedSettings,
            setPlant,
            setSetting,
            setSettingText,
            renameSetting,
            removeSetting,
            addSetting,
        };
    },
});
</script>
