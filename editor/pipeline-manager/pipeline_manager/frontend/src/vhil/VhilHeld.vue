<!--
vHIL: the frozen-bus banner (step 17; docs/debugger.md). While a LIVE
session is held at a debugger stop, every board is paused (a halted CPU holds
the emulation's time), so the buses carry nothing: the Debug and Bus tabs say
so, with when and where.
-->

<template>
    <div v-if="live.held && replay.live" class="vhil-held-banner" role="status">
        <span aria-hidden="true">❚❚</span>
        <span>
            Bus frozen: all boards paused at t={{ seconds(at) }}
            · {{ heldText(live.held) }}
        </span>
    </div>
</template>

<script>
import { computed, defineComponent } from 'vue';
import { live } from './session.js';
import { replay, seconds } from './replay.js';
import { heldText } from './debug.js';

export default defineComponent({
    setup() {
        // When the board halted (Renode's time), else the clock's.
        const at = computed(() => {
            replay.debugVersion; // eslint-disable-line no-unused-expressions
            const stop = live.held ? replay.debug.boards.get(live.held.board)?.stop : null;
            return stop?.at_us ?? replay.end;
        });
        return {
            live, replay, at, seconds, heldText,
        };
    },
});
</script>
