<!--
vHIL: the Scenario tab's timeline (step 11 of
docs/architecture/editor-workspace.md; CHANGELOG-VHIL.md), above its table,
on the virtual-time axis REPLAY's scrubber uses. A lane per bus and per
board (timeline.js lays them out): a diamond per can_send, a hatched bar per
can_periodic (to its stop), a step per gpio/analog set, a bracket per expect
over its window; the watches are the gutter below. Drag a mark to move it
(1/5/10 ms snap), or focus it and use the arrow keys; Ctrl+wheel zooms
around the pointer. After a run, each expect's result sits on its lane at
its evidence time (✓/✕; a click replays the run there), the frames and pin
edges the scenario names are drawn faintly behind the marks (actual against
expected), and the scrubber's time is a line. SVG geometry is attributes and
colour is classes (scenario.css): nothing goes through style attributes.
-->

<template>
    <section class="vhil-tl" aria-label="Scenario timeline">
        <div class="vhil-tl-bar">
            <span class="vhil-tl-title">Timeline</span>
            <label class="vhil-bus-field">Snap
                <select v-model.number="snapMs" class="vhil-input mono">
                    <option v-for="s in SNAPS" :key="s" :value="s">{{ s }} ms</option>
                </select>
            </label>
            <div class="vhil-seg" role="group" aria-label="Zoom">
                <button type="button" class="vhil-seg-btn" @click="zoomBy(1 / 1.5)">−</button>
                <button type="button" class="vhil-seg-btn" @click="fit">fit</button>
                <button type="button" class="vhil-seg-btn" @click="zoomBy(1.5)">+</button>
            </div>
            <span class="muted vhil-tl-hint">
                Drag a mark (or ←/→) to move it · Ctrl+wheel zooms
            </span>
            <span v-if="overlay" class="vhil-tl-legend mono">
                <span class="vhil-tl-key --actual">▮ actual (run {{ overlay.runId }})</span>
                <span class="vhil-scen-ok">✓ pass</span>
                <span class="vhil-count-error">✕ fail</span>
            </span>
        </div>
        <div class="vhil-tl-body">
            <div class="vhil-tl-labels" aria-hidden="true">
                <div class="vhil-tl-axis-label mono">t</div>
                <div
                    v-for="lane in lanes" :key="lane.id" class="vhil-tl-label"
                    :class="`--${lane.kind}`" :title="lane.id"
                >{{ lane.name }}</div>
            </div>
            <div ref="scrollEl" class="vhil-tl-scroll" @wheel="onWheel">
                <svg
                    class="vhil-tl-svg" :width="width" :height="height"
                    :viewBox="`0 0 ${width} ${height}`" role="group"
                    aria-label="Rows on the time axis"
                >
                    <defs>
                        <pattern
                            id="vhil-tl-hatch" width="6" height="6"
                            patternUnits="userSpaceOnUse" patternTransform="rotate(45)"
                        >
                            <line x1="0" y1="0" x2="0" y2="6" class="vhil-tl-hatch" />
                        </pattern>
                    </defs>
                    <rect
                        class="vhil-tl-axis" x="0" y="0" :width="width" :height="AXIS_H"
                        @click="axisClick"
                    />
                    <g v-for="t in tickList" :key="`t${t}`">
                        <line
                            class="vhil-tl-grid" :x1="x(t)" :x2="x(t)" :y1="AXIS_H" :y2="height"
                        />
                        <text class="vhil-tl-tick" :x="x(t) + 3" y="12">{{ tickLabel(t) }}</text>
                    </g>
                    <rect
                        v-for="(lane, i) in lanes" :key="lane.id" class="vhil-tl-lane"
                        :class="{ '--odd': i % 2 }" x="0" :y="laneY(lane.id)"
                        :width="width" :height="LANE_H"
                    />
                    <rect
                        class="vhil-tl-end" :x="x(endMs)" :y="AXIS_H"
                        :width="Math.max(0, width - x(endMs))" :height="height - AXIS_H"
                    />

                    <!-- actual: the run's frames and edges for what the scenario names -->
                    <g v-if="overlay" class="vhil-tl-actual">
                        <path
                            v-for="p in actualPaths" :key="p.key" :d="p.d"
                            :class="`--${p.kind}`"
                        />
                    </g>

                    <g
                        v-for="m in marks" :key="m.key" class="vhil-tl-mark"
                        :class="markClass(m)"
                        :transform="`translate(${x(m.t0)},${laneY(m.lane)})`"
                        tabindex="0" role="button"
                        :aria-label="`${m.key} ${m.label} at ${m.t0} ms`"
                        :aria-pressed="m.key === scen.selected"
                        @pointerdown="(ev) => dragStart(ev, m)"
                        @keydown.left.prevent="nudge(m, -1)"
                        @keydown.right.prevent="nudge(m, 1)"
                        @keydown.enter.prevent="scen.selected = m.key"
                        @focus="scen.selected = m.key"
                    >
                        <title>{{ m.key }} · {{ m.label }} · {{ m.t0 }} ms</title>
                        <polygon
                            v-if="m.shape === 'diamond'" class="vhil-tl-diamond"
                            :points="`0,${mid - 6} 6,${mid} 0,${mid + 6} -6,${mid}`"
                        />
                        <template v-else-if="m.shape === 'bar'">
                            <rect
                                class="vhil-tl-bar-fill" x="0" :y="mid - 5"
                                :width="Math.max(2, (m.t1 - m.t0) * scale)" height="10"
                            />
                            <line
                                class="vhil-tl-bar-start" x1="0" x2="0"
                                :y1="mid - 7" :y2="mid + 7"
                            />
                        </template>
                        <path
                            v-else-if="m.shape === 'step'" class="vhil-tl-step"
                            :d="step(m)"
                        />
                        <path
                            v-else-if="m.shape === 'expect'" class="vhil-tl-bracket"
                            :d="bracket(m)"
                        />
                        <text
                            class="vhil-tl-text" :y="mid + 4"
                            :x="m.shape === 'bar' || m.shape === 'expect' ? 4 : 9"
                        >
                            {{ m.label }}
                        </text>
                    </g>

                    <!-- each expect's result at its evidence time -->
                    <g
                        v-for="r in resultMarks" :key="`r${r.key}`" class="vhil-tl-result"
                        :class="r.passed ? '--pass' : '--fail'"
                        :transform="`translate(${x(r.t)},${laneY(r.lane)})`"
                        tabindex="0" role="button"
                        :aria-label="resultLabel(r)"
                        @click="jump(r)" @keydown.enter.prevent="jump(r)"
                    >
                        <title>{{ r.detail }}</title>
                        <circle cx="0" :cy="mid" r="7" />
                        <text x="0" :y="mid + 4" text-anchor="middle">
                            {{ r.passed ? '✓' : '✕' }}
                        </text>
                    </g>

                    <line
                        v-if="cursor !== null" class="vhil-tl-cursor"
                        :x1="x(cursor)" :x2="x(cursor)" y1="0" :y2="height"
                    />
                </svg>
            </div>
        </div>
        <div class="vhil-tl-gutter" role="list" aria-label="Watches">
            <span class="vhil-tl-gutter-label muted">watch</span>
            <span v-if="!watches.length" class="muted">
                none: + Add row… → watch samples a symbol or records a pin
            </span>
            <button
                v-for="w in watches" :key="w.key" type="button" role="listitem"
                class="vhil-tl-chip mono" :class="{ '--selected': w.key === scen.selected }"
                @click="scen.selected = w.key"
            >{{ w.text }}</button>
        </div>
    </section>
</template>

<script>
import {
    computed, defineComponent, onBeforeUnmount, ref, watch,
} from 'vue';
import {
    SNAPS, moveItem, parseSignal, snap,
} from './scenario.js';
import {
    AXIS_H, LANE_H, fitScale, lanesOf, marksOf, tickLabel, ticks, zoomAround,
} from './timeline.js';
import { edited, scen } from './scenarios.js';
import { replay, store } from './replay.js';
import { openRun } from './workspace.js';

export default defineComponent({
    props: {
        buses: { type: Array, default: () => [] },
        boards: { type: Array, default: () => [] },
    },
    setup(props) {
        const scrollEl = ref(null);
        const snapMs = ref(5);
        const scale = ref(0.12); // px per ms
        const mid = LANE_H / 2;

        const endMs = computed(() => Number(scen.doc?.virtual_ms) || 3000);
        const lanes = computed(() => {
            scen.version; // eslint-disable-line no-unused-expressions
            return lanesOf(scen.doc, { buses: props.buses, boards: props.boards });
        });
        const laneIndex = computed(() => new Map(lanes.value.map((l, i) => [l.id, i])));
        const laneY = (id) => AXIS_H + (laneIndex.value.get(id) ?? 0) * LANE_H;
        const marks = computed(() => {
            scen.version; // eslint-disable-line no-unused-expressions
            return marksOf(scen.doc, endMs.value).filter((m) => m.lane);
        });
        const width = computed(() => Math.ceil(endMs.value * scale.value) + 40);
        const height = computed(() => AXIS_H + Math.max(1, lanes.value.length) * LANE_H);
        const x = (t) => t * scale.value;
        const tickList = computed(() => ticks(endMs.value, scale.value));
        const watches = computed(() => {
            scen.version; // eslint-disable-line no-unused-expressions
            return (scen.doc?.watch || []).map((w, i) => ({
                key: `watch[${i}]`,
                text: w.kind === 'pin' ? `pin ${w.board}.${w.pin}`
                    : `${w.board}.${w.name} · ${w.size} B / ${w.period_ms} ms`,
            }));
        });

        // -- the run against it ------------------------------------------------
        // The run shown: REPLAY's when it ran this scenario, else the last
        // result seen here (no actual data then, only the results).
        const overlay = computed(() => {
            const res = scen.results;
            if (!res) return null;
            const replaying = replay.id === res.runId && replay.state === 'ready';
            return { runId: res.runId, replaying };
        });
        const cursor = computed(() => (replay.state === 'ready' && overlay.value?.replaying
            ? replay.t / 1000 : null));
        const resultMarks = computed(() => {
            scen.version; // eslint-disable-line no-unused-expressions
            const expects = scen.results?.summary?.expects || [];
            const byKey = new Map(marks.value.map((m) => [m.key, m]));
            return expects.map((r) => {
                const key = `expect[${r.index}]`;
                const m = byKey.get(key);
                if (!m) return null;
                const t = r.t_us === null || r.t_us === undefined ? m.t1 : r.t_us / 1000;
                return {
                    key, lane: m.lane, t, passed: r.passed, detail: r.detail, tUs: r.t_us,
                };
            }).filter(Boolean);
        });
        // What the scenario names: (bus, id) of its frames and frame expects,
        // (board, pin) of its pins, as faint ticks and level lines.
        const actualPaths = computed(() => {
            replay.version; // eslint-disable-line no-unused-expressions
            scen.version; // eslint-disable-line no-unused-expressions
            if (!overlay.value?.replaying || !scen.doc) return [];
            const frames = new Map(); // "bus/id" -> lane
            const pins = new Map(); // "board/pin" -> lane
            (scen.doc.stimuli || []).forEach((s) => {
                if (s.bus) frames.set(`${s.bus}/${s.id}`, `bus:${s.bus}`);
                if (s.kind === 'gpio') pins.set(`${s.board}/${s.pin}`, `board:${s.board}`);
            });
            (scen.doc.expect || []).forEach((e) => {
                const sig = parseSignal(e.signal);
                if (!sig) return;
                if (sig.kind === 'pin') pins.set(`${sig.owner}/${sig.item}`, `board:${sig.owner}`);
                if (sig.kind === 'frame') {
                    const msgs = scen.contract?.buses?.[sig.owner] || {};
                    const id = /^0x/i.test(sig.item) ? parseInt(sig.item, 16)
                        : Object.values(msgs).find((m) => m.name === sig.item)?.id;
                    if (id !== undefined) frames.set(`${sig.owner}/${id}`, `bus:${sig.owner}`);
                }
            });
            const ticksBy = new Map(); // lane -> Set of px columns
            for (let i = 0; i < store.length; i += 1) {
                const lane = frames.get(`${store.busName(i)}/${store.idAt(i)}`);
                if (lane && laneIndex.value.has(lane)) {
                    if (!ticksBy.has(lane)) ticksBy.set(lane, new Set());
                    ticksBy.get(lane).add(Math.round(x(store.tAt(i) / 1000)));
                }
            }
            const out = [];
            ticksBy.forEach((cols, lane) => {
                const y = laneY(lane);
                const d = [...cols].map((c) => `M${c},${y + 3}V${y + 7}`).join('');
                out.push({ key: `f${lane}`, kind: 'frames', d });
            });
            const edges = replay.edges || [];
            pins.forEach((lane, key) => {
                if (!laneIndex.value.has(lane)) return;
                const [board, pin] = key.split('/');
                const mine = edges.filter((e) => e.board === board && e.pin === pin);
                if (!mine.length) return;
                const y = laneY(lane);
                const hi = y + 4;
                const lo = y + LANE_H - 4;
                let { level } = mine[0];
                let d = `M0,${level ? hi : lo}`;
                mine.forEach((e) => {
                    const px = x(e.t_us / 1000);
                    d += `H${px}V${e.level ? hi : lo}`;
                    level = e.level;
                });
                d += `H${x(endMs.value)}`;
                out.push({ key: `p${key}`, kind: 'pin', d });
            });
            return out;
        });

        const jump = async (r) => {
            const runId = scen.results?.runId;
            if (!runId || r.tUs === null || r.tUs === undefined) return;
            if (replay.id !== runId) await openRun(runId, { tab: 'scenario' });
            if (replay.id === runId) replay.t = Math.min(replay.end, r.tUs);
        };
        const axisClick = (ev) => {
            if (cursor.value === null) return;
            const box = ev.currentTarget.getBoundingClientRect();
            const t = (ev.clientX - box.left) / scale.value;
            replay.t = Math.max(0, Math.min(replay.end, Math.round(t * 1000)));
        };

        // -- editing: drag and keys ------------------------------------------
        let drag = null;
        const move = (ev) => {
            if (!drag) return;
            const t = snap(drag.t0 + (ev.clientX - drag.x0) / scale.value, snapMs.value);
            if (t !== Number(drag.mark.item.at_ms)) {
                moveItem(drag.mark.item, t);
                scen.version += 1;
                drag.moved = true;
            }
        };
        const end = () => {
            if (!drag) return;
            const { moved } = drag;
            drag = null;
            document.removeEventListener('pointermove', move);
            document.removeEventListener('pointerup', end);
            if (moved) edited();
        };
        const dragStart = (ev, m) => {
            if (ev.button !== 0) return;
            scen.selected = m.key;
            drag = {
                mark: m, x0: ev.clientX, t0: Number(m.item.at_ms || 0), moved: false,
            };
            document.addEventListener('pointermove', move);
            document.addEventListener('pointerup', end);
        };
        const nudge = (m, dir) => {
            moveItem(m.item, snap(Number(m.item.at_ms || 0) + dir * snapMs.value, snapMs.value));
            edited();
        };
        onBeforeUnmount(end);

        // -- zoom -------------------------------------------------------------
        const zoomAt = (factor, px) => {
            const el = scrollEl.value;
            const anchor = ((el?.scrollLeft || 0) + px) / scale.value;
            const z = zoomAround(scale.value, factor, anchor, px);
            scale.value = z.pxPerMs;
            requestAnimationFrame(() => { if (el) el.scrollLeft = z.scrollLeft; });
        };
        const onWheel = (ev) => {
            if (!ev.ctrlKey && !ev.metaKey) return;
            ev.preventDefault();
            const box = scrollEl.value.getBoundingClientRect();
            zoomAt(ev.deltaY < 0 ? 1.25 : 0.8, ev.clientX - box.left);
        };
        const zoomBy = (factor) => zoomAt(factor, (scrollEl.value?.clientWidth || 600) / 2);
        const fit = () => {
            scale.value = fitScale(endMs.value, (scrollEl.value?.clientWidth || 800) - 40);
        };
        // Fit a newly selected scenario.
        watch(() => scen.name, () => requestAnimationFrame(fit), { immediate: true });

        const markClass = (m) => ({
            [`--${m.shape}`]: true,
            '--selected': m.key === scen.selected,
            '--error': scen.errors.some((e) => e.startsWith(m.key)),
        });
        const step = (m) => (m.up ? `M-8,${mid + 6} H0 V${mid - 6} H8`
            : `M-8,${mid - 6} H0 V${mid + 6} H8`);
        const resultLabel = (r) => `${r.key} ${r.passed ? 'passed' : 'failed'} at ${r.t} ms: `
            + 'replay there';
        const bracket = (m) => {
            const w = Math.max(2, (m.t1 - m.t0) * scale.value);
            return `M0,4V${LANE_H - 4}M0,${mid}H${w}M${w},4V${LANE_H - 4}`;
        };

        return {
            SNAPS,
            AXIS_H,
            LANE_H,
            scen,
            scrollEl,
            snapMs,
            scale,
            mid,
            endMs,
            lanes,
            laneY,
            marks,
            width,
            height,
            x,
            tickList,
            tickLabel,
            watches,
            overlay,
            cursor,
            resultMarks,
            actualPaths,
            jump,
            axisClick,
            dragStart,
            nudge,
            onWheel,
            zoomBy,
            fit,
            markClass,
            bracket,
            step,
            resultLabel,
        };
    },
});
</script>
