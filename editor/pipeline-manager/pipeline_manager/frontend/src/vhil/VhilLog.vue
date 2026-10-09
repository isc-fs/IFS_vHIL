<!--
The dock's Log tab (CHANGELOG-VHIL.md, live logs): Pipeline Manager's main
terminal (the editor's messages and the run's `log` records, runlog.js) in
the plain log view, with a chip per source to hide or show its lines
(hidden ones are remembered with the layout) and a link to the run's whole
Renode log. The chips are buttons, not a <select> (#249); their list keeps
its value while the sources stay the same (stable.js keep), so a streaming
log doesn't re-render them.
-->

<template>
    <div class="vhil-logtab">
        <div class="vhil-logtab-bar" role="toolbar" aria-label="Log sources">
            <span class="vhil-logtab-label muted">Sources</span>
            <button
                v-for="s in sources"
                :key="s"
                type="button"
                class="vhil-logtab-chip"
                :class="{ '--off': hiddenSet.has(s) }"
                :aria-pressed="!hiddenSet.has(s)"
                :title="hiddenSet.has(s) ? `Show ${s}'s lines` : `Hide ${s}'s lines`"
                @click="toggle(s)"
            >{{ s }}</button>
            <span class="vhil-logtab-spacer" />
            <a
                v-if="runId"
                class="vhil-logtab-link"
                :href="artifactUrl(runId, 'renode.log')"
                :download="`run-${runId}-renode.log`"
                title="The whole Renode log of the run (the Log shows its warnings, errors and notable events)"
            >renode.log of run {{ runId }}</a>
        </div>
        <LogView
            class="vhil-logtab-view"
            :entries="entries"
            :hidden="ws.layout.logHidden"
            label="Log"
        />
    </div>
</template>

<script>
import {
    computed, defineComponent, ref, watch,
} from 'vue';
import LogView from './LogView.vue';
import { terminalStore, MAIN_TERMINAL } from '../core/stores.js';
import { artifactUrl } from './artifacts.js';
import { replay } from './replay.js';
import { sourceOf, sourcesOf, toggled } from './runlog.js';
import { keep } from './stable.js';
import { ws } from './workspace.js';

export default defineComponent({
    components: { LogView },
    setup() {
        const entries = computed(() => terminalStore.logs[MAIN_TERMINAL] ?? []);
        // The sources seen, scanned as entries come (not the whole log on
        // every line); a clear starts over.
        const seen = ref([]);
        let scanned = 0;
        const scan = () => {
            const list = entries.value;
            if (list.length < scanned) { seen.value = []; scanned = 0; }
            const add = new Set();
            for (; scanned < list.length; scanned += 1) add.add(sourceOf(list[scanned]));
            const known = new Set(seen.value);
            if ([...add].some((s) => !known.has(s))) seen.value = sourcesOf([...known, ...add]);
        };
        watch(() => entries.value.length, scan, { immediate: true });
        watch(entries, () => { seen.value = []; scanned = 0; scan(); });
        // A hidden source stays a chip after a clear, so it can be shown again.
        const sources = computed((old) => keep(old, sourcesOf([...seen.value,
            ...(ws.layout.logHidden || [])])));
        const hiddenSet = computed(() => new Set(ws.layout.logHidden || []));
        const toggle = (s) => { ws.layout.logHidden = toggled(ws.layout.logHidden, s); };
        const runId = computed(() => (['REPLAY', 'LIVE', 'PAUSED'].includes(ws.mode)
            ? replay.id : ws.run?.id) || null);
        return {
            ws, entries, sources, hiddenSet, toggle, runId, artifactUrl,
        };
    },
});
</script>

<style lang="scss" scoped>
.vhil-logtab {
    display: flex;
    flex-direction: column;
    width: 100%;
    height: 100%;
}

.vhil-logtab-bar {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 4px;
    padding: 4px 8px;
    border-bottom: 1px solid $gray-500;
    font-size: $fs-small;
}

.vhil-logtab-chip {
    height: 20px;
    padding: 0 8px;
    font-family: $roboto-mono;
    font-size: 11px;
    color: $white;
    background: $gray-700;
    border: 1px solid $gray-200;
    border-radius: 10px;
    cursor: pointer;

    &.--off {
        color: $gray-300;
        border-style: dashed;
        text-decoration: line-through;
    }

    &:focus-visible {
        outline: 2px solid $focus;
    }
}

.vhil-logtab-spacer {
    flex: 1;
}

.vhil-logtab-link {
    color: $green;
}

.vhil-logtab-view {
    flex: 1;
    min-height: 0;
}
</style>
