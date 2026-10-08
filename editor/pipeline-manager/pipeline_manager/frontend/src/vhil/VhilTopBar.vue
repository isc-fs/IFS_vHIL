<!--
vHIL: the workspace's top bar, 40 px (step 7 of
docs/architecture/editor-workspace.md; CHANGELOG-VHIL.md), in place of
Pipeline Manager's NavBar: the mark, what is open, one firmware chip per
board (opens the ref picker), the scenario Run runs and its duration (step
10: a scenario's own), Run and Stop, the mode, Commit… and Open PR
(dialogs), and the theme. In REPLAY (step 9) the scenario and duration give
way to the run's clock, a scrubber over its virtual time. In LIVE (step 15)
they give way to the session's clock ("t=12.345 s · RTF 0.98×"), who
controls it, Pause/Resume and Stop; ● Live starts one, and "Save session as
scenario" records it.
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
            v-if="live" class="vhil-live" role="group" :aria-label="`Live session ${replay.id}`"
        >
            <span class="vhil-live-dot" aria-hidden="true" />
            <a
                class="vhil-scrub-run mono" :href="runPage(replay.id)"
                target="_blank" rel="noopener" :title="`Run ${replay.id} in a new tab (it watches)`"
            >run {{ replay.id }}</a>
            <output class="vhil-live-clock mono num" :title="clockTitle">{{ clock }}</output>
            <span
                class="vhil-live-role" :class="`--${session.role}`" :title="roleTitle"
            >{{ roleText }}</span>
            <button
                v-if="canTake" type="button" class="vhil-btn --small" @click="take"
                title="Take this session's control: its holder let it go"
            >Take control</button>
            <span v-if="idle" class="vhil-live-idle" role="status">
                ▲ {{ idle }}
                <button
                    type="button" class="vhil-btn --small" :disabled="!control"
                    @click="keepAlive"
                >Keep alive</button>
            </span>
            <button
                type="button" class="vhil-btn" :disabled="!control" aria-keyshortcuts="Space"
                :title="`${session.paused ? 'Resume' : 'Pause'} virtual time (Space)`"
                @click="pauseLive"
            >{{ session.paused ? '▶ Resume' : '❚❚ Pause' }}</button>
            <button
                type="button" class="vhil-btn" :disabled="!control" aria-keyshortcuts="Shift+F5"
                title="Stop the session (Shift+F5): it ends at the end of the next slice"
                @click="stopLive"
            >■ Stop</button>
            <button
                type="button" class="vhil-btn --small"
                title="Leave: the session goes on without this tab until it is idle"
                @click="exitReplay"
            >✕ Leave</button>
        </div>
        <div
            v-else-if="ws.mode === 'REPLAY'" class="vhil-scrub" role="group"
            :aria-label="`Run ${replay.id} clock`"
        >
            <button
                type="button" class="vhil-scrub-run mono vhil-link"
                :title="`Run ${replay.id}: its record, tests and files (the Artifacts tab)`"
                @click="showArtifacts"
            >run {{ replay.id }}</button>
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
            v-if="ws.mode === 'DESIGN'" class="vhil-top-scen"
            title="The scenario Run runs: its stimuli, watches and expects (the Scenario tab)"
        >
            <span class="vhil-visually-hidden">Scenario</span>
            <select
                :value="scen.name" :disabled="!ws.id" aria-label="Scenario for Run"
                @change="(ev) => pickScenario(ev.target.value)"
            >
                <option value="">no scenario</option>
                <option v-for="s in scen.list" :key="s.name" :value="s.name">
                    ▸ {{ s.name }}{{ s.unsaved ? ' (new)' : '' }}
                </option>
            </select>
        </label>
        <label
            v-if="ws.mode === 'DESIGN'"
            class="vhil-duration"
            title="Virtual time from power-on; each board spends its bootloader's 2 s first"
        >
            <span>Run for</span>
            <input
                v-model.number="duration"
                type="number" min="1" max="600000" step="100"
                class="mono" aria-label="Run duration, virtual ms"
            />
            <span class="muted">ms</span>
        </label>

        <div v-if="!live" class="vhil-run-buttons">
            <button
                type="button" class="vhil-btn --primary" :disabled="ws.busy || running"
                aria-keyshortcuts="F5" title="Run the saved system (F5)" @click="runNow"
            >▶ Run <kbd>F5</kbd></button>
            <button
                type="button" class="vhil-btn" :disabled="!running"
                aria-keyshortcuts="Shift+F5" title="Stop the run (Shift+F5)" @click="stopRun"
            >■ Stop</button>
            <button
                type="button" class="vhil-btn" :disabled="ws.busy || running || !ws.id"
                :title="LIVE_TITLE"
                @click="startLive"
            ><span class="vhil-live-dot --idle" aria-hidden="true" /> Live</button>
            <button
                type="button" class="vhil-btn" :disabled="ws.busy || running || !ws.id || ws.isNew"
                title="Run the native tests (tests/sim) on this system: a pytest run"
                @click="ws.dialog = 'pytest'"
            >Tests…</button>
        </div>
        <button
            v-if="recorded" type="button" class="vhil-btn"
            title="The session's ops as a new scenario of this system, in the Scenario tab"
            @click="ws.dialog = 'save-session'"
        >Save as scenario…</button>

        <span
            class="vhil-mode" :class="`--${ws.mode.toLowerCase()}`" role="status"
            :aria-label="`Mode: ${ws.mode}`"
        >{{ ws.mode }}</span>
        <span
            v-if="live && session.held" class="vhil-held" role="status"
            :title="`Every board is paused: ${heldText(session.held)}`"
        >· {{ heldText(session.held) }}</span>

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
    ws, boardFirmware, cycleTheme, exitReplay, isLive, keepAlive, pauseLive, pickScenario,
    runActive, runNow, startLive, stopLive, stopRun,
} from './workspace.js';
import { live as session, take } from './session.js';
import { clockText, idleText } from './live.js';
import { heldText } from './debug.js';
import './debug.css';
import './live.css';
import { edited, scen } from './scenarios.js';
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
        // The selected scenario's own virtual time, else the run's.
        const duration = computed({
            get: () => (scen.doc ? scen.doc.virtual_ms : ws.virtualMs),
            set: (v) => {
                ws.virtualMs = v;
                if (scen.doc) {
                    scen.doc.virtual_ms = v;
                    edited();
                }
            },
        });
        const step = computed(() => Math.max(100, Math.ceil(replay.end / 20000 / 100) * 100));
        // -- LIVE ----------------------------------------------------------
        const live = computed(() => isLive());
        const control = computed(() => session.role === 'control' && !session.ended);
        const clock = computed(() => clockText(replay.end, session.rtf, session.paused));
        const clockTitle = 'Virtual time from power-on, and the real-time factor: virtual over '
            + 'wall time over the last second (1.00× is as fast as the bench)';
        const roleText = computed(() => {
            if (!session.connected) return 'connecting…';
            if (session.role === 'control') return 'you control it';
            return session.holder ? `view only · ${session.holder} controls` : 'view only';
        });
        const roleTitle = computed(() => (session.role === 'control'
            ? 'This tab sends the session\'s ops; other tabs on this run watch'
            : 'This tab watches: one tab controls a live session'));
        const canTake = computed(() => session.role === 'view' && session.mayControl
            && !session.holder);
        const idle = computed(() => idleText(session.idleLeft));
        // A live run, now or replayed: its ops can become a scenario.
        const recorded = computed(() => Boolean(replay.run?.scenario?.live)
            && (live.value || ws.mode === 'REPLAY'));
        const showArtifacts = () => {
            ws.layout.dock = true;
            ws.layout.dockTab = 'artifacts';
        };
        return {
            showArtifacts,
            LIVE_TITLE: 'A live session of the saved system: drive its buses and pins as on the '
                + 'bench, until you stop it',
            live,
            session,
            heldText,
            control,
            clock,
            clockTitle,
            roleText,
            roleTitle,
            canTake,
            take,
            idle,
            recorded,
            startLive,
            stopLive,
            pauseLive,
            keepAlive,
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
            scen,
            pickScenario,
            duration,
        };
    },
});
</script>
