<!--
vHIL: the dock's Artifacts tab (step 18 of docs/architecture/editor-workspace.md;
CHANGELOG-VHIL.md): the run on the workspace beyond its trace, what the
shell's run page showed before it was retired. Its record (state, system and
ref, owner, what it ran, virtual and wall time, worker, frames per bus,
tests, firmware, its error; Cancel while it may be), a pytest run's JUnit
cases with their failure snapshots, the worker's error, the tail of
pytest.txt, and every file, each opened from the API (text or an
attachment, never rendered: vhil/server/runs.py). Loaded when the tab is
shown for a run, again with Refresh.
-->

<template>
    <div class="vhil-artifacts">
        <p v-if="!replay.id" class="vhil-placeholder muted">
            Open a run from Runs in the sidebar: its record, test results and files show here.
        </p>
        <template v-else-if="run">
            <h3>
                Run {{ run.id }}
                <span class="vhil-run-state" :class="`--${run.state}`">
                    {{ STATE_GLYPH[run.state] || '' }} {{ run.state }}
                </span>
                <button type="button" class="vhil-btn --small" :disabled="loading" @click="load">
                    ↻ Refresh
                </button>
                <button
                    v-if="run.can_cancel && !isTerminal(run.state)" type="button"
                    class="vhil-btn --small" @click="cancel"
                >■ Cancel</button>
            </h3>
            <dl class="vhil-run-meta">
                <div><dt>System</dt><dd>{{ run.system }}</dd></div>
                <div>
                    <dt>Ref</dt>
                    <dd class="mono">
                        {{ run.ref_name ? `${run.ref_name} ` : '' }}{{
                            (run.ref || 'working tree').slice(0, 12) }}
                    </dd>
                </div>
                <div v-if="run.owner"><dt>Owner</dt><dd>{{ run.owner }}</dd></div>
                <div><dt>Ran</dt><dd :title="scenarioText(run)">{{ scenarioText(run) }}</dd></div>
                <div><dt>Virtual</dt><dd class="mono num">{{ ms(run.virtual_us) }} ms</dd></div>
                <div><dt>Wall</dt><dd class="mono num">{{ wallText(run) }}</dd></div>
                <div><dt>Created</dt><dd class="mono">{{ created }}</dd></div>
                <div v-if="run.worker"><dt>Worker</dt><dd class="muted">{{ run.worker }}</dd></div>
                <div v-if="frames"><dt>Frames</dt><dd class="mono">{{ frames }}</dd></div>
                <div v-if="tests"><dt>Tests</dt><dd>{{ tests }}</dd></div>
                <div v-if="aligned">
                    <dt>Moved</dt>
                    <dd :title="aligned">{{ aligned }}</dd>
                </div>
                <div v-if="firmware">
                    <dt>Firmware</dt><dd class="mono" :title="firmware">{{ firmware }}</dd>
                </div>
            </dl>
            <p v-if="run.summary?.error" class="vhil-warn">✕ {{ run.summary.error }}</p>
            <p v-if="error" class="vhil-warn">{{ error }}</p>

            <template v-if="cases.length">
                <h3>
                    Tests
                    <span class="muted">
                        {{ counts.passed }} passed · {{ counts.failed }} failed ·
                        {{ counts.error }} errors · {{ counts.skipped }} skipped
                    </span>
                </h3>
                <table class="vhil-junit">
                    <thead><tr><th>Outcome</th><th>Test</th><th class="num">Time</th></tr></thead>
                    <tbody>
                        <tr v-for="(c, i) in cases" :key="i">
                            <td class="vhil-outcome" :class="`--${c.outcome}`">
                                {{ OUTCOME_GLYPH[c.outcome] }} {{ c.outcome }}
                            </td>
                            <td>
                                <span class="muted">{{ c.classname }}</span>::{{ c.name }}
                                <details v-if="c.message || c.text">
                                    <summary>{{ c.message.slice(0, 200) || 'details' }}</summary>
                                    <pre>{{ c.text }}</pre>
                                </details>
                                <button
                                    v-for="s in snapshotsFor(c, snaps)" :key="s.dir"
                                    type="button" class="vhil-btn --small" @click="openSnap(s)"
                                >snapshot</button>
                            </td>
                            <td class="num mono">{{ c.time.toFixed(2) }} s</td>
                        </tr>
                    </tbody>
                </table>
            </template>

            <template v-if="snaps.length">
                <h3>Failure snapshots</h3>
                <details
                    v-for="s in snaps" :id="`vhil-snap-${s.name}`" :key="s.dir"
                    :open="openSnaps.has(s.dir)" @toggle="(ev) => toggled(s, ev)"
                >
                    <summary class="mono">{{ s.name }}</summary>
                    <div v-for="f in s.files" :key="f">
                        <a :href="artifactUrl(run.id, f)" target="_blank" rel="noopener">
                            {{ f.split('/').pop() }}
                        </a>
                        <pre v-if="texts[f] !== undefined">{{ texts[f] }}</pre>
                    </div>
                </details>
            </template>

            <template v-if="names.includes('worker-error.txt')">
                <h3>Worker error</h3>
                <pre>{{ texts['worker-error.txt'] ?? 'loading…' }}</pre>
            </template>
            <details v-if="names.includes('pytest.txt')" open>
                <summary><b>pytest.txt</b> <span class="muted">(its end)</span></summary>
                <pre ref="pytestEl">{{ texts['pytest.txt'] ?? 'loading…' }}</pre>
            </details>

            <h3>Files</h3>
            <ul v-if="names.length" class="vhil-files">
                <li v-for="n in names" :key="n">
                    <a :href="artifactUrl(run.id, n)" target="_blank" rel="noopener" class="mono">
                        {{ n }}
                    </a>
                </li>
            </ul>
            <p v-else class="muted">
                {{ loading ? 'Loading…' : `No artifacts${isTerminal(run.state) ? '' : ' yet'}.` }}
            </p>
        </template>
        <p v-else class="vhil-placeholder muted">Loading run {{ replay.id }}…</p>
    </div>
</template>

<script>
import {
    computed, defineComponent, nextTick, reactive, ref, watch,
} from 'vue';
import { call, text } from './api.js';
import { replay } from './replay.js';
import { ws } from './workspace.js';
import {
    artifactUrl, firmwareText, isTerminal, junitCounts, parseJunit, scenarioText, snapshotsFor,
    snapshotsOf, tail, wallText,
} from './artifacts.js';
import './signals.css';

const STATE_GLYPH = {
    passed: '✓', failed: '✕', error: '▲', cancelled: '⊘', running: '●', queued: '○',
};
const OUTCOME_GLYPH = {
    passed: '✓', failed: '✕', error: '▲', skipped: '⊘',
};
const PYTEST_TAIL = 400000;

export default defineComponent({
    setup() {
        const run = ref(null);
        const names = ref([]);
        const cases = ref([]);
        const texts = reactive({});
        const openSnaps = reactive(new Set());
        const loading = ref(false);
        const error = ref('');
        const pytestEl = ref(null);
        let loadedFor = null;

        const snaps = computed(() => snapshotsOf(names.value));
        const counts = computed(() => junitCounts(cases.value));
        const ms = (us) => ((us || 0) / 1000).toFixed(0);
        const created = computed(() => (run.value?.created || '').replace('T', ' ').slice(0, 19));
        const frames = computed(() => Object.entries(run.value?.summary?.frames || {})
            .map(([b, n]) => `${b} ${n}`).join(' · '));
        const tests = computed(() => {
            const s = run.value?.summary || {};
            return s.tests === undefined ? ''
                : `${s.tests} tests · ${s.failures} failed · ${s.errors} errors · ${s.skipped} skipped`;
        });
        const aligned = computed(() => (run.value?.summary?.aligned || [])
            .map((a) => `${a.row} ${a.at_us / 1000} → ${a.applied_us / 1000} ms`).join(', '));
        const firmware = computed(() => (run.value ? firmwareText(run.value) : ''));

        async function loadText(name, max = 0) {
            if (texts[name] !== undefined) return;
            texts[name] = 'loading…';
            try {
                texts[name] = tail(await text(artifactUrl(run.value.id, name)), max);
            } catch (e) {
                texts[name] = `could not load: ${e.message}`;
            }
        }

        async function load() {
            const { id } = replay;
            if (!id) return;
            loading.value = true;
            error.value = '';
            Object.keys(texts).forEach((k) => delete texts[k]);
            try {
                const [r, n] = await Promise.all([
                    call('GET', `/api/runs/${id}`), call('GET', `/api/runs/${id}/artifacts`)]);
                if (replay.id !== id) return;
                run.value = r;
                names.value = n;
                cases.value = [];
                if (n.includes('worker-error.txt')) loadText('worker-error.txt');
                if (n.includes('pytest.txt')) {
                    // pytest's summary is at its end: scrolled there.
                    loadText('pytest.txt', PYTEST_TAIL).then(async () => {
                        await nextTick();
                        if (pytestEl.value) pytestEl.value.scrollTop = pytestEl.value.scrollHeight;
                    });
                }
                if (n.includes('junit.xml')) {
                    const xml = await text(artifactUrl(id, 'junit.xml'));
                    if (replay.id === id) cases.value = parseJunit(xml);
                }
            } catch (e) {
                error.value = `Artifacts of run ${id}: ${e.message}`;
            } finally {
                loading.value = false;
            }
        }

        // Loaded when shown for a run not loaded yet (and the run's record
        // follows REPLAY's while it is not).
        const shown = () => ws.layout.dock && ws.layout.dockTab === 'artifacts';
        watch(() => [replay.id, shown()], () => {
            if (!replay.id) {
                run.value = null;
                loadedFor = null;
                return;
            }
            if (!run.value || run.value.id !== replay.id) run.value = replay.run;
            if (shown() && loadedFor !== replay.id) {
                loadedFor = replay.id;
                openSnaps.clear();
                load();
            }
        }, { immediate: true });

        const toggled = (s, ev) => {
            if (ev.target.open) {
                openSnaps.add(s.dir);
                s.files.forEach((f) => loadText(f));
            } else {
                openSnaps.delete(s.dir);
            }
        };
        const openSnap = async (s) => {
            openSnaps.add(s.dir);
            s.files.forEach((f) => loadText(f));
            await nextTick();
            document.getElementById(`vhil-snap-${s.name}`)?.scrollIntoView({ block: 'start' });
        };
        const cancel = async () => {
            try {
                run.value = await call('POST', `/api/runs/${run.value.id}/cancel`);
            } catch (e) {
                error.value = `Cancel: ${e.message}`;
            }
        };

        return {
            replay,
            run,
            names,
            cases,
            counts,
            texts,
            snaps,
            openSnaps,
            loading,
            error,
            pytestEl,
            ms,
            created,
            frames,
            tests,
            aligned,
            firmware,
            load,
            toggled,
            openSnap,
            cancel,
            artifactUrl,
            isTerminal,
            scenarioText,
            snapshotsFor,
            wallText,
            STATE_GLYPH,
            OUTCOME_GLYPH,
        };
    },
});
</script>
