<!--
vHIL: the dock's Debug tab (step 17 of docs/architecture/editor-workspace.md;
docs/debugger.md), following the canvas selection: a board's debugger in a
LIVE session. A step toolbar with labelled buttons and their keys (Continue
F5, Step over F10, Step into F11, Step out Shift+F11, Break), breakpoints by
function, file:line or address, the firmware source at the stop (read only,
from the image's checkout; GET /api/runs/<id>/debug/source) with a
breakpoint gutter and the current line, or its disassembly, the watch list
(shared with the State tab: VhilWatches.vue) and the frozen-bus banner
(VhilHeld.vue). The stack, locals and registers are in the inspector
(VhilDebugInspect.vue). A REPLAY can't be debugged: the tab says so and
lists where the session stopped.
-->

<template>
    <div class="vhil-debug">
        <VhilHeld />
        <p v-if="!replay.id" class="vhil-placeholder muted">
            The debugger works in a LIVE session: start one (● Live), then set a breakpoint
            on a board here.
        </p>
        <template v-else-if="!replay.live">
            <p class="vhil-debug-note" role="note">
                <span aria-hidden="true">ⓘ</span>
                Run {{ replay.id }} is a REPLAY: a recorded trace with no emulator behind it,
                so it can't be debugged. Start a LIVE session (● Live) to set breakpoints and
                step. Where this session stopped:
            </p>
            <p v-if="!stops.length" class="vhil-placeholder muted">
                It never stopped in a debugger.
            </p>
            <ul v-else class="vhil-debug-stops vhil-list">
                <li v-for="(s, i) in stops" :key="i">
                    <button type="button" class="vhil-btn --small" @click="seek(s.t_us)">
                        t={{ seconds(s.at_us ?? s.t_us) }}
                    </button>
                    <span class="mono">{{ stopText(s) }}</span>
                </li>
            </ul>
        </template>
        <template v-else>
            <div class="vhil-debug-bar" role="toolbar" aria-label="Debugger">
                <label class="vhil-debug-board">Board
                    <select v-model="dbg.board" class="vhil-input">
                        <option v-for="b in boards" :key="b" :value="b">{{ b }}</option>
                    </select>
                </label>
                <span class="vhil-debug-state" :class="`--${state}`" role="status">
                    <span aria-hidden="true">{{ STATE_GLYPH[state] }}</span> {{ stateText }}
                </span>
                <button
                    type="button" class="vhil-btn" :disabled="!stop || !!cannot"
                    aria-keyshortcuts="F5" title="Continue (F5)" @click="go('continue')"
                >▶ Continue <kbd>F5</kbd></button>
                <button
                    type="button" class="vhil-btn" :disabled="!stop || !!cannot"
                    aria-keyshortcuts="F10" title="Step over: the next line (F10)"
                    @click="go('next')"
                >⤼ Step over <kbd>F10</kbd></button>
                <button
                    type="button" class="vhil-btn" :disabled="!stop || !!cannot"
                    aria-keyshortcuts="F11" title="Step into the call (F11)" @click="go('step')"
                >↓ Step into <kbd>F11</kbd></button>
                <button
                    type="button" class="vhil-btn" :disabled="!stop || !!cannot"
                    aria-keyshortcuts="Shift+F11" title="Step out: run to the caller (Shift+F11)"
                    @click="go('finish')"
                >↑ Step out <kbd>Shift+F11</kbd></button>
                <button
                    type="button" class="vhil-btn" :disabled="!!stop || !!cannot || live.paused"
                    title="Break: stop the board where it is" @click="interrupt"
                >❚❚ Break</button>
                <button
                    v-if="attached" type="button" class="vhil-btn --small" :disabled="!!cannot"
                    title="Delete its breakpoints, let it run, stop its GDB" @click="detach"
                >Detach</button>
            </div>
            <p class="vhil-debug-status muted" role="status" aria-live="polite">
                {{ cannot ? `Watching: ${cannot}` : dbg.status }}
            </p>

            <div class="vhil-debug-main">
                <section class="vhil-debug-code" aria-label="Source">
                    <div class="vhil-debug-codebar">
                        <span class="mono vhil-debug-file" :title="file">{{ fileLabel }}</span>
                        <div class="vhil-seg" role="group" aria-label="Code view">
                            <button
                                type="button" class="vhil-seg-btn"
                                :aria-pressed="dbg.view === 'source'"
                                @click="dbg.view = 'source'"
                            >Source</button>
                            <button
                                type="button" class="vhil-seg-btn"
                                :aria-pressed="dbg.view === 'disasm'"
                                :disabled="!stop" @click="showDisasm"
                            >Disassembly</button>
                        </div>
                    </div>
                    <div v-if="dbg.view === 'disasm'" ref="codeEl" class="vhil-debug-lines">
                        <p v-if="!disasm.length" class="vhil-placeholder muted">
                            {{ stop ? 'Loading the disassembly…' : 'Stopped boards only.' }}
                        </p>
                        <table v-else class="vhil-debug-asm mono">
                            <tbody>
                                <tr
                                    v-for="ins in disasm" :key="ins.addr"
                                    :class="{ '--current': isPc(ins.addr) }"
                                    :aria-current="isPc(ins.addr) ? 'step' : undefined"
                                >
                                    <td class="vhil-debug-mark" aria-hidden="true">
                                        {{ isPc(ins.addr) ? '▶' : '' }}
                                    </td>
                                    <td class="num">{{ ins.addr }}</td>
                                    <td class="muted">&lt;{{ ins.func }}+{{ ins.offset }}&gt;</td>
                                    <td>{{ ins.inst }}</td>
                                </tr>
                            </tbody>
                        </table>
                    </div>
                    <div v-else ref="codeEl" class="vhil-debug-lines">
                        <p v-if="!file" class="vhil-placeholder muted">
                            {{ stop ? 'No source for this frame (no line information).'
                                : 'The source shows where the board stops. Set a breakpoint below, '
                                    + 'or Break.' }}
                        </p>
                        <p v-else-if="source.error" class="vhil-placeholder vhil-warn">
                            {{ fileLabel }}: {{ source.error }}
                        </p>
                        <p v-else-if="!source.lines" class="vhil-placeholder muted">
                            Loading {{ fileLabel }}…
                        </p>
                        <ol
                            v-else class="vhil-debug-src mono" :start="from"
                            aria-label="Source lines"
                        >
                            <li
                                v-for="r in rows" :key="r.n"
                                :class="{ '--current': r.current, '--bp': r.bp }"
                                :aria-current="r.current ? 'step' : undefined"
                            >
                                <button
                                    type="button" class="vhil-debug-gutter"
                                    :disabled="!!cannot"
                                    :aria-label="r.bp ? `Clear the breakpoint at line ${r.n}`
                                        : `Set a breakpoint at line ${r.n}`"
                                    :aria-pressed="r.bp"
                                    @click="toggle(r.n)"
                                >{{ r.bp ? '●' : '' }}</button>
                                <span class="vhil-debug-ln num" aria-hidden="true">{{ r.n }}</span>
                                <span class="vhil-debug-mark" aria-hidden="true">
                                    {{ r.current ? '▶' : '' }}
                                </span>
                                <code class="vhil-debug-text">{{ r.text }}</code>
                            </li>
                        </ol>
                        <p v-if="more" class="muted">
                            Lines {{ from }}–{{ to }} of {{ source.lines.length }}
                            <button type="button" class="vhil-btn --small" @click="around += 400">
                                Show more
                            </button>
                        </p>
                    </div>
                </section>

                <section class="vhil-debug-side" aria-label="Breakpoints and watches">
                    <h4>Breakpoints</h4>
                    <form class="vhil-debug-add" @submit.prevent="add">
                        <label class="vhil-visually-hidden" for="vhil-debug-loc">
                            Breakpoint at
                        </label>
                        <input
                            id="vhil-debug-loc" v-model="where" class="vhil-input mono"
                            placeholder="function, file.c:123 or 0x08020000"
                            :disabled="!!cannot" autocomplete="off" spellcheck="false"
                        />
                        <button
                            type="submit" class="vhil-btn --small"
                            :disabled="!!cannot || !where.trim()"
                        >
                            Add
                        </button>
                    </form>
                    <p v-if="!breakpoints.length" class="muted">None.</p>
                    <ul v-else class="vhil-list vhil-debug-bps">
                        <li v-for="bp in breakpoints" :key="bp.number">
                            <span class="vhil-debug-bpdot" aria-hidden="true">●</span>
                            <span class="mono" :title="bp.func">
                                {{ bp.number }} · {{ bpText(bp) }}
                            </span>
                            <button
                                type="button" class="vhil-btn --small" :disabled="!!cannot"
                                :aria-label="`Remove breakpoint ${bp.number}`"
                                @click="clear(bp.number)"
                            >✕ Remove</button>
                        </li>
                    </ul>
                    <h4>Watches</h4>
                    <VhilWatches :board="dbg.board" />
                    <h4>Timeouts</h4>
                    <p class="muted vhil-debug-hint">
                        No list of the firmware timeouts that would have fired: the contracts give
                        each frame's period, not the timeouts the receiving firmware applies, so
                        it can't be told from them (docs/debugger.md).
                    </p>
                </section>
            </div>
        </template>
    </div>
</template>

<script>
import {
    computed, defineComponent, nextTick, ref, watch,
} from 'vue';
import VhilHeld from './VhilHeld.vue';
import VhilWatches from './VhilWatches.vue';
import { replay, seconds } from './replay.js';
import { ws } from './workspace.js';
import { nodeById, nodeName } from './graph.js';
import { heldText, sourceWindow, whereText } from './debug.js';
import {
    dbg, addBreakpoint, cannotDebug, debugOp, loadDisasm, loadSource, resume, runBoards, stopOf,
    toggleLine, live,
} from './debugui.js';
import './debug.css';

const STATE_GLYPH = {
    held: '■', running: '▶', detached: '○', idle: '○',
};

export default defineComponent({
    components: { VhilHeld, VhilWatches },
    setup() {
        const boards = computed(() => runBoards());
        const cannot = computed(() => cannotDebug());
        // Follow the canvas: a selected board, else the board held, else the first.
        watch([() => ws.selectedId, () => live.held, boards], () => {
            const sel = ws.selectedId ? nodeById(ws.selectedId) : null;
            const name = sel ? nodeName(sel) : '';
            if (boards.value.includes(name)) dbg.board = name;
            else if (live.held?.board) dbg.board = live.held.board;
            else if (!boards.value.includes(dbg.board)) dbg.board = boards.value[0] || '';
        }, { immediate: true });

        const info = computed(() => {
            replay.debugVersion; // eslint-disable-line no-unused-expressions
            return replay.debug.boards.get(dbg.board) ?? null;
        });
        const stop = computed(() => stopOf(dbg.board));
        const attached = computed(() => Boolean(info.value?.attached));
        const state = computed(() => {
            if (stop.value) return 'held';
            if (attached.value) return 'running';
            return 'detached';
        });
        // "breakpoint in ecu (control.cpp:35)" of a stop record.
        const stopText = (st) => heldText({ board: st.board, reason: st.reason, ...st.frame });
        const bpText = (bp) => (bp.file ? whereText(bp) : (bp.func || bp.addr));
        const stateText = computed(() => ({
            held: `stopped: ${stopText({ board: dbg.board, ...stop.value })}`,
            running: 'runs (debugger attached)',
            detached: 'no debugger attached: a breakpoint attaches one',
        }[state.value]));
        const breakpoints = computed(() => info.value?.breakpoints ?? []);

        // The frame shown: the stop's, or the one picked in the inspector.
        const frame = computed(() => {
            const s = stop.value;
            if (!s) return null;
            return (s.frames || [])[dbg.frame] || s.frame || null;
        });
        const file = computed(() => frame.value?.file || '');
        const line = computed(() => frame.value?.line || 0);
        const fileLabel = computed(() => (file.value ? whereText(frame.value) : 'no source'));
        const source = computed(() => dbg.sources[`${dbg.board}|${file.value}`] || {});
        const around = ref(150);
        const count = computed(() => source.value.lines?.length || 0);
        const range = computed(() => sourceWindow(line.value || 1, count.value, around.value));
        const from = computed(() => range.value[0]);
        const to = computed(() => range.value[1]);
        const marks = computed(() => {
            replay.debugVersion; // eslint-disable-line no-unused-expressions
            return replay.debug.linesOf(dbg.board, file.value);
        });
        const rows = computed(() => (source.value.lines || []).slice(from.value - 1, to.value)
            .map((text, i) => {
                const n = from.value + i;
                return {
                    n, text, current: n === line.value, bp: marks.value.has(n),
                };
            }));
        const more = computed(() => count.value > 0 && (from.value > 1 || to.value < count.value));
        const disasm = computed(() => dbg.disasm?.list ?? []);
        const pc = computed(() => parseInt(stop.value?.frame?.addr || '0', 16));
        const isPc = (addr) => parseInt(addr, 16) === pc.value;

        const codeEl = ref(null);
        const scrollToLine = async () => {
            await nextTick();
            const el = codeEl.value?.querySelector('[aria-current="step"]');
            if (el) el.scrollIntoView({ block: 'center' });
        };
        watch([() => dbg.board, file], () => {
            if (file.value) loadSource(dbg.board, file.value);
        }, { immediate: true });
        watch([line, () => source.value.lines, () => dbg.view, disasm], scrollToLine);
        watch(stop, () => {
            dbg.disasm = null;
            if (stop.value && dbg.view === 'disasm') loadDisasm(dbg.board);
        });

        const showDisasm = () => {
            dbg.view = 'disasm';
            loadDisasm(dbg.board);
        };
        const where = ref('');
        const add = async () => {
            if (await addBreakpoint(dbg.board, where.value)) where.value = '';
        };
        const toggle = (n) => toggleLine(dbg.board, file.value, n, marks.value.get(n));
        const clear = (number) => debugOp(dbg.board, { cmd: 'clear', number });
        const go = (how) => resume(how);
        const interrupt = () => debugOp(dbg.board, { cmd: 'interrupt' });
        const detach = () => debugOp(dbg.board, { cmd: 'detach' });

        const stops = computed(() => {
            replay.debugVersion; // eslint-disable-line no-unused-expressions
            return replay.debug.stops.slice(-200);
        });
        const seek = (t) => { replay.t = Math.max(0, Math.min(replay.end, t)); };

        return {
            replay,
            live,
            dbg,
            boards,
            cannot,
            stop,
            attached,
            state,
            stateText,
            STATE_GLYPH,
            breakpoints,
            file,
            line,
            fileLabel,
            source,
            around,
            from,
            rows,
            more,
            disasm,
            isPc,
            codeEl,
            showDisasm,
            where,
            add,
            toggle,
            clear,
            go,
            interrupt,
            detach,
            stops,
            seek,
            seconds,
            stopText,
            bpText,
        };
    },
});
</script>
