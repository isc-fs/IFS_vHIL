// The editor's Run (step 5 of docs/architecture/editor-workspace.md): a
// normal run through POST /api/runs, the same queue, worker and history as
// the Runs page. What it asks for and when it refuses are here, with no DOM
// or network, so tests/js/editor_run.test.mjs checks them under node.
//
// A run runs a saved system: the file at a workspace ref, never the graph in
// the browser. So Run refuses while the graph differs from what was opened
// or last saved, and otherwise runs exactly that commit.

export const entryGraph = (dataflow) =>
  dataflow.graphs.find((g) => g.id === dataflow.entryGraph) || dataflow.graphs[0];

// Each board's firmware refs, from its node's firmware_ref and bootloader_ref
// properties, keyed as the run's images are ("<board>", "<board>.bootloader";
// vhil.system System.images). An empty ref is the catalogue's: left out.
export function firmwareRefs(dataflow) {
  const refs = {};
  for (const node of entryGraph(dataflow).nodes) {
    const props = Object.fromEntries((node.properties || []).map((p) => [p.name, p.value]));
    if (!("firmware" in props)) continue;   // a board has a firmware; a bus or a device has none
    const board = node.instanceName || node.id;
    const app = String(props.firmware_ref ?? "").trim();
    const bootloader = String(props.bootloader_ref ?? "").trim();
    if (app) refs[board] = app;
    if (bootloader) refs[`${board}.bootloader`] = bootloader;
  }
  return refs;
}

// The POST /api/runs body: the system at `ref` (none: the workspace's
// checked-out tree), the graph's firmware refs, `virtualMs` of virtual time
// from power-on.
export function runRequest({ system, ref, dataflow, virtualMs }) {
  const body = { system, firmware: firmwareRefs(dataflow),
                 scenario: { kind: "run", virtual_ms: virtualMs } };
  if (ref) body.ref = ref;
  return body;
}

// Why Run won't start, or null. `saved` is the system file the graph gave
// when it was opened or last saved, `current` what it gives now (both from
// POST /api/systems/{id}/preview, so formatting can't tell them apart).
export function runBlocker({ id, isNew, saved, current, virtualMs }) {
  if (!id) return "Open a system first.";
  if (isNew) return `${id} is not saved yet. Save it to a branch; Run then runs it there.`;
  if (saved == null || current !== saved) {
    return `${id} has unsaved edits, and a run runs a saved system. Save them to a branch; ` +
      "Run then runs that commit.";
  }
  if (!Number.isInteger(virtualMs) || virtualMs < 1) return "Virtual time must be a whole number of ms, at least 1.";
  return null;
}

// Frames per bus as one line ("can_acu 1234 · can_inv 56"), for the status.
export const frameCounts = (counts) =>
  Object.entries(counts).map(([bus, n]) => `${bus} ${n}`).join(" · ") || "no frames";
