<!--
Copyright (c) 2022-2023 Antmicro <www.antmicro.com>

SPDX-License-Identifier: Apache-2.0
-->

<!--
The entrypoint of the application.

vHIL: the vHIL workspace (step 7 of docs/architecture/editor-workspace.md;
CHANGELOG-VHIL.md) in place of NavBar, the canvas alone and TerminalPanel:
a 40 px top bar, the activity rail and sidebar (the palette is teleported
into it from the canvas), the canvas, the inspector, the bottom dock (the
terminal is its Log tab) and the status strip. The workspace's own code is
in src/vhil/.
-->

<template>
    <div>
        <LoadingScreen v-if="loading" />
        <div
            id="container"
            class="vhil-ws"
            :class="{
                '--no-sidebar': !ws.layout.sidebar,
                '--no-inspector': !ws.layout.inspector,
                '--dock-collapsed': !ws.layout.dock,
            }"
            :style="gridVars"
            @pointerup.capture="changed"
            @keyup.capture="changed"
            @change.capture="changed"
        >
            <VhilTopBar :tick="tick" />
            <VhilRail />
            <main ref="canvas" class="vhil-canvas" aria-label="Canvas">
                <Editor
                    class="inner-editor"
                    :view-model="editorManager.baklavaView"
                    @setLoad="handleLoad"
                    :loading="loading"
                >
                    <template #palette>
                        <Teleport to="#vhil-palette-host" defer>
                            <Palette />
                        </Teleport>
                    </template>
                </Editor>
                <CustomSidebar />
            </main>
            <VhilInspector v-show="ws.layout.inspector" :tick="tick" />
            <VhilDock />
            <VhilStatus :tick="tick" />
            <VhilRefPicker :tick="tick" />
            <VhilDialogs />
            <VhilInputsMenu />
        </div>
    </div>
</template>

<script>
import {
    ref, computed, provide, onMounted, onBeforeUnmount, watch,
} from 'vue';
import EditorManager from '../core/EditorManager.js';
import LoadingScreen from './LoadingScreen.vue';
import Palette from './Palette.vue';
import Editor from '../custom/Editor.vue';
import CustomSidebar from '../custom/CustomSidebar.vue';
import '@baklavajs/themes/dist/classic.css';
import getExternalApplicationManager from '../core/communication/ExternalApplicationManager.js';
import VhilTopBar from '../vhil/VhilTopBar.vue';
import VhilRail from '../vhil/VhilRail.vue';
import VhilInspector from '../vhil/VhilInspector.vue';
import VhilDock from '../vhil/VhilDock.vue';
import VhilStatus from '../vhil/VhilStatus.vue';
import VhilRefPicker from '../vhil/VhilRefPicker.vue';
import VhilDialogs from '../vhil/VhilDialogs.vue';
import VhilInputsMenu from '../vhil/VhilInputsMenu.vue';
import '../vhil/workspace.css';
import {
    ws, refreshDirty, setCanvas, start,
} from '../vhil/workspace.js';
import { liveGraph } from '../vhil/graph.js';
import { onKeyDown } from '../vhil/shortcuts.js';

export default {
    components: {
        Editor,
        LoadingScreen,
        Palette,
        CustomSidebar,
        VhilTopBar,
        VhilRail,
        VhilInspector,
        VhilDock,
        VhilStatus,
        VhilRefPicker,
        VhilDialogs,
        VhilInputsMenu,
    },
    setup() {
        const editorManager = EditorManager.getEditorManagerInstance();
        const loading = ref(false);

        const cache = {};
        // Importing all assets to a cache so that they can be accessed dynamically during runtime
        function importAll(r) {
            r.keys().forEach((key) => (cache[key] = r(key))); // eslint-disable-line no-return-assign,max-len
        }
        try {
            importAll(require.context('../../assets', true, /\.(svg|png|jpg|jpeg|gif|webp|avif)$/));
        } catch (e) {
            // assets directory not found
        } finally {
            editorManager.baklavaView.cache = cache;
        }

        const handleLoad = (value) => {
            loading.value = value;
        };

        // The node sidebar (CustomSidebar) asks for it; NavBar used to give it.
        provide('hoveredOver', () => {});

        // vHIL: the workspace. The grid's sizes are per viewer (localStorage).
        const canvas = ref(null);
        const gridVars = computed(() => ({
            '--vhil-inspector-w': `${ws.layout.inspectorW}px`,
            '--vhil-dock-h': `${ws.layout.dockH}px`,
        }));

        // What follows the graph (chips, inspector, the dirty dot) is
        // recomputed after each interaction, and again once the backend has
        // had time to answer one (a role change comes back as a new graph).
        const tick = ref(0);
        const syncSelection = () => {
            const selected = liveGraph()?.selectedNodes ?? [];
            ws.selectedId = selected.length === 1 ? selected[0].id : null;
        };
        let later = null;
        const changed = () => {
            syncSelection();
            tick.value += 1;
            refreshDirty();
            clearTimeout(later);
            later = setTimeout(() => {
                tick.value += 1;
                refreshDirty();
            }, 800);
        };
        watch(() => (liveGraph()?.selectedNodes ?? []).map((n) => n.id).join(','), syncSelection);

        const externalApplicationManager = getExternalApplicationManager();
        const syncBackend = () => {
            ws.backend = externalApplicationManager.isConnected() ? 'connected' : 'disconnected';
            tick.value += 1;
        };

        onMounted(() => {
            setCanvas(canvas.value);
            // Capture: the canvas handles (and stops) some keys itself.
            window.addEventListener('keydown', onKeyDown, true);
            externalApplicationManager.registerConnectionHook(syncBackend);
            externalApplicationManager.registerDisconnectionHook(syncBackend);
            if (externalApplicationManager.isConnected()) syncBackend();
            start().then(changed);
        });
        onBeforeUnmount(() => window.removeEventListener('keydown', onKeyDown, true));

        return {
            editorManager,
            handleLoad,
            loading,
            ws,
            canvas,
            gridVars,
            tick,
            changed,
        };
    },
};
</script>
