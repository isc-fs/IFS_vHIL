<!--
vHIL: a board's debugger watch list (step 17; docs/debugger.md), shared by
the Debug tab and the State tab: one list per board, kept by the worker (the
`watches` debug op) and read back from the trace (its `breakpoints`
records), so every tab shows the same one. Each expression's value is the
one at the board's current stop (the stop record's `watches`).
-->

<template>
    <div class="vhil-watches">
        <p v-if="!list.length" class="muted">
            None. A watch reads a variable path (in.apps1_raw, this->state_) at every stop.
        </p>
        <ul v-else class="vhil-list vhil-watch-list">
            <li v-for="w in list" :key="w">
                <span class="mono vhil-watch-expr">{{ w }}</span>
                <output class="mono vhil-watch-value" :class="{ '--error': values[w]?.error }">
                    {{ valueText(w) }}
                </output>
                <button
                    type="button" class="vhil-btn --small" :disabled="!!cannot"
                    :aria-label="`Remove watch ${w}`" @click="remove(w)"
                >✕</button>
            </li>
        </ul>
        <form class="vhil-debug-add" @submit.prevent="add">
            <label class="vhil-visually-hidden" :for="inputId">
                Watch expression on {{ board }}
            </label>
            <input
                :id="inputId" v-model="expr" class="vhil-input mono"
                placeholder="variable, e.g. in.apps1_raw" :disabled="!!cannot || !board"
                autocomplete="off" spellcheck="false"
            />
            <button type="submit" class="vhil-btn --small" :disabled="!!cannot || !expr.trim()">
                Watch
            </button>
        </form>
    </div>
</template>

<script>
import { computed, defineComponent, ref } from 'vue';
import { replay } from './replay.js';
import { cannotDebug, setWatches, stopOf } from './debugui.js';

let ids = 0;

export default defineComponent({
    props: {
        board: { type: String, required: true },
    },
    setup(props) {
        ids += 1;
        const inputId = `vhil-watch-${ids}`;
        const cannot = computed(() => cannotDebug());
        const list = computed(() => {
            replay.debugVersion; // eslint-disable-line no-unused-expressions
            return replay.debug.boards.get(props.board)?.watches ?? [];
        });
        const values = computed(() => Object.fromEntries(
            (stopOf(props.board)?.watches || []).map((w) => [w.expr, w]),
        ));
        const valueText = (w) => {
            const v = values.value[w];
            if (!v) return stopOf(props.board) ? '…' : 'at the next stop';
            return v.error ? `✕ ${v.error}` : v.value;
        };
        const expr = ref('');
        const add = async () => {
            const e = expr.value.trim();
            if (!e || list.value.includes(e)) return;
            if (await setWatches(props.board, [...list.value, e])) expr.value = '';
        };
        const remove = (w) => setWatches(props.board, list.value.filter((x) => x !== w));
        return {
            inputId, cannot, list, values, valueText, expr, add, remove,
        };
    },
});
</script>
