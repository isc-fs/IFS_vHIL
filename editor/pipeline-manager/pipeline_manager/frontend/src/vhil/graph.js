/*
 * vHIL: the graph on the canvas, as the workspace reads and edits it
 * (CHANGELOG-VHIL.md). The system file stays the source of truth: the graph
 * is translated to and from it by the API (vhil/editor.py), never kept here.
 *
 * Nodes are Pipeline Manager's own (baklava) nodes: a property is the input
 * `property_<name>`, and setting its value is what the node's own control
 * does (the node then tells the backend, which relabels a board whose role
 * changed: vhil/editor.py relabel()).
 */

import EditorManager from '../core/EditorManager.js';
import getExternalApplicationManager from '../core/communication/ExternalApplicationManager.js';

const editorManager = () => EditorManager.getEditorManagerInstance();

export const entryGraph = (dataflow) =>
    dataflow.graphs.find((g) => g.id === dataflow.entryGraph) || dataflow.graphs[0];

/** The graph as Pipeline Manager saves it (its dataflow format). */
export const saveDataflow = () => editorManager().saveDataflow();

/** The graph on the canvas (baklava's), and its nodes. */
export const liveGraph = () => editorManager().baklavaView.displayedGraph;
export const liveNodes = () => [...(liveGraph()?.nodes ?? [])];
export const nodeById = (id) => liveNodes().find((n) => n.id === id);
export const nodeName = (node) => node.title || node.type;

/** A node type's specification (vhil/editor.py specification()). */
export function specNode(type) {
    return editorManager().specification.currentSpecification?.nodes?.find((n) => n.name === type);
}

/** board, bus or model: the node type's additionalData.vhil.kind. */
export const vhilKind = (type) => specNode(type)?.additionalData?.vhil?.kind;

export const prop = (node, name) => node?.inputs?.[`property_${name}`];
export const propValue = (node, name) => prop(node, name)?.value ?? '';

/** Sets a property as its control on the node would. */
export function setProp(node, name, value) {
    const intf = prop(node, name);
    if (!intf) throw new Error(`${nodeName(node)} has no property ${name}`);
    intf.value = value;
}

/** The boards on the canvas: the nodes with a firmware property. */
export const boards = () => liveNodes().filter((n) => prop(n, 'firmware'));

/**
 * What the system file is made of, without the view (positions, panning):
 * nodes with their properties and interfaces, and the connections. Two equal
 * signatures give the same file, so it tells whether the graph has unsaved
 * edits without asking the server.
 */
export function signature(dataflow) {
    const g = entryGraph(dataflow);
    const nodes = (g.nodes || []).map((n) => [
        n.id, n.name, n.instanceName ?? '',
        (n.properties || []).map((p) => [p.name, p.value]).sort(),
        (n.interfaces || []).map((i) => [i.id, i.name]).sort(),
        n.enabledInterfaceGroups ?? [],
    ]).sort();
    const connections = (g.connections || []).map((c) => [c.from, c.to]).sort();
    return JSON.stringify([nodes, connections]);
}

const sleep = (ms) => new Promise((r) => { setTimeout(r, ms); });

/**
 * Loads a dataflow onto the canvas once the specification has come from the
 * backend (before that every node is unknown), and checks it took every node.
 */
export async function loadGraph(dataflow, { timeoutMs = 30000 } = {}) {
    const manager = editorManager();
    const deadline = Date.now() + timeoutMs;
    while (!(manager.isSpecificationLoaded()
             && manager.specification.currentSpecification?.nodes?.length)) {
        if (Date.now() > deadline) {
            throw new Error('no specification from the editor backend yet: is it running?');
        }
        await sleep(250); // eslint-disable-line no-await-in-loop
    }
    await getExternalApplicationManager().updateDataflow(dataflow);
    const want = entryGraph(dataflow).nodes.length;
    const got = liveNodes().length;
    if (got !== want) throw new Error(`the editor took ${got} of ${want} nodes`);
    manager.baklavaView.editor.centerZoom();
}

/** Locks the topology (LIVE: the session runs the saved system, so nodes
 *  and wires can't be added, removed or rewired), or unlocks it: Pipeline
 *  Manager's own read-only mode. */
export function setLocked(on) {
    editorManager().baklavaView.editor.readonly = Boolean(on);
}

/** Selects one node (or none) on the canvas. */
export function selectNode(node) {
    const graph = liveGraph();
    graph.selectedNodes = node ? [node] : [];
}

/** Pans the canvas so a node is in the middle of `el` (the canvas element). */
export function centerOn(node, el) {
    const graph = liveGraph();
    if (!graph || !node || !el) return;
    const { width, height } = el.getBoundingClientRect();
    const s = graph.scaling || 1;
    const w = node.width || 300;
    graph.panning.x = width / (2 * s) - (node.position.x + w / 2);
    graph.panning.y = height / (2 * s) - (node.position.y + 150);
}

/**
 * The node a validation message is about: the one whose name appears in it
 * as a word (`ams.PB9`, "board ams"), the longest name if several do.
 */
export function nodeOfMessage(text) {
    const words = (n) => new RegExp(`(^|[^\\w-])${n.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}([^\\w-]|$)`);
    return liveNodes()
        .filter((n) => n.title && words(n.title).test(text))
        .sort((a, b) => b.title.length - a.title.length)[0] ?? null;
}
