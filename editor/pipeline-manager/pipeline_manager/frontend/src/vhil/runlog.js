/**
 * The Log tab's lines (CHANGELOG-VHIL.md, live logs). A run's `log` trace
 * records carry a source and a level (vhil/runlog.py,
 * docs/integration-contract.md): each becomes an entry the Log shows as
 * time, source, level, text, and its source chips filter. The editor's own
 * messages stay plain strings (source `editor`). Pure, so
 * tests/js/runlog.test.mjs runs it under node.
 */

export const EDITOR = 'editor';
// A trace written before records had a source (contract 1 as first built).
export const RUN = 'run';
const LEVEL_TAG = {
    debug: 'debug', info: 'info ', warning: 'WARN ', error: 'ERROR',
};
const SOURCE_WIDTH = 12;

const pad2 = (n) => String(n).padStart(2, '0');

/** When a record happened: virtual seconds, or its wall time (hh:mm:ss)
 *  before the system was powered on (a build, a pytest run). */
export function when(rec) {
    if (rec.wall_s !== undefined && rec.wall_s !== null) {
        const d = new Date(rec.wall_s * 1000);
        return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
    }
    return `${(rec.t_us / 1e6).toFixed(3)} s`;
}

/** A log record as a Log entry: { text, source, level }. */
export function logEntry(rec) {
    const source = rec.source || RUN;
    const level = LEVEL_TAG[rec.level] ? rec.level : 'info';
    const text = `  ${when(rec).padStart(9)}  ${source.padEnd(SOURCE_WIDTH)} `
        + `${LEVEL_TAG[level]}  ${rec.text}`;
    return { text, source, level };
}

/** A Log entry's source and level (a plain string is the editor's). */
export const sourceOf = (entry) => (typeof entry === 'string' ? EDITOR : entry?.source || EDITOR);
export const levelOf = (entry) => (typeof entry === 'string' ? 'info' : entry?.level || 'info');
export const textOf = (entry) => (typeof entry === 'string' ? entry : String(entry?.text ?? ''));

/** The sources present, in a stable order: the editor first, then the
 *  run's (worker, build, renode, scenario, …), then each board's UARTs. */
const ORDER = [EDITOR, RUN, 'worker', 'build', 'pytest', 'renode', 'scenario', 'session'];
export function sourcesOf(entries) {
    const seen = new Set();
    entries.forEach((e) => seen.add(sourceOf(e)));
    const rank = (s) => {
        const i = ORDER.indexOf(s);
        return i < 0 ? ORDER.length : i;
    };
    return [...seen].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
}

/** The entries whose source isn't hidden. */
export function shownEntries(entries, hidden) {
    if (!hidden || !hidden.length) return entries;
    const h = new Set(hidden);
    return entries.filter((e) => !h.has(sourceOf(e)));
}

/** hidden with `source` toggled (a new array, sorted). */
export function toggled(hidden, source) {
    const h = new Set(hidden || []);
    if (h.has(source)) h.delete(source);
    else h.add(source);
    return [...h].sort();
}
