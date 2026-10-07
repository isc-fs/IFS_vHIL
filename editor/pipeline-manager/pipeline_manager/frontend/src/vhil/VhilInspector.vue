<!--
vHIL: the inspector (resizable, 320 px by default; CHANGELOG-VHIL.md). In
DESIGN, the selected node's properties: a board's role, its firmware and the
refs picked for it, and a bus's or a device's own; in REPLAY and LIVE, a
board's state card above them (VhilStateCard.vue, step 13), and in LIVE its
inputs (VhilInputs.vue, step 15: pin switches, analog voltages); with nothing selected,
the open system and its Check, Commit and PR, which used to be the shell's
fixed aside. A board's properties live here, not on its node.
-->

<template>
    <aside class="vhil-inspector" aria-labelledby="vhil-inspector-title">
        <div
            class="vhil-resize --x"
            role="separator"
            aria-orientation="vertical"
            aria-label="Resize the inspector"
            :aria-valuenow="ws.layout.inspectorW"
            aria-valuemin="260"
            aria-valuemax="560"
            tabindex="0"
            @pointerdown="startResize"
            @keydown.left.prevent="resizeBy(16)"
            @keydown.right.prevent="resizeBy(-16)"
        />
        <h2 id="vhil-inspector-title" class="vhil-panel-title">
            {{ node ? nodeName(node) : 'Inspector' }}
            <span v-if="node" class="muted">{{ node.type }}</span>
        </h2>

        <div v-if="node" class="vhil-inspector-body">
            <span v-if="kind" class="vhil-kind">{{ kind }}</span>
            <!-- In REPLAY, the board's state at the scrubber's time (step 13) -->
            <VhilStateCard
                v-if="stateTrace && kind === 'board' && stateTrace.items.has(nodeName(node))"
                :trace="stateTrace" :board="nodeName(node)" :t="replay.t" :end="replay.end"
                :version="replay.version"
            />
            <VhilInputs v-if="live && kind === 'board'" :board="nodeName(node)" />
            <dl class="vhil-props">
                <div v-for="p in properties" :key="p.name" class="vhil-prop">
                    <dt>
                        <label :for="`vhil-prop-${p.name}`" :title="p.description">
                            {{ p.label }}
                        </label>
                    </dt>
                    <dd>
                        <!-- a ref: the picker, as the top bar's chip -->
                        <button
                            v-if="p.ref"
                            :id="`vhil-prop-${p.name}`"
                            type="button"
                            class="vhil-btn vhil-ref-button mono"
                            :disabled="!p.fw"
                            :title="refTitle(p)"
                            @click="(ev) => openPicker(ev)"
                        >{{ p.value || (p.fw ? `${p.fw.ref} (catalogue)` : 'none') }}</button>
                        <output
                            v-else-if="p.type === 'constant' || p.readonly"
                            :id="`vhil-prop-${p.name}`" class="mono"
                        >{{ p.value }}</output>
                        <select
                            v-else-if="p.type === 'select'"
                            :id="`vhil-prop-${p.name}`" class="vhil-input"
                            :value="p.value" @change="(ev) => set(p.name, ev.target.value)"
                        >
                            <option v-for="v in p.values" :key="v" :value="v">{{ v }}</option>
                        </select>
                        <input
                            v-else-if="p.type === 'bool'"
                            :id="`vhil-prop-${p.name}`" type="checkbox"
                            :checked="Boolean(p.value)"
                            @change="(ev) => set(p.name, ev.target.checked)"
                        />
                        <input
                            v-else-if="NUMERIC.includes(p.type)"
                            :id="`vhil-prop-${p.name}`" type="number" class="vhil-input mono"
                            :min="p.min" :max="p.max" :step="p.type === 'integer' ? 1 : 'any'"
                            :value="p.value" @change="(ev) => set(p.name, Number(ev.target.value))"
                        />
                        <input
                            v-else
                            :id="`vhil-prop-${p.name}`" class="vhil-input mono"
                            :value="p.value" @change="(ev) => set(p.name, ev.target.value)"
                        />
                        <p v-if="p.note" class="vhil-note">{{ p.note }}</p>
                    </dd>
                </div>
            </dl>
            <p v-if="!properties.length" class="muted">No properties.</p>
            <p v-if="nodeProblems.length" class="vhil-note">
                {{ nodeProblems.length }} problem(s) on this node: see Problems.
            </p>
        </div>

        <div v-else class="vhil-inspector-body">
            <template v-if="ws.id">
                <dl class="vhil-props">
                    <dt>System</dt><dd class="mono">{{ ws.id }}{{ ws.isNew ? ' (new)' : '' }}</dd>
                    <dt>Branch</dt><dd class="mono">{{ ws.branch || 'checked-out tree' }}</dd>
                    <dt>Commit</dt><dd class="mono">{{ (ws.ref || '').slice(0, 8) || '—' }}</dd>
                    <dt>Edits</dt><dd>{{ ws.dirty ? '● unsaved' : 'none' }}</dd>
                </dl>
                <p class="muted">Select a node on the canvas to see its properties.</p>
                <div class="vhil-actions">
                    <button type="button" class="vhil-btn" :disabled="ws.busy" @click="check">
                        Check
                    </button>
                    <button type="button" class="vhil-btn" @click="ws.dialog = 'commit'">
                        Commit…
                    </button>
                    <button type="button" class="vhil-btn" @click="ws.dialog = 'pr'">
                        Open PR
                    </button>
                </div>
                <p class="muted">
                    Edits live in the graph until committed to a branch; Run runs the
                    committed system.
                </p>
            </template>
            <p v-else class="muted">Open a system from Systems in the sidebar.</p>
        </div>
    </aside>
</template>

<script>
import { computed, defineComponent } from 'vue';
import VhilStateCard from './VhilStateCard.vue';
import VhilInputs from './VhilInputs.vue';
import { replay } from './replay.js';
import {
    ws, boardFirmware, check, isLive, pickRef, refreshDirty,
} from './workspace.js';
import {
    nodeById, nodeName, prop, setProp, specNode, vhilKind,
} from './graph.js';

const LABELS = {
    firmware_ref: 'app ref', bootloader_ref: 'bootloader ref', host_netdev: 'host netdev',
};
const NUMERIC = ['integer', 'number', 'slider'];
const ROLE_NOTE = 'Sets its firmware, node ID, flash bus and pin labels.';

export default defineComponent({
    components: { VhilStateCard, VhilInputs },
    props: {
        tick: { type: Number, default: 0 },
    },
    setup(props) {
        const stateTrace = computed(() => {
            replay.version; // eslint-disable-line no-unused-expressions
            return ws.mode === 'REPLAY' || isLive() ? replay.stateTrace : null;
        });
        const live = computed(() => isLive());
        const node = computed(() => {
            props.tick; // eslint-disable-line no-unused-expressions
            return ws.selectedId ? nodeById(ws.selectedId) ?? null : null;
        });
        const kind = computed(() => (node.value ? vhilKind(node.value.type) : null));

        // The node's properties as its type declares them (vhil/editor.py
        // specification()), with the values on the canvas; hidden ones (a
        // system's write_protect) stay hidden.
        const properties = computed(() => {
            props.tick; // eslint-disable-line no-unused-expressions
            const n = node.value;
            if (!n) return [];
            const fw = Object.fromEntries(boardFirmware(n).map((f) => [f.refProp, f]));
            return (specNode(n.type)?.properties ?? [])
                .filter((p) => !p.hidden && prop(n, p.name))
                .map((p) => {
                    const f = fw[p.name];
                    return {
                        ...p,
                        label: LABELS[p.name] ?? p.name.replace(/_/g, ' '),
                        value: prop(n, p.name).value,
                        ref: f ? f.what : null,
                        fw: f?.fw ?? null,
                        fwId: f?.fwId,
                        note: p.name === 'role' ? ROLE_NOTE : '',
                    };
                });
        });
        const nodeProblems = computed(() => ws.problems.filter((p) => p.nodeId === ws.selectedId));

        const set = (name, value) => {
            setProp(node.value, name, value);
            refreshDirty();
        };
        const openPicker = (ev) => {
            const r = ev.currentTarget.getBoundingClientRect();
            ws.picker = { nodeId: node.value.id, x: r.left - 40, y: r.bottom + 4 };
        };
        const refTitle = (p) => (p.fw ? `Pick a ${p.ref} ref` : `No ${p.fwId} firmware in the catalogue`);

        // Width: drag the left edge, or arrow keys on it.
        const clamp = (w) => Math.max(260, Math.min(560, Math.round(w)));
        const resizeBy = (dx) => { ws.layout.inspectorW = clamp(ws.layout.inspectorW + dx); };
        const startResize = (ev) => {
            const x0 = ev.clientX;
            const w0 = ws.layout.inspectorW;
            const move = (e) => { ws.layout.inspectorW = clamp(w0 + (x0 - e.clientX)); };
            const up = () => {
                document.removeEventListener('pointermove', move);
                document.removeEventListener('pointerup', up);
            };
            document.addEventListener('pointermove', move);
            document.addEventListener('pointerup', up);
        };

        return {
            ws,
            live,
            replay,
            stateTrace,
            node,
            kind,
            properties,
            nodeProblems,
            nodeName,
            set,
            openPicker,
            refTitle,
            NUMERIC,
            check,
            pickRef,
            resizeBy,
            startResize,
        };
    },
});
</script>
