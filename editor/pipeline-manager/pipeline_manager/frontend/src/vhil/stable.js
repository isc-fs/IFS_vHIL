/**
 * Native <select>s that keep a pick (CHANGELOG-VHIL.md). Chrome and Safari
 * on macOS drop the pick of an open select's popup when its options are
 * re-patched, and Vue re-patches every `:value` on each re-render of the
 * component (it always patches `value`, and since 3.5 also sets the
 * attribute), whether or not it changed. So: computeds that hand a select
 * its data keep their old value when the new one is the same (`keep`), and
 * a select inside a v-for (where v-memo can't go) takes its value through
 * `vPick`, which writes the DOM only when the value differs. Pure, so
 * tests/js/stable.test.mjs runs it under node.
 */

/** Deep equality of plain data (what the graph and the API give). */
export function same(a, b) {
    if (Object.is(a, b)) return true;
    if (typeof a !== 'object' || typeof b !== 'object' || a === null || b === null) return false;
    if (Array.isArray(a) !== Array.isArray(b)) return false;
    const ka = Object.keys(a);
    const kb = Object.keys(b);
    return ka.length === kb.length && ka.every((k) => Object.hasOwn(b, k) && same(a[k], b[k]));
}

/**
 * For `computed((old) => keep(old, next))`: the old value when the new one
 * is the same, so the computed doesn't change and its readers don't
 * re-render.
 */
export function keep(old, next) {
    return old !== undefined && same(old, next) ? old : next;
}

const text = (v) => (v == null ? '' : String(v));

/** `v-pick="value"` on a <select>, in place of `:value`: the DOM is
 *  written only when the value differs (after the options are patched). */
export const vPick = {
    mounted(el, { value }) {
        el.value = text(value); // eslint-disable-line no-param-reassign
    },
    updated(el, { value }) {
        if (el.value !== text(value)) el.value = text(value); // eslint-disable-line no-param-reassign
    },
};
