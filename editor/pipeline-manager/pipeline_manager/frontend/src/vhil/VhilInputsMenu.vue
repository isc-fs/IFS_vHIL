<!--
vHIL: a board's inputs from its context menu in LIVE ("Inputs and analog…";
custom/CustomNode.vue): VhilInputs.vue in a small panel at the pointer, a
non-modal dialog that Escape or Close dismisses, focus moving into it.
Placed through the CSSOM (style-src 'self').
-->

<template>
    <div
        v-if="menu" ref="el" class="vhil-inputs-menu" role="dialog"
        :aria-label="`${menu.board} inputs`" tabindex="-1"
        @keydown.esc.prevent="close"
    >
        <VhilInputs :board="menu.board" />
        <div class="vhil-actions">
            <span class="vhil-spacer" />
            <button type="button" class="vhil-btn --small" @click="close">Close</button>
        </div>
    </div>
</template>

<script>
import {
    computed, defineComponent, nextTick, ref, watch,
} from 'vue';
import VhilInputs from './VhilInputs.vue';
import { live } from './session.js';

export default defineComponent({
    components: { VhilInputs },
    setup() {
        const el = ref(null);
        const menu = computed(() => live.menu);
        const close = () => { live.menu = null; };
        watch(menu, async (m) => {
            if (!m) return;
            await nextTick();
            const box = el.value;
            if (!box) return;
            const x = Math.max(8, Math.min(m.x, window.innerWidth - box.offsetWidth - 8));
            const y = Math.max(48, Math.min(m.y, window.innerHeight - box.offsetHeight - 8));
            box.style.transform = `translate(${x}px, ${y}px)`;
            (box.querySelector('button[role="switch"], input, button') || box).focus();
        });
        return { el, menu, close };
    },
});
</script>
