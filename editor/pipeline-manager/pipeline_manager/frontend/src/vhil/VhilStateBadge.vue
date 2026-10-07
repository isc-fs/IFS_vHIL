<!--
vHIL: a run's state as text with a glyph, never colour alone
(docs/architecture/editor-workspace.md, "Tokens": status).
-->

<template>
    <span class="vhil-badge" :class="`--${state}`">
        <span aria-hidden="true">{{ glyph }}</span> {{ state }}
    </span>
</template>

<script>
import { computed, defineComponent } from 'vue';

const GLYPHS = {
    queued: '○', building: '◌', running: '●', passed: '✓', failed: '✕', error: '▲', cancelled: '⊘',
};

export default defineComponent({
    props: { state: { type: String, required: true } },
    setup(props) {
        return { glyph: computed(() => GLYPHS[props.state] ?? '?') };
    },
});
</script>
