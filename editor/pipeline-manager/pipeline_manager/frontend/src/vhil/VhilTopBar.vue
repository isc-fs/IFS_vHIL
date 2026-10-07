<!--
vHIL: the workspace's top bar, 40 px (step 7 of
docs/architecture/editor-workspace.md; CHANGELOG-VHIL.md), in place of
Pipeline Manager's NavBar: the mark, what is open, one firmware chip per
board (opens the ref picker), the run's duration, Run and Stop, the mode,
Commit… and Open PR (dialogs), and the theme. In REPLAY (step 9) the
duration gives way to the run's clock, a scrubber over its virtual time.
-->

<template>
    <header class="vhil-top" aria-label="Workspace">
        <a class="vhil-mark" href="/" title="IFS vHIL: systems and runs">
            <svg viewBox="0 0 32 20" width="32" height="20" aria-hidden="true" focusable="false">
                <rect class="vhil-mark-fill" width="32" height="20" rx="3" />
                <text x="16" y="14.5" text-anchor="middle">IFS</text>
            </svg>
            <span class="vhil-visually-hidden">IFS vHIL home</span>
        </a>

        <div class="vhil-crumb" :title="crumbTitle">
            <template v-if="ws.id">
                <span class="vhil-crumb-system">{{ ws.id }}</span>
                <span class="vhil-crumb-at">@</span>
                <span class="vhil-crumb-branch mono">{{ ws.branch || 'checked-out tree' }}</span>
                <span v-if="ws.isNew" class="vhil-crumb-new">new</span>
                <span v-if="ws.dirty" class="vhil-dirty" role="img" aria-label="unsaved edits">
                    ●
                </span>
            </template>
            <span v-else class="muted">No system open</span>
        </div>

        <div class="vhil-chips" role="group" aria-label="Firmware per board">
            <button
                v-for="chip in chips"
                :key="chip.id"
                type="button"
                class="vhil-chip"
                :class="{ '--picked': chip.picked, '--none': !chip.fw }"
                :aria-label="`${chip.name} firmware: ${chip.label}. Pick a ref`"
                :aria-expanded="ws.picker?.nodeId === chip.id"
                @click="(ev) => openPicker(chip.id, ev)"
            >
                <span class="vhil-chip-board">{{ chip.name }}</span>
                <span class="vhil-chip-ref mono">{{ chip.label }}</span>
            </button>
        </div>

        <div
            v-if="ws.mode === 'REPLAY'" class="vhil-scrub" role="group"
            :aria-label="`Run ${replay.id} clock`"
        >
            <a
                class="vhil-scrub-run mono" :href="runPage(replay.id)"
                target="_blank" rel="noopener"
                :title="`Run ${replay.id}'s page: its signals, log and artifacts`"
            >run {{ replay.id }}</a>
            <input
                v-model.number="replay.t"
                type="range" min="0" :max="replay.end" :step="step"
                class="vhil-scrub-range" :disabled="replay.state !== 'ready'"
                :aria-label="`Virtual time of run ${replay.id}`"
                :aria-valuetext="`t=${seconds(replay.t)}`"
            />
            <output class="vhil-scrub-clock mono num">t={{ seconds(replay.t) }}</output>
            <span class="vhil-scrub-end mono num muted">/ {{ seconds(replay.end) }}</span>
            <button
                type="button" class="vhil-btn --small" title="Back to DESIGN" @click="exitReplay"
            >✕ Exit</button>
        </div>
        <label
            v-else
            class="vhil-duration"
            title="Virtual time from power-on; each board spends its bootloader's 2 s first"
        >
            <span>Run for</span>
            <input
                v-model.number="ws.virtualMs"
                type="number" min="1" max="600000" step="100"
                class="mono" aria-label="Run duration, virtual ms"
            />
            <span class="muted">ms</span>
        </label>

        <div class="vhil-run-buttons">
            <button
                type="button" class="vhil-btn --primary" :disabled="ws.busy || running"
                aria-keyshortcuts="F5" title="Run the saved system (F5)" @click="runNow"
            >▶ Run <kbd>F5</kbd></button>
            <button
                type="button" class="vhil-btn" :disabled="!running"
                aria-keyshortcuts="Shift+F5" title="Stop the run (Shift+F5)" @click="stopRun"
            >■ Stop</button>
        </div>

        <span
            class="vhil-mode" :class="`--${ws.mode.toLowerCase()}`" role="status"
            :aria-label="`Mode: ${ws.mode}`"
        >{{ ws.mode }}</span>

        <div class="vhil-git-buttons">
            <button type="button" class="vhil-btn" :disabled="!ws.id" @click="ws.dialog = 'commit'">
                Commit…
            </button>
            <button type="button" class="vhil-btn" :disabled="!ws.id" @click="ws.dialog = 'pr'">
                Open PR
            </button>
        </div>

        <button
            type="button" class="vhil-btn vhil-theme"
            :aria-label="`Theme: ${ws.theme}. Switch theme`" :title="`Theme: ${ws.theme}`"
            @click="cycleTheme"
        >{{ themeGlyph }} {{ ws.theme }}</button>
        <button
            type="button" class="vhil-btn" aria-keyshortcuts="?" title="Keyboard shortcuts (?)"
            aria-label="Keyboard shortcuts" @click="ws.dialog = 'shortcuts'"
        >?</button>
    </header>
</template>

<script>
import { computed, defineComponent } from 'vue';
import {
    ws, boardFirmware, cycleTheme, exitReplay, runActive, runNow, stopRun,
} from './workspace.js';
import { boards, nodeName } from './graph.js';
import { replay, seconds } from './replay.js';
import { runPage } from './api.js';

export default defineComponent({
    props: {
        // Bumped when the graph changes, so the chips follow it.
        tick: { type: Number, default: 0 },
    },
    setup(props) {
        const chips = computed(() => {
            props.tick; // eslint-disable-line no-unused-expressions
            if (!ws.id) return [];
            return boards().map((node) => {
                const app = boardFirmware(node).find((f) => f.what === 'app');
                let label = 'no firmware';
                if (app?.fw) label = app.ref || app.fw.ref;
                else if (app) label = `${app.fwId}: none`;
                return {
                    id: node.id,
                    name: nodeName(node),
                    fw: app?.fw ?? null,
                    label,
                    picked: Boolean(app?.ref),
                };
            });
        });
        const crumbTitle = computed(() => (ws.id
            ? `${ws.id} @ ${ws.branch || 'the checked-out tree'}${ws.ref ? ` (${ws.ref.slice(0, 8)})` : ''}${ws.dirty ? ': unsaved edits' : ''}`
            : ''));
        const running = computed(() => Boolean(runActive()));
        const themeGlyph = computed(() => ({ dark: '◐', light: '◑', auto: '◒' })[ws.theme] || '◐');
        const openPicker = (nodeId, ev) => {
            const r = ev.currentTarget.getBoundingClientRect();
            ws.picker = ws.picker?.nodeId === nodeId ? null
                : { nodeId, x: r.left, y: r.bottom + 4 };
        };
        // 100 µs a step, coarser over a long run (at most ~20000 steps).
        const step = computed(() => Math.max(100, Math.ceil(replay.end / 20000 / 100) * 100));
        return {
            ws,
            chips,
            crumbTitle,
            running,
            themeGlyph,
            openPicker,
            runNow,
            stopRun,
            cycleTheme,
            replay,
            seconds,
            step,
            exitReplay,
            runPage,
        };
    },
});
</script>
