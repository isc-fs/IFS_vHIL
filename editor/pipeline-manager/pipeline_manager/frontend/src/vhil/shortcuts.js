/*
 * vHIL: the workspace's keyboard map (step 7 of
 * docs/architecture/editor-workspace.md; CHANGELOG-VHIL.md). `?` lists it.
 * Ctrl is Cmd on a Mac. Browsers keep Ctrl+1..7 for their own tabs in some
 * setups, so Alt+1..7 picks a dock tab too.
 */

import {
    ws, DOCK_TABS, isLive, pauseLive, runNow, stopRun,
} from './workspace.js';

export const SHORTCUTS = [
    ['F5', 'Run the saved system'],
    ['Shift+F5', 'Stop the run (LIVE: the session)'],
    ['Space', 'LIVE: pause or resume the session'],
    ['Ctrl+B', 'Show or hide the sidebar'],
    ['Ctrl+J', 'Show or hide the dock'],
    ...DOCK_TABS.map((t, i) => [`Ctrl+${i + 1} / Alt+${i + 1}`, `Dock: ${t.label}`]),
    ['?', 'This list'],
    ['Esc', 'Close a dialog or the ref picker'],
];

const typing = (el) => el && (el.isContentEditable
    || ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName));

export function onKeyDown(ev) {
    const mod = ev.ctrlKey || ev.metaKey;
    if (ev.key === 'F5') {
        ev.preventDefault();
        if (ev.shiftKey) stopRun(); else runNow();
        return;
    }
    if (mod && !ev.altKey && !ev.shiftKey && (ev.key === 'b' || ev.key === 'B')) {
        ev.preventDefault();
        ws.layout.sidebar = !ws.layout.sidebar;
        return;
    }
    if (mod && !ev.altKey && !ev.shiftKey && (ev.key === 'j' || ev.key === 'J')) {
        ev.preventDefault();
        ws.layout.dock = !ws.layout.dock;
        return;
    }
    const digit = /^Digit([1-7])$/.exec(ev.code);
    if (digit && (mod || ev.altKey) && !ev.shiftKey) {
        ev.preventDefault();
        ws.layout.dockTab = DOCK_TABS[Number(digit[1]) - 1].id;
        ws.layout.dock = true;
        return;
    }
    if (ev.key === 'Escape' && ws.picker) {
        ws.picker = null;
        return;
    }
    if (ev.key === ' ' && !mod && isLive() && !typing(ev.target) && !ws.dialog
        && !ev.target?.closest?.('button, a, [role="switch"]')) {
        ev.preventDefault();
        pauseLive();
        return;
    }
    if (ev.key === '?' && !mod && !typing(ev.target) && !ws.dialog) {
        ev.preventDefault();
        ws.dialog = 'shortcuts';
    }
}
