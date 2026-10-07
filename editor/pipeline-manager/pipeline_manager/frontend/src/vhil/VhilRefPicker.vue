<!--
vHIL: a board's firmware ref picker (CHANGELOG-VHIL.md), opened from its chip
in the top bar or from the inspector; it replaces the shell's firmware aside.
Per board, the app's branch or tag and the bootloader's tag. The role sets
which firmware (shown, not picked); a ref equal to the catalogue's is written
as no ref, so the file keeps no firmware_ref.
-->

<template>
    <div
        v-if="node"
        ref="root"
        class="vhil-picker"
        role="dialog"
        :aria-label="`Firmware refs of ${name}`"
        :style="position"
        @keydown.esc.stop="close"
    >
        <div class="vhil-picker-head">
            <strong>{{ name }}</strong>
            <span class="muted">firmware refs</span>
            <button type="button" class="vhil-btn --small" @click="close">Close</button>
        </div>
        <p v-if="!parts.length" class="muted">This board names no firmware.</p>
        <section v-for="part in parts" :key="part.refProp" class="vhil-picker-part">
            <label :for="`vhil-ref-${part.refProp}`">
                <span class="vhil-picker-what">{{ part.what }}</span>
                <span class="mono">{{ part.fwId }}</span>
                <span v-if="part.fw" class="muted mono">{{ part.fw.repo }}</span>
            </label>
            <p v-if="!part.fw" class="vhil-warn">
                ▲ No {{ part.fwId }} firmware in the catalogue yet: this role can't run.
            </p>
            <template v-else>
                <input
                    v-model="filter[part.refProp]" type="search" class="vhil-input"
                    :aria-label="`Filter ${part.what} refs`" placeholder="Filter refs"
                />
                <select
                    :id="`vhil-ref-${part.refProp}`"
                    class="vhil-input mono"
                    size="7"
                    :value="part.ref"
                    @change="(ev) => pick(part, ev.target.value)"
                >
                    <option value="">{{ part.fw.ref }} (catalogue)</option>
                    <option v-if="part.ref && !known(part, part.ref)" :value="part.ref">
                        {{ part.ref }} ({{ refs[part.fwId] ? 'not found' : 'loading…' }})
                    </option>
                    <optgroup v-for="kind in part.kinds" :key="kind" :label="kind">
                        <option v-for="r in options(part, kind)" :key="r.name" :value="r.name">
                            {{ r.name }}{{ r.built ? '' : ' · not built' }}
                        </option>
                    </optgroup>
                </select>
                <p class="vhil-picker-info muted" aria-live="polite">
                    <template v-if="errors[part.fwId]">
                        refs of {{ part.fw.repo }}: {{ errors[part.fwId] }}
                    </template>
                    <template v-else-if="detail(part)">
                        <code :title="detail(part).sha">{{ detail(part).sha.slice(0, 8) }}</code>
                        <span v-if="!detail(part).built" class="vhil-warn">
                            ▲ not built yet: the first run builds it
                        </span>
                    </template>
                    <template v-else-if="!refs[part.fwId]">loading refs…</template>
                </p>
            </template>
        </section>
    </div>
</template>

<script>
import {
    computed, defineComponent, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch,
} from 'vue';
import {
    ws, boardFirmware, pickRef, refsOf,
} from './workspace.js';
import { nodeById, nodeName } from './graph.js';

export default defineComponent({
    props: {
        tick: { type: Number, default: 0 },
    },
    setup(props) {
        const root = ref(null);
        const refs = reactive({}); // fwId -> {branches, tags}
        const errors = reactive({});
        const filter = reactive({});
        const node = computed(() => {
            props.tick; // eslint-disable-line no-unused-expressions
            return ws.picker ? nodeById(ws.picker.nodeId) : null;
        });
        const name = computed(() => (node.value ? nodeName(node.value) : ''));
        const parts = computed(() => {
            props.tick; // eslint-disable-line no-unused-expressions
            return node.value ? boardFirmware(node.value) : [];
        });
        // Kept on screen: below its chip, within the window.
        const position = computed(() => {
            const width = 340;
            const x = Math.max(8, Math.min(ws.picker?.x ?? 8, window.innerWidth - width - 8));
            return { left: `${x}px`, top: `${ws.picker?.y ?? 48}px` };
        });

        let opener = null;
        const close = () => {
            ws.picker = null;
            if (opener?.isConnected) opener.focus();
            opener = null;
        };

        watch(parts, (list) => list.forEach((part) => {
            if (!part.fw || refs[part.fwId] || errors[part.fwId]) return;
            refsOf(part.fw.id).then((r) => { refs[part.fwId] = r; })
                .catch((e) => { errors[part.fwId] = e.message; });
        }), { immediate: true });

        const byName = (part) => new Map(part.kinds.flatMap((k) => refs[part.fwId]?.[k] ?? [])
            .map((r) => [r.name, r]));
        const known = (part, refName) => byName(part).has(refName);
        const detail = (part) => byName(part).get(part.ref || part.fw.ref) ?? null;
        const options = (part, kind) => {
            const f = (filter[part.refProp] || '').toLowerCase();
            return (refs[part.fwId]?.[kind] ?? [])
                .filter((r) => r.name !== part.fw.ref && r.name.toLowerCase().includes(f));
        };
        const pick = (part, value) => {
            if (node.value) pickRef(node.value, part.refProp, value);
        };

        const outside = (ev) => {
            if (!root.value || root.value.contains(ev.target)) return;
            // Its opener toggles it.
            if (ev.target.closest?.('.vhil-chip, .vhil-ref-button')) return;
            close();
        };
        watch(() => ws.picker?.nodeId, async (id) => {
            if (!id) return;
            opener = document.activeElement;
            await nextTick();
            root.value?.querySelector('select, input')?.focus();
        }, { immediate: true });
        onMounted(() => document.addEventListener('pointerdown', outside, true));
        onBeforeUnmount(() => document.removeEventListener('pointerdown', outside, true));

        return {
            root,
            node,
            name,
            parts,
            position,
            refs,
            errors,
            filter,
            close,
            known,
            detail,
            options,
            pick,
        };
    },
});
</script>
