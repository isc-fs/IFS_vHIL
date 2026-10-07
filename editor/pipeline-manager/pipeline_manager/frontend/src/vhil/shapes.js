/*
 * vHIL: what the node shapes and the wires show (step 8 of
 * docs/architecture/editor-workspace.md; CHANGELOG-VHIL.md). A board is a
 * card with a role band and a mono sub-line ("node 0x2 · FDCAN1"), a CAN bus
 * a thin rail tinted --can-1..4 in system order with its name and bitrate
 * on it, a device a compact dashed card with a count pill. What each reads
 * comes from the specification's additionalData.vhil (vhil/editor.py
 * specification()) and the node's own properties, so a role change
 * relabels the card as it relabels the pins.
 *
 * The wire tooltip ("can_acu · 500 kbit/s · 3 nodes") is one element on the
 * page, placed through the CSSOM: the editor's CSP is style-src 'self'.
 */

import { liveGraph, nodeById, specNode } from './graph.js';

export const ROLE_SEP = ' · ';
const CAN_TINTS = 4;

/** The node type's vHIL data: {kind: board|bus|model, …} or null. */
export const vhilOf = (type) => specNode(type)?.additionalData?.vhil ?? null;

/** A board's role ("ams"), from its role select ("ams (node 0x2)"). */
export function roleOf(node) {
    const value = node?.inputs?.property_role?.value;
    return value ? String(value).split(' ')[0] : null;
}

const hex = (n) => `0x${Number(n).toString(16).toUpperCase()}`;

/** A board's sub-line: its role's node ID and flash bus, else its type. */
export function boardSubline(node) {
    const info = vhilOf(node.type)?.role_info?.[roleOf(node)];
    return info ? `node ${hex(info.node_id)}${ROLE_SEP}${info.flash_bus}` : node.type;
}

/** 500000 -> "500 kbit/s". */
export function bitrateText(bps) {
    if (!bps) return '';
    if (bps >= 1e6) return `${bps / 1e6} Mbit/s`;
    return `${bps / 1e3} kbit/s`;
}

/** The bus nodes on the canvas, in graph (= system) order. */
export function busNodes(graph = liveGraph()) {
    return (graph?.nodes ?? []).filter((n) => vhilOf(n.type)?.kind === 'bus');
}

/** A bus's tint, 1..4 (--can-N): its place among the buses, wrapping. */
export function busTint(node, graph = liveGraph()) {
    const i = busNodes(graph).findIndex((n) => n.id === node.id);
    return i < 0 ? 1 : (i % CAN_TINTS) + 1;
}

/** How many connections land on a bus (each one a node's connector). */
export function busNodeCount(node, graph = liveGraph()) {
    return (graph?.connections ?? [])
        .filter((c) => c.from?.nodeId === node.id || c.to?.nodeId === node.id).length;
}

/** "can_acu · 500 kbit/s · 3 nodes". */
export function busSummary(node, graph = liveGraph()) {
    const n = busNodeCount(node, graph);
    return [node.title || node.type, bitrateText(vhilOf(node.type)?.bitrate),
        `${n} node${n === 1 ? '' : 's'}`].filter(Boolean).join(ROLE_SEP);
}

const firstType = (intf) => {
    const t = intf?.type;
    return (Array.isArray(t) ? t[0] : t) || '';
};

/** A wire's interface type ("can", "spi", …): its ends share it. */
export const wireType = (connection) => firstType(connection.from) || firstType(connection.to);

/** The bus node a wire lands on, if any. */
export function wireBus(connection, graph = liveGraph()) {
    return [connection.from, connection.to]
        .map((intf) => intf && (graph?.findNodeById?.(intf.nodeId) ?? nodeById(intf.nodeId)))
        .find((n) => n && vhilOf(n.type)?.kind === 'bus') ?? null;
}

const pinOf = (name) => String(name ?? '').split(ROLE_SEP)[0];

/** What a wire's tooltip reads: a bus's summary, else its type and ends. */
export function wireText(connection, graph = liveGraph()) {
    const bus = wireBus(connection, graph);
    if (bus) return busSummary(bus, graph);
    const end = (intf) => {
        const n = intf && (graph?.findNodeById?.(intf.nodeId) ?? nodeById(intf.nodeId));
        return n ? `${n.title || n.type}.${pinOf(intf.name)}` : '?';
    };
    const type = wireType(connection);
    return `${type ? type.toUpperCase() : 'wire'}${ROLE_SEP}${end(connection.from)} ↔ ${end(connection.to)}`;
}

// -- the tooltip ---------------------------------------------------------------

let tip = null;

/** Shows the wire tooltip next to the pointer. */
export function showWireTip(text, ev) {
    if (!tip) {
        tip = document.createElement('div');
        tip.className = 'vhil-wire-tip';
        tip.setAttribute('role', 'tooltip');
        document.body.appendChild(tip);
    }
    if (tip.textContent !== text) tip.textContent = text;
    // Below and right of the pointer, kept on the page.
    const x = Math.min(ev.clientX + 12, window.innerWidth - tip.offsetWidth - 8);
    const y = Math.min(ev.clientY + 16, window.innerHeight - tip.offsetHeight - 8);
    tip.style.transform = `translate(${Math.max(0, x)}px, ${Math.max(0, y)}px)`;
    tip.classList.add('--shown');
}

export function hideWireTip() {
    tip?.classList.remove('--shown');
}
