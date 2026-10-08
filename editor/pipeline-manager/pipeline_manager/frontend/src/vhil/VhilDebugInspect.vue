<!--
vHIL: a held board in the inspector (step 17; docs/debugger.md): PAUSED at
a debugger stop, its call stack (a click picks the frame the Debug tab's
source and these locals show), the frame's arguments and locals, and the
registers, from the stop's trace record (the top frame's) or a `locals` op
(another frame's).
-->

<template>
    <section class="vhil-dbg-inspect" :aria-label="`${board} at its stop`">
        <h3 class="vhil-dbg-title">
            <span aria-hidden="true">■</span> stopped ·
            <span class="mono">{{ heldText({ board, reason: stop.reason, ...stop.frame }) }}</span>
        </h3>
        <h4>Call stack</h4>
        <ol class="vhil-dbg-frames" start="0">
            <li v-for="(f, i) in frames" :key="i">
                <button
                    type="button" class="vhil-dbg-frame mono"
                    :aria-pressed="dbg.frame === i" :title="f.file ? `${f.file}:${f.line}` : f.addr"
                    @click="pick(i)"
                >
                    <span class="vhil-dbg-func">{{ f.func }}</span>
                    <span class="muted">{{ whereText(f) }}</span>
                </button>
            </li>
        </ol>
        <h4>Locals <span v-if="dbg.frame" class="muted">· frame {{ dbg.frame }}</span></h4>
        <p v-if="!locals" class="muted">Reading…</p>
        <p v-else-if="!locals.length" class="muted">None.</p>
        <dl v-else class="vhil-dbg-vars">
            <div v-for="v in locals" :key="v.name">
                <dt class="mono" :title="v.type">
                    {{ v.name }}<span v-if="v.arg" class="muted"> (arg)</span>
                </dt>
                <dd class="mono">{{ shown(v) }}</dd>
            </div>
        </dl>
        <h4>Registers</h4>
        <dl class="vhil-dbg-regs">
            <div v-for="r in registers" :key="r.name">
                <dt class="mono">{{ r.name }}</dt>
                <dd class="mono num">{{ r.value }}</dd>
            </div>
        </dl>
        <p v-if="stop.errors" class="vhil-note">
            Not read at this stop: {{ Object.keys(stop.errors).join(', ') }}
        </p>
    </section>
</template>

<script>
import { computed, defineComponent } from 'vue';
import { heldText, whereText } from './debug.js';
import { dbg, pickFrame } from './debugui.js';

export default defineComponent({
    props: {
        board: { type: String, required: true },
        stop: { type: Object, required: true },
    },
    setup(props) {
        const frames = computed(() => props.stop.frames
            || (props.stop.frame ? [props.stop.frame] : []));
        const locals = computed(() => {
            if (!dbg.frame) return props.stop.locals || [];
            return dbg.frameLocals?.list ?? null;
        });
        const registers = computed(() => props.stop.registers || []);
        const pick = (i) => pickFrame(props.board, i);
        // A struct or array has no simple value: its type stands in.
        const shown = (v) => v.value ?? `(${v.type})`;
        return {
            dbg, frames, locals, registers, pick, shown, heldText, whereText,
        };
    },
});
</script>
