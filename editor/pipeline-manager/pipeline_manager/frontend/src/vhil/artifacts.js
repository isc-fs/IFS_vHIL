/*
 * vHIL: the Artifacts tab's model (step 18 of
 * docs/architecture/editor-workspace.md; CHANGELOG-VHIL.md): what the
 * shell's run page showed of a run beyond its trace. Pure, no DOM, no Vue:
 * tests/js/artifacts.test.mjs runs it under node.
 *
 * A run's artifacts are files in its results directory
 * (GET /api/runs/<id>/artifacts, each served as text or an attachment, never
 * rendered: vhil/server/runs.py): pytest's junit.xml and pytest.txt, the
 * failure snapshots tests/sim/conftest.py writes under
 * sim-logs/failures/<test>-<when>/, worker-error.txt, renode.log, the trace.
 */

const TERMINAL = new Set(['passed', 'failed', 'error', 'cancelled']);
export const isTerminal = (state) => TERMINAL.has(state);

/** An artifact's URL: each path segment encoded. */
export const artifactUrl = (id, name) => `/api/runs/${id}/artifacts/${name.split('/').map(encodeURIComponent).join('/')}`;

const ENTITIES = {
    '&lt;': '<', '&gt;': '>', '&quot;': '"', '&apos;': '\'', '&amp;': '&',
};
const unescape = (s) => String(s || '').replace(/&(lt|gt|quot|apos|amp|#\d+|#x[0-9a-f]+);/gi, (m) => {
    if (ENTITIES[m]) return ENTITIES[m];
    const hex = /^&#x/i.test(m);
    return String.fromCodePoint(parseInt(m.slice(hex ? 3 : 2, -1), hex ? 16 : 10));
});
const attr = (tag, name) => unescape(new RegExp(`\\s${name}="([^"]*)"`).exec(tag)?.[1] || '');

/**
 * pytest's junit.xml as its test cases: {classname, name, time, outcome
 * (passed | failed | error | skipped), message, text}. pytest writes it
 * flat (testsuites > testsuite > testcase > failure|error|skipped), which is
 * all this reads.
 */
export function parseJunit(xml) {
    const cases = [];
    const re = /<testcase\b([^>]*?)(\/>|>([\s\S]*?)<\/testcase>)/g;
    let m = re.exec(xml);
    while (m) {
        const [, head, , body = ''] = m;
        const node = /<(failure|error|skipped)\b([^>]*?)(\/>|>([\s\S]*?)<\/\1>)/.exec(body);
        cases.push({
            classname: attr(head, 'classname'),
            name: attr(head, 'name'),
            time: Number(attr(head, 'time') || 0),
            outcome: node ? { failure: 'failed', error: 'error', skipped: 'skipped' }[node[1]] : 'passed',
            message: node ? attr(node[2], 'message') : '',
            text: node ? unescape((node[4] || '').replace(/^<!\[CDATA\[|\]\]>$/g, '')) : '',
        });
        m = re.exec(xml);
    }
    return cases;
}

/** {passed, failed, error, skipped} counts of the cases. */
export function junitCounts(cases) {
    const n = {
        passed: 0, failed: 0, error: 0, skipped: 0,
    };
    cases.forEach((c) => { n[c.outcome] += 1; });
    return n;
}

/** The failure snapshots among the artifact names: [{dir, name, files}]. */
export function snapshotsOf(names) {
    const groups = new Map();
    names.filter((n) => n.startsWith('sim-logs/failures/')).forEach((n) => {
        const dir = n.split('/').slice(0, 3).join('/');
        if (!groups.has(dir)) groups.set(dir, []);
        groups.get(dir).push(n);
    });
    return [...groups].map(([dir, files]) => ({ dir, name: dir.split('/')[2], files }));
}

/** A failed case's snapshots: its directory is <nodeid with non-word runs as
 *  _>-<when> (tests/sim/conftest.py _snapshot_report). */
export function snapshotsFor(c, snaps) {
    if (c.outcome !== 'failed' && c.outcome !== 'error') return [];
    const safe = (s) => s.replace(/[^\w.-]+/g, '_');
    const mod = c.classname.split('.').pop();
    return snaps.filter((s) => s.name.includes(safe(c.name)) && s.name.includes(mod));
}

/** The tail of a long text (pytest's summary is at its end). */
export function tail(text, max) {
    if (!max || text.length <= max) return text;
    return `… (${(text.length - max).toLocaleString('en')} characters cut; the file has all of it)\n${text.slice(-max)}`;
}

/** "ams: dev @ 1a2b3c4d5e6f, ams.bootloader: v1.7.0 @ …": each image's ref
 *  and the commit it ran (or will run: resolved when the run was created),
 *  else the image directory it ran, else the ref it asked for. */
export function firmwareText(run) {
    const commits = run.summary?.firmware_commits || run.firmware_commits || {};
    const used = run.summary?.firmware || {};
    const keys = [...new Set([...Object.keys(commits), ...Object.keys(used)])];
    if (keys.length) {
        return keys.map((k) => {
            const c = commits[k];
            if (c?.commit) return `${k}: ${c.ref} @ ${c.commit.slice(0, 12)}`;
            const dir = used[k] && String(used[k]).split('/').find((s) => /[@+]/.test(s));
            return `${k}: ${dir || used[k] || c?.ref || 'default'}`;
        }).join(', ');
    }
    return Object.entries(run.firmware || {}).map(([k, v]) => `${k}@${v ?? 'default'}`).join(', ');
}

/** "3 d ago", "5 h ago", "just now": a branch head's age. */
export function ageText(iso, now = Date.now()) {
    const t = Date.parse(iso || '');
    if (Number.isNaN(t)) return '';
    const s = Math.max(0, (now - t) / 1000);
    if (s < 3600) return s < 120 ? 'just now' : `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    if (s < 86400 * 60) return `${Math.floor(s / 86400)} d ago`;
    return new Date(t).toISOString().slice(0, 10);
}

/** "4.20 s", "3 min 7 s", "1 h 2 min", or "–". */
export function wallText(run, now = Date.now()) {
    if (!run.started) return '–';
    const end = run.finished ? Date.parse(run.finished) : now;
    const s = Math.max(0, (end - Date.parse(run.started)) / 1000);
    if (s < 60) return `${s.toFixed(s < 10 ? 2 : 1)} s`;
    const min = Math.floor(s / 60);
    return min < 60 ? `${min} min ${Math.round(s % 60)} s` : `${Math.floor(min / 60)} h ${min % 60} min`;
}

/** What a run ran: "run 3000 ms", "scenario tsms (6000 ms)", "pytest …", "live session". */
export function scenarioText(run) {
    const sc = run.scenario || {};
    if (sc.kind === 'pytest') return `pytest ${sc.select || ''}`;
    if (sc.live) return 'live session';
    return sc.name ? `scenario ${sc.name} (${sc.virtual_ms} ms)` : `run ${sc.virtual_ms} ms`;
}
