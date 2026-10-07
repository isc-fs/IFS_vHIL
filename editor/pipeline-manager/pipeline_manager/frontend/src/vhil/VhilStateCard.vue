<!--
vHIL: one board's state card (step 13 of docs/architecture/editor-workspace.md;
docs/state-view.md), at a virtual time, from the boards' state over a run
(state.js StateTrace): the FSM state at 18 px with how long it has held it
and the one before; contactors and relays as square pills with text
(filled = closed); active faults as pills with their reason and age,
cleared ones outlined; key values with units; "stale" when its state's
source went silent. In the State tab (`history`) it adds the history: the
FSM and relay lanes over the run and the transitions, where a click moves
the scrubber. Status is always text and a glyph, never colour alone.
-->

<template>
    <article
        class="vhil-state-card"
        :class="{ '--stale': card.stale, '--faulted': card.faulted, '--compact': !history }"
        :aria-label="`${board} state`"
    >
        <header class="vhil-state-head">
            <h3 class="vhil-state-board">{{ board }}</h3>
            <span v-if="card.firmware" class="vhil-state-fw mono">{{ card.firmware }}</span>
            <span v-if="card.stale" class="vhil-state-stalebadge" :title="staleTitle">
                <span aria-hidden="true">◌</span> stale
            </span>
        </header>

        <p v-if="!card.state" class="muted">No state view for {{ card.firmware || board }}.</p>
        <section v-else class="vhil-state-fsm" aria-live="polite">
            <span class="vhil-state-key muted">{{ card.state.label }}</span>
            <span class="vhil-state-now" :class="{ '--none': !card.state.has || card.boot }">
                <template v-if="card.boot">
                    <span aria-hidden="true">◔</span> in bootloader
                    <span class="vhil-state-hint muted">the app starts at 2 s</span>
                </template>
                <template v-else>
                    {{ card.state.text }}
                    <span v-if="card.state.noEnum" class="vhil-state-hint muted">
                        raw · no enum
                    </span>
                </template>
            </span>
            <span v-if="card.state.has && !card.boot" class="vhil-state-since muted mono num">
                for {{ duration(t - card.state.since) }}<template v-if="card.state.prev">
                    · was {{ card.state.prev.text }}</template>
            </span>
            <span v-if="card.state.note" class="vhil-state-hint muted">{{ card.state.note }}</span>
        </section>

        <ul v-if="relays.length" class="vhil-state-pills" aria-label="Contactors and relays">
            <li
                v-for="r in relays" :key="r.label"
                class="vhil-relay" :class="`--${r.look.cls}`" :title="`${r.label}: ${r.look.title}`"
            >
                <span aria-hidden="true">{{ r.look.glyph }}</span>
                {{ r.label }}
                <span class="vhil-visually-hidden">{{ r.look.word }}</span>
            </li>
        </ul>

        <div class="vhil-state-faults" role="group" aria-label="Faults">
            <span v-if="!card.active.length && !card.cleared.length" class="vhil-state-ok">
                <span aria-hidden="true">✓</span> no active faults
            </span>
            <span
                v-for="f in card.active" :key="`a-${f.label}`" class="vhil-fault --active"
                :title="`active since ${seconds(f.since)}`"
            >
                <span aria-hidden="true">✕</span>
                {{ f.label }}<template v-if="f.reason">: {{ f.reason }}</template>
                <span class="mono num"> · {{ duration(t - f.since) }}</span>
            </span>
            <span
                v-for="f in card.cleared" :key="`c-${f.label}`" class="vhil-fault --cleared"
                :title="`raised at ${seconds(f.since)}, cleared at ${seconds(f.clearedAt)}`"
            >
                <span aria-hidden="true">○</span>
                {{ f.label }}<template v-if="f.reason">: {{ f.reason }}</template>
                <span class="mono num"> · cleared {{ duration(t - f.clearedAt) }} ago</span>
            </span>
        </div>

        <dl v-if="card.values.length" class="vhil-state-values">
            <div
                v-for="v in card.values" :key="v.label" class="vhil-state-value"
                :class="{ '--stale': v.stale, '--none': !v.has }"
                :title="v.note || (v.stale ? 'stale: its source went silent' : '')"
            >
                <dt>{{ v.label }}</dt>
                <dd class="mono num">
                    {{ v.text }}<span
                        v-if="v.has && v.unit" class="vhil-state-unit"
                    >{{ v.unit }}</span>
                    <span v-if="v.stale" class="vhil-state-hint"> stale</span>
                </dd>
            </div>
        </dl>

        <section v-if="history && lanes.length" class="vhil-state-history" aria-label="History">
            <div class="vhil-lanes">
                <div
                    v-for="lane in lanes" :key="lane.label" class="vhil-lane"
                    :class="`--${lane.kind}`"
                >
                    <span class="vhil-lane-label">{{ lane.label }}</span>
                    <!-- Pointer only: the transitions below are the keyboard's way -->
                    <div
                        class="vhil-lane-track" aria-hidden="true"
                        :title="`${lane.label} over the run: click to move the scrubber`"
                        @click="(ev) => seekAt(ev)"
                    >
                        <span
                            v-for="(s, i) in lane.segments" :key="i"
                            class="vhil-lane-seg"
                            :class="segClass(lane, s)"
                            :style="segStyle(s)"
                            :title="`${s.text}: ${seconds(s.t0)} – ${seconds(s.t1)}`"
                        >{{ lane.kind === 'state' ? s.text : '' }}</span>
                        <span class="vhil-lane-cursor" :style="cursorStyle" aria-hidden="true" />
                    </div>
                </div>
            </div>
            <ol class="vhil-transitions" aria-label="Transitions">
                <li v-for="h in card.state?.history || []" :key="h.t">
                    <button
                        type="button" class="vhil-transition"
                        :class="{ '--current': h.current, '--future': h.future }"
                        :aria-current="h.current ? 'true' : undefined"
                        @click="$emit('seek', h.t)"
                    >
                        <span class="mono num">{{ seconds(h.t) }}</span>
                        <span>{{ h.text }}</span>
                    </button>
                </li>
            </ol>
        </section>
    </article>
</template>

<script>
import { computed, defineComponent } from 'vue';
import { duration } from './state.js';
import './state.css';

const seconds = (us) => (us === null || us === undefined ? '–' : `${(us / 1e6).toFixed(3)} s`);

export default defineComponent({
    props: {
        trace: { type: Object, required: true },
        board: { type: String, required: true },
        t: { type: Number, required: true },
        end: { type: Number, default: 0 },
        version: { type: Number, default: 0 },
        history: { type: Boolean, default: false },
    },
    emits: ['seek'],
    setup(props, { emit }) {
        const card = computed(() => {
            props.version; // eslint-disable-line no-unused-expressions
            return props.trace.cardAt(props.board, props.t);
        });
        const span = computed(() => Math.max(1, props.end || props.trace.end));
        const lanes = computed(() => {
            props.version; // eslint-disable-line no-unused-expressions
            return props.history ? props.trace.lanesOf(props.board, span.value) : [];
        });
        // Positions as percentages of the run, set through the CSSOM (Vue's
        // :style objects): the editor's CSP is style-src 'self'.
        const pct = (us) => `${Math.max(0, Math.min(100, (100 * us) / span.value))}%`;
        const segStyle = (s) => ({ left: pct(s.t0), width: pct(Math.max(0, s.t1 - s.t0)) });
        const cursorStyle = computed(() => ({ left: pct(props.t) }));
        // The state lane's segments take the series colours by value (with
        // their text on them); a relay's are filled when closed.
        const segClass = (lane, s) => {
            if (lane.kind === 'state') return `--s${(Math.abs(Number(s.raw)) % 6) + 1}`;
            return s.raw ? '--on' : '--off';
        };
        // A relay as a pill: closed (filled ■), open (□) or not yet seen.
        const LOOKS = {
            on: {
                cls: 'on', glyph: '■', word: 'closed', title: 'closed (high)',
            },
            off: {
                cls: 'off', glyph: '□', word: 'open', title: 'open (low)',
            },
            none: {
                cls: 'unknown', glyph: '?', word: 'no data', title: 'no data',
            },
        };
        const relays = computed(() => card.value.relays.map((r) => {
            let look = LOOKS.none;
            if (r.on !== null) look = r.on ? LOOKS.on : LOOKS.off;
            return { ...r, look };
        }));
        const seekAt = (ev) => {
            const r = ev.currentTarget.getBoundingClientRect();
            const x = Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width));
            emit('seek', Math.round(x * span.value));
        };
        const staleTitle = computed(() => {
            const s = card.value.state;
            if (!s?.has) return 'no state yet';
            return `last update ${duration(props.t - s.t)} before t`;
        });
        return {
            card,
            relays,
            lanes,
            segStyle,
            cursorStyle,
            segClass,
            seekAt,
            staleTitle,
            duration,
            seconds,
        };
    },
});
</script>
