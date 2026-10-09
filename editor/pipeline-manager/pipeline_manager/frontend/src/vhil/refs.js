/*
 * vHIL: what a board's firmware and bootloader dropdowns list
 * (VhilBoardControls.vue, on the board's node; CHANGELOG-VHIL.md). Pure, so
 * node's test runner takes it (tests/js/refs.test.mjs).
 *
 * A part is a board's app or bootloader as workspace.js boardFirmware()
 * gives it: {what, refProp, kinds, fwId, fw: {id, repo, ref} | null, ref}.
 * A listing is GET /api/firmware/{id}/refs: the repo's active branches
 * (newest first, each with its date, author, PRs and whether its commit is
 * built) and its tags; `every` the same with ?all=1 (every branch).
 * The empty value is the catalogue's ref: a system file keeps no ref then.
 */

import { ageText } from './artifacts.js';

// Option values that are actions, not refs: git refuses ':' in a ref name
// (git check-ref-format), so no branch or tag can be one of them.
export const SHOW_ALL = ':show-all';
export const SHOW_ACTIVE = ':show-active';

const hasBranches = (part) => part.kinds.includes('branches');

/** The listing a part shows: every branch with show all, else the active ones. */
export function listingOf(part, { refs, every, showAll }) {
    return (showAll && hasBranches(part) ? every : null) || refs || null;
}

/** The kinds a part lists: the app its branches (and tags with show all),
 *  the bootloader its tags. */
export function shownKinds(part, showAll) {
    return hasBranches(part) && !showAll ? ['branches'] : part.kinds;
}

/** Every ref either listing names, by name. */
export function byName(part, { refs, every }) {
    return new Map([refs, every]
        .flatMap((l) => part.kinds.flatMap((k) => l?.[k] ?? []))
        .map((r) => [r.name, r]));
}

/** "feat/x · 3 d ago · raul · #12 · not built" (just the name when unknown). */
export function refLabel(name, r, now = Date.now()) {
    if (!r) return name;
    const bits = [name];
    if (r.date) bits.push(ageText(r.date, now));
    if (r.author) bits.push(r.author);
    (r.prs || []).forEach((pr) => bits.push(`#${pr.number}`));
    if (!r.built) bits.push('not built');
    return bits.join(' · ');
}

/**
 * A part's dropdown: [{label, options: [{value, label}]}], the first group
 * unlabelled (the catalogue's ref, and the picked ref when the listing
 * doesn't show it), then the listing's kinds, then the show-all toggle
 * (the app only).
 */
export function refOptions(part, lists, now = Date.now()) {
    const { showAll } = lists;
    const listing = listingOf(part, lists);
    const names = byName(part, lists);
    const kinds = shownKinds(part, showAll);
    const catalogue = part.fw.ref;
    const head = [{ value: '', label: `${refLabel(catalogue, names.get(catalogue), now)} (catalogue)` }];
    const listed = (name) => name === catalogue
        || kinds.some((k) => (listing?.[k] ?? []).some((r) => r.name === name));
    if (part.ref && !listed(part.ref)) {
        let why = 'not found';
        if (!listing) why = 'loading…';
        else if (names.has(part.ref)) why = 'not active';
        head.push({ value: part.ref, label: `${part.ref} (${why})` });
    }
    const groups = [{ label: '', options: head }];
    kinds.forEach((kind) => {
        const options = (listing?.[kind] ?? [])
            .filter((r) => r.name !== catalogue)
            .map((r) => ({ value: r.name, label: refLabel(r.name, r, now) }));
        if (!options.length) return;
        let label = kind;
        if (kind === 'branches' && !showAll && listing?.details === 'github') label = 'active branches';
        groups.push({ label, options });
    });
    if (hasBranches(part) && listing) {
        const more = listing.hidden ? ` (${listing.hidden} more)` : '';
        groups.push({
            label: '',
            options: [showAll
                ? { value: SHOW_ACTIVE, label: 'Show active branches only' }
                : { value: SHOW_ALL, label: `Show all branches and tags${more}…` }],
        });
    }
    return groups;
}

/** The picked ref's (or the catalogue's) entry: commit, date, author, PRs, built. */
export const refDetail = (part, lists) => byName(part, lists).get(part.ref || part.fw.ref) ?? null;

/** The line under the app's dropdown: its commit, age and whether it's built. */
export function detailText(part, lists, now = Date.now()) {
    const d = refDetail(part, lists);
    if (!d) return listingOf(part, lists) ? '' : 'loading refs…';
    return [d.sha ? d.sha.slice(0, 8) : '', d.date ? ageText(d.date, now) : '', d.author || '']
        .filter(Boolean).join(' · ');
}

/** Whether the picked ref's commit is known not to be built yet. */
export const notBuilt = (part, lists) => refDetail(part, lists)?.built === false;

/** What the listing says of itself (no GitHub API: why every branch is listed). */
export function listingNote(part, lists) {
    const listing = listingOf(part, lists);
    if (!listing || !hasBranches(part) || listing.details === 'github') return '';
    return listing.note || 'branch dates and PRs unavailable';
}
