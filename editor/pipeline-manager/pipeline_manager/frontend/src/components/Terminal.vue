<!--
Copyright (c) 2022-2024 Antmicro <www.antmicro.com>

SPDX-License-Identifier: Apache-2.0
-->

<!--
A terminal instance's log. vHIL: a plain virtualised log (src/vhil/LogView.vue)
in place of the hterm.js terminal (CHANGELOG-VHIL.md).
-->

<template>
    <LogView
        id="pm-terminal"
        :entries="entries"
        :readonly="readonly"
        :label="terminalInstance ?? 'Terminal'"
        @input="onInput"
    />
</template>

<script>
import { defineComponent, computed } from 'vue';
import { terminalStore } from '../core/stores';
import getExternalApplicationManager from '../core/communication/ExternalApplicationManager';
import LogView from '../vhil/LogView.vue';

export default defineComponent({
    components: { LogView },
    props: {
        terminalInstance: {
            type: String,
        },
    },
    setup(props) {
        const externalApplicationManager = getExternalApplicationManager();
        const entries = computed(() => terminalStore.logs[props.terminalInstance] ?? []);
        const readonly = computed(() => terminalStore.isReadOnly(props.terminalInstance) !== false);
        const onInput = (text) => {
            if (!readonly.value) {
                externalApplicationManager.requestTerminalRead(props.terminalInstance, text);
            }
        };
        return { entries, readonly, onInput };
    },
});
</script>

<style lang="scss" scoped>
#pm-terminal {
    position: relative;
    width: 100%;
    height: 100%;
}
</style>
