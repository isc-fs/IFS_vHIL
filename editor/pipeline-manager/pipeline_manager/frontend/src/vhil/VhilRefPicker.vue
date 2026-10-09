<!--
vHIL: a board's firmware ref picker (CHANGELOG-VHIL.md), opened from its chip
in the top bar or from the inspector; it replaces the shell's firmware aside.
Per board, the app's branch or tag and the bootloader's tag. The role sets
which firmware (shown, not picked); a ref equal to the catalogue's is written
as no ref, so the file keeps no firmware_ref. The app lists the repo's active
branches (GET /api/firmware/{id}/refs: default branch, dev/main, open PRs,
recent heads), newest first, with each head's age, author and PR; "Show all
branches and tags" lists every branch and the release tags.
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
                <label v-if="part.kinds.includes('branches')" class="vhil-picker-all muted">
                    <input v-model="showAll" type="checkbox" />
                    Show all branches and tags
                </label>
                <select
                    :id="`vhil-ref-${part.refProp}`"
                    class="vhil-input mono"
                    size="8"
                    :value="part.ref"
                    @change="(ev) => pick(part, ev.target.value)"
                >
                    <option value="">{{ label(part, part.fw.ref) }} (catalogue)</option>
                    <option v-if="part.ref && !listed(part, part.ref)" :value="part.ref">
                        {{ part.ref }} ({{ missing(part, part.ref) }})
                    </option>
                    <optgroup v-for="kind in shownKinds(part)" :key="kind" :label="groupLabel(part, kind)">
                        <option v-for="r in options(part, kind)" :key="r.name" :value="r.name">
                            {{ label(part, r.name) }}
                        </option>
                    </optgroup>
                </select>
                <p class="vhil-picker-info muted" aria-live="polite">
                    <template v-if="errors[part.fwId]">
                        refs of {{ part.fw.repo }}: {{ errors[part.fwId] }}
                    </template>
                    <template v-else-if="detail(part)">
                        <code :title="detail(part).sha">{{ detail(part).sha.slice(0, 8) }}</code>
                        <span v-if="detail(part).date" :title="detail(part).date">
                            {{ ageText(detail(part).date) }}
                        </span>
                        <span v-if="detail(part).author">· {{ detail(part).author }}</span>
                        <a
                            v-for="pr in detail(part).prs || []" :key="pr.number"
                            :href="pr.url" :title="pr.title" target="_blank" rel="noopener noreferrer"
                        >#{{ pr.number }}</a>
                        <span v-if="!detail(part).built" class="vhil-warn">
                            ▲ this commit is not built yet: the first run builds it
                        </span>
                    </template>
                    <template v-else-if="!listing(part)">loading refs…</template>
                </p>
                <p v-if="listing(part) && part.kinds.includes('branches')" class="vhil-picker-info muted">
                    <template v-if="listing(part).details !== 'github'">
                        {{ listing(part).note || 'branch dates and PRs unavailable' }}
                    </template>
                    <template v-else-if="!showAll">
                        Active: default branch, dev/main, open PRs, heads in the last
                        {{ listing(part).active_days }} days<template v-if="listing(part).hidden">
                            ({{ listing(part).hidden }} more hidden)</template>.
                    </template>
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
import { ageText } from './artifacts.js';
import { nodeById, nodeName } from './graph.js';

export default defineComponent({
    props: {
        tick: { type: Number, default: 0 },
    },
    setup(props) {
        const root = ref(null);
        const refs = reactive({}); // fwId -> the active listing
        const every = reactive({}); // fwId -> every branch (show all)
        const errors = reactive({});
        const filter = reactive({});
        const showAll = ref(false);
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
            const width = 380;
            const x = Math.max(8, Math.min(ws.picker?.x ?? 8, window.innerWidth - width - 8));
            return { left: `${x}px`, top: `${ws.picker?.y ?? 48}px` };
        });

        let opener = null;
        const close = () => {
            ws.picker = null;
            if (opener?.isConnected) opener.focus();
            opener = null;
        };

        const load = (list) => list.forEach((part) => {
            if (!part.fw || errors[part.fwId]) return;
            if (!refs[part.fwId]) {
                refsOf(part.fw.id).then((r) => { refs[part.fwId] = r; })
                    .catch((e) => { errors[part.fwId] = e.message; });
            }
            if (showAll.value && part.kinds.includes('branches') && !every[part.fwId]) {
                refsOf(part.fw.id, true).then((r) => { every[part.fwId] = r; })
                    .catch((e) => { errors[part.fwId] = e.message; });
            }
        });
        watch([parts, showAll], ([list]) => load(list), { immediate: true });

        // What the part lists: the app its active branches (every branch and
        // the tags with show all), the bootloader its tags.
        const listing = (part) => (showAll.value && part.kinds.includes('branches')
            ? every[part.fwId] : null) || refs[part.fwId] || null;
        const shownKinds = (part) => (part.kinds.includes('branches') && !showAll.value
            ? ['branches'] : part.kinds);
        const byName = (part) => new Map([refs[part.fwId], every[part.fwId]]
            .flatMap((l) => part.kinds.flatMap((k) => l?.[k] ?? []))
            .map((r) => [r.name, r]));
        const listed = (part, refName) => refName === part.fw.ref
            || shownKinds(part).some((k) => (listing(part)?.[k] ?? []).some((r) => r.name === refName));
        const missing = (part, refName) => {
            if (!listing(part)) return 'loading…';
            return byName(part).has(refName) ? 'not active' : 'not found';
        };
        const detail = (part) => byName(part).get(part.ref || part.fw.ref) ?? null;
        const label = (part, refName) => {
            const r = byName(part).get(refName);
            if (!r) return refName;
            const bits = [refName];
            if (r.date) bits.push(ageText(r.date));
            if (r.author) bits.push(r.author);
            (r.prs || []).forEach((pr) => bits.push(`#${pr.number}`));
            if (!r.built) bits.push('not built');
            return bits.join(' · ');
        };
        const groupLabel = (part, kind) => {
            if (kind !== 'branches') return kind;
            return showAll.value || listing(part)?.details !== 'github' ? 'branches' : 'active branches';
        };
        const options = (part, kind) => {
            const f = (filter[part.refProp] || '').toLowerCase();
            return (listing(part)?.[kind] ?? [])
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
            errors,
            filter,
            showAll,
            close,
            listing,
            shownKinds,
            listed,
            missing,
            detail,
            label,
            groupLabel,
            options,
            pick,
            ageText,
        };
    },
});
</script>
