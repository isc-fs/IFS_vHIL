<!--
vHIL: a board's role, firmware and bootloader dropdowns, on its node
(custom/CustomNode.vue; CHANGELOG-VHIL.md). They are the only place these are
edited: the inspector shows them read-only. The role sets the board's node
ID, flash bus, firmware and pin labels (the backend relabels the node:
vhil/editor.py relabel(), which also puts the app back on the catalogue's
ref); the firmware it runs is shown, not picked. The app lists its repo's
active branches, newest first ("show all" lists every branch and the tags),
the bootloader its tags; what they list is refs.js. A ref equal to the
catalogue's is the empty value, so the system file keeps no ref.

Native selects: keyboard and screen readers work as anywhere; `no-drag`
keeps a press on them from dragging the node, and keys stay out of the
canvas's hotkeys. No style attribute (the editor's CSP is style-src 'self').
-->

<template>
    <div
        class="vhil-board-controls"
        no-drag="true"
        role="group"
        :aria-label="`${name}: role and firmware`"
        @keydown.stop
        @dblclick.stop
    >
        <label class="vhil-board-row --role">
            <span class="vhil-board-key">Role</span>
            <select
                class="vhil-board-select"
                :value="role"
                :disabled="locked || !roles.length"
                :title="ROLE_NOTE"
                @change="(ev) => setRole(ev.target.value)"
            >
                <option v-for="r in roles" :key="r" :value="r" :selected="r === role">{{ r }}</option>
            </select>
        </label>
        <label v-if="boot" class="vhil-board-row --boot">
            <span class="vhil-board-key">Bootloader</span>
            <select
                class="vhil-board-select mono"
                :value="boot.ref"
                :disabled="locked || !boot.fw"
                :title="boot.fw ? `${boot.fwId} tag (${boot.fw.repo})` : ''"
                @change="(ev) => choose(boot, ev)"
            >
                <template v-if="boot.fw">
                    <template v-for="(g, i) in optionsOf(boot)" :key="i">
                        <optgroup v-if="g.label" :label="g.label">
                            <option
                                v-for="o in g.options" :key="o.value" :value="o.value"
                                :selected="o.value === boot.ref"
                            >
                                {{ o.label }}
                            </option>
                        </optgroup>
                        <template v-else>
                            <option
                                v-for="o in g.options" :key="o.value" :value="o.value"
                                :selected="o.value === boot.ref"
                            >
                                {{ o.label }}
                            </option>
                        </template>
                    </template>
                </template>
                <option v-else value="">{{ boot.fwId }}: not in the catalogue</option>
            </select>
        </label>
        <div v-if="app" class="vhil-board-row --app">
            <label class="vhil-board-key" :for="app.fw ? appId : null">Firmware</label>
            <span class="vhil-board-fw mono" :title="app.fw ? app.fw.repo : ''">
                {{ app.fwId }}<span v-if="app.fw" class="muted"> · {{ app.fw.repo }}</span>
            </span>
            <p v-if="!app.fw" class="vhil-board-warn" role="note">
                ▲ No {{ app.fwId }} firmware in the catalogue yet: this role can't run.
            </p>
            <template v-else>
                <select
                    :id="appId"
                    class="vhil-board-select --wide mono"
                    :value="app.ref"
                    :disabled="locked"
                    :aria-describedby="`${appId}-info`"
                    @change="(ev) => choose(app, ev)"
                >
                    <template v-for="(g, i) in optionsOf(app)" :key="i">
                        <optgroup v-if="g.label" :label="g.label">
                            <option
                                v-for="o in g.options" :key="o.value" :value="o.value"
                                :selected="o.value === app.ref"
                            >
                                {{ o.label }}
                            </option>
                        </optgroup>
                        <template v-else>
                            <option
                                v-for="o in g.options" :key="o.value" :value="o.value"
                                :selected="o.value === app.ref"
                            >
                                {{ o.label }}
                            </option>
                        </template>
                    </template>
                </select>
                <p :id="`${appId}-info`" class="vhil-board-info mono" aria-live="polite">
                    <template v-if="errors[app.fwId]">refs: {{ errors[app.fwId] }}</template>
                    <template v-else>
                        {{ detail(app) }}
                        <span v-if="unbuilt(app)" class="vhil-board-warn">▲ not built: the first run builds it</span>
                        <span v-if="note(app)" class="muted">{{ note(app) }}</span>
                    </template>
                </p>
            </template>
        </div>
    </div>
</template>

<script>
import {
    computed, defineComponent, reactive, ref, watch,
} from 'vue';
import {
    ws, boardFirmware, isLive, pickRef, refreshDirty, refsOf,
} from './workspace.js';
import {
    nodeName, prop, setProp, specNode,
} from './graph.js';
import {
    SHOW_ACTIVE, SHOW_ALL, detailText, listingNote, notBuilt, refOptions,
} from './refs.js';

const ROLE_NOTE = 'Sets its firmware, node ID, flash bus and pin labels.';

export default defineComponent({
    props: {
        node: { type: Object, required: true },
    },
    setup(props) {
        const name = computed(() => nodeName(props.node));
        const appId = computed(() => `vhil-fw-${props.node.id}`);
        const roles = computed(() => specNode(props.node.type)?.properties
            ?.find((p) => p.name === 'role')?.values ?? []);
        const role = computed(() => prop(props.node, 'role')?.value ?? '');
        const parts = computed(() => {
            ws.firmware; // eslint-disable-line no-unused-expressions
            return boardFirmware(props.node);
        });
        const app = computed(() => parts.value.find((p) => p.what === 'app') ?? null);
        const boot = computed(() => parts.value.find((p) => p.what === 'bootloader') ?? null);
        // The topology is locked in LIVE (graph.js setLocked).
        const locked = computed(() => isLive());

        // Listings per firmware: the active branches and tags, and every
        // branch once "show all" is picked (workspace.js caches each request).
        const refs = reactive({});
        const every = reactive({});
        const errors = reactive({});
        const showAll = ref(false);
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

        const lists = (part) => ({
            refs: refs[part.fwId], every: every[part.fwId], showAll: showAll.value,
        });
        const optionsOf = (part) => refOptions(part, lists(part));
        const detail = (part) => detailText(part, lists(part));
        const unbuilt = (part) => notBuilt(part, lists(part));
        const note = (part) => listingNote(part, lists(part));

        const choose = (part, ev) => {
            const { value } = ev.target;
            if (value === SHOW_ALL || value === SHOW_ACTIVE) {
                showAll.value = value === SHOW_ALL;
                ev.target.value = part.ref; // the pick stays what it was
                return;
            }
            pickRef(props.node, part.refProp, value);
        };
        // The node's property, as its control would set it: the node tells
        // the backend, which relabels it in the new role.
        const setRole = (value) => {
            setProp(props.node, 'role', value);
            refreshDirty();
        };

        return {
            name,
            appId,
            roles,
            role,
            app,
            boot,
            locked,
            errors,
            optionsOf,
            detail,
            unbuilt,
            note,
            choose,
            setRole,
            ROLE_NOTE,
        };
    },
});
</script>
