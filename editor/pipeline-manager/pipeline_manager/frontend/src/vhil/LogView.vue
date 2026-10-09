<!--
vHIL (CHANGELOG-VHIL.md): a plain, virtualised log in place of the hterm
terminal, which wrote inline styles (a `style-src 'self'` CSP refuses them)
and shipped a 500 kB terminal emulator for what is a read-only log here.

Only the rows in view are in the DOM (fixed 18 px rows over a spacer), so a
long log costs what fits on screen. It follows the end while scrolled to the
bottom and stays put once scrolled up. Lines are plain text: terminal
escape sequences (colours, cursor moves) are dropped. A writable terminal
(terminal_add with readonly false) gets an input line; Enter sends it.

An entry is a string (the editor's own message) or a run's log record as
runlog.js logEntry makes it ({ text, source, level }): a warning or an error
row is coloured, and `hidden` drops the entries of those sources.
-->

<template>
    <div class="vhil-log">
        <div
            ref="viewport"
            class="vhil-log-viewport"
            tabindex="0"
            role="log"
            aria-live="off"
            :aria-label="label"
            @scroll="onScroll"
        >
            <div class="vhil-log-spacer" :style="{ height: `${total * ROW}px` }">
                <div class="vhil-log-rows" :style="{ transform: `translateY(${first * ROW}px)` }">
                    <div
                        v-for="(line, i) in visible"
                        :key="first + i"
                        class="vhil-log-row"
                        :class="line.level !== 'info' && `--${line.level}`"
                    >{{ line.text }}</div>
                </div>
            </div>
        </div>
        <form v-if="!readonly" class="vhil-log-input" @submit.prevent="send">
            <label>
                <span class="vhil-visually-hidden">Input to {{ label }}</span>
                <input v-model="draft" type="text" autocomplete="off" spellcheck="false" />
            </label>
        </form>
    </div>
</template>

<script>
import {
    defineComponent, ref, computed, watch, nextTick, onMounted, onBeforeUnmount,
} from 'vue';
import { levelOf, sourceOf, textOf } from './runlog.js';

const ROW = 18;          // px: the row height in the stylesheet below
const OVERSCAN = 10;     // rows rendered beyond each edge of the view
// eslint-disable-next-line no-control-regex
const ESCAPES = /\u001b(\[[0-9;:?<=>]*[ -/]*[@-~]|\][^\u0007\u001b]*(\u0007|\u001b\\)|[()][0-9A-Za-z]|[@-_])/g;

/** Plain lines of a log entry: escape sequences and carriage returns dropped. */
export function toLines(entry) {
    return textOf(entry).replace(ESCAPES, '').replace(/\r\n?/g, '\n').split('\n');
}

export default defineComponent({
    props: {
        // The entries, in order; each may hold several lines.
        entries: { type: Array, default: () => [] },
        readonly: { type: Boolean, default: true },
        label: { type: String, default: 'Log' },
        // At most this many lines are kept; the oldest go first.
        maxLines: { type: Number, default: 20000 },
        // Sources whose entries are not shown (runlog.js sourceOf).
        hidden: { type: Array, default: () => [] },
    },
    emits: ['input'],
    setup(props, { emit }) {
        const viewport = ref(null);
        const scrollTop = ref(0);
        const height = ref(0);
        const draft = ref('');
        // The lines, kept outside Vue's reactivity (it would proxy every
        // string); `total` is the reactive count the view follows.
        let lines = [];
        let consumed = 0;   // entries already split into lines
        const total = ref(0);
        let follow = true;

        const rebuild = () => {
            const { entries } = props;
            const hidden = new Set(props.hidden);
            if (entries.length < consumed) { lines = []; consumed = 0; }   // cleared
            for (; consumed < entries.length; consumed += 1) {
                const entry = entries[consumed];
                if (!hidden.has(sourceOf(entry))) {
                    const level = levelOf(entry);
                    toLines(entry).forEach((text) => lines.push({ text, level }));
                }
            }
            if (lines.length > props.maxLines) lines = lines.slice(lines.length - props.maxLines);
            total.value = lines.length;
        };

        const first = computed(() => Math.max(0, Math.floor(scrollTop.value / ROW) - OVERSCAN));
        const visible = computed(() => {
            // eslint-disable-next-line no-unused-expressions
            total.value;   // a dependency: new lines re-render the view
            const count = Math.ceil(height.value / ROW) + 2 * OVERSCAN;
            return lines.slice(first.value, first.value + count);
        });

        const toEnd = () => {
            const el = viewport.value;
            if (el) el.scrollTop = el.scrollHeight;
        };
        const onScroll = () => {
            const el = viewport.value;
            scrollTop.value = el.scrollTop;
            follow = el.scrollTop + el.clientHeight >= el.scrollHeight - ROW;
        };

        watch(() => props.entries.length, async () => {
            rebuild();
            if (follow) { await nextTick(); toEnd(); }
        });
        watch(() => props.entries, () => { lines = []; consumed = 0; rebuild(); });
        watch(() => props.hidden.join('\n'), async () => {
            lines = [];
            consumed = 0;
            rebuild();
            if (follow) { await nextTick(); toEnd(); }
        });

        let observer;
        onMounted(() => {
            rebuild();
            observer = new ResizeObserver(() => {
                height.value = viewport.value?.clientHeight ?? 0;
                if (follow) toEnd();
            });
            observer.observe(viewport.value);
            nextTick(toEnd);
        });
        onBeforeUnmount(() => observer?.disconnect());

        const send = () => {
            emit('input', `${draft.value}\n`);
            draft.value = '';
        };

        return {
            ROW, viewport, total, first, visible, onScroll, draft, send,
        };
    },
});
</script>

<style lang="scss" scoped>
.vhil-log {
    display: flex;
    flex-direction: column;
    width: 100%;
    height: 100%;
    background-color: $gray-600;
    color: $white;
}

.vhil-log-viewport {
    flex: 1;
    min-height: 0;
    overflow: auto;
    padding: 0 8px;
}

.vhil-log-spacer {
    position: relative;
}

.vhil-log-row {
    height: 18px;     // ROW in the script
    line-height: 18px;
    font-family: $roboto-mono;
    font-size: 12px;
    white-space: pre;

    &.--warning {
        color: $gold;
    }

    &.--error {
        color: $red;
    }
}

.vhil-log-input input {
    width: 100%;
    box-sizing: border-box;
    font-family: $roboto-mono;
    font-size: 12px;
    color: $white;
    background-color: $gray-700;
    border: 1px solid $gray-500;
    padding: 2px 8px;
}

.vhil-visually-hidden {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
}
</style>
