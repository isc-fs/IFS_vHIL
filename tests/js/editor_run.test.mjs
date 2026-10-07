// static/editor-run.js under node's test runner (tests/unit/test_inspect.py
// runs every tests/js/*.test.mjs): the editor's Run request and its refusals.
// tests/unit/test_editor_run.py posts what it builds from real systems.
import assert from "node:assert/strict";
import { test } from "node:test";
import { firmwareRefs, frameCounts, runBlocker, runRequest } from "../../vhil/server/static/editor-run.js";

const prop = (name, value) => ({ id: `p:${name}`, name, value });
const board = (name, props) => ({ id: `n:${name}`, name: "mainlite", instanceName: name,
  properties: Object.entries(props).map(([k, v]) => prop(k, v)), interfaces: [] });
const GRAPH = {
  version: "20250623.14", entryGraph: "system",
  graphs: [{ id: "other", nodes: [board("x", { firmware: "ecu", firmware_ref: "nope" })] },
           { id: "system", nodes: [
             board("ecu", { role: "ecu (node 0x1)", firmware: "ecu", firmware_ref: " feat/x ",
                            bootloader: "can-bootloader", bootloader_ref: "v1.6.2" }),
             board("ams", { role: "ams (node 0x2)", firmware: "ams", firmware_ref: "",
                            bootloader: "can-bootloader", bootloader_ref: "" }),
             { id: "n:can_acu", name: "CAN bus", instanceName: "can_acu",
               properties: [prop("host_netdev", "")], interfaces: [] },
             { id: "n:isospi", name: "ltc6820", instanceName: "isospi",
               properties: [prop("firmware_ref", "not a board")], interfaces: [] }] }],
};

test("firmware refs come from each board's node, keyed as the run's images", () => {
  assert.deepEqual(firmwareRefs(GRAPH), { ecu: "feat/x", "ecu.bootloader": "v1.6.2" });
});

test("an empty ref is the catalogue's: not sent", () => {
  assert.equal("ams" in firmwareRefs(GRAPH), false);
  assert.equal("ams.bootloader" in firmwareRefs(GRAPH), false);
});

test("the request is a normal run scenario, at the ref when there is one", () => {
  assert.deepEqual(runRequest({ system: "ecu-ams", ref: "0123abcd", dataflow: GRAPH, virtualMs: 3000 }), {
    system: "ecu-ams", ref: "0123abcd", firmware: { ecu: "feat/x", "ecu.bootloader": "v1.6.2" },
    scenario: { kind: "run", virtual_ms: 3000 } });
  const tree = runRequest({ system: "ecu-ams", ref: "", dataflow: GRAPH, virtualMs: 2500 });
  assert.equal("ref" in tree, false);
  assert.equal(tree.scenario.virtual_ms, 2500);
});

test("Run refuses what it can't run as saved", () => {
  const ok = { id: "ams", isNew: false, saved: "a: 1\n", current: "a: 1\n", virtualMs: 3000 };
  assert.equal(runBlocker(ok), null);
  assert.match(runBlocker({ ...ok, id: null }), /Open a system/);
  assert.match(runBlocker({ ...ok, isNew: true }), /not saved yet/);
  assert.match(runBlocker({ ...ok, current: "a: 2\n" }), /unsaved edits/);
  assert.match(runBlocker({ ...ok, saved: null }), /unsaved edits/);
  assert.match(runBlocker({ ...ok, virtualMs: 0 }), /Virtual time/);
  assert.match(runBlocker({ ...ok, virtualMs: 1.5 }), /Virtual time/);
});

test("frame counts read as one line", () => {
  assert.equal(frameCounts({}), "no frames");
  assert.equal(frameCounts({ can_acu: 12, can_inv: 3 }), "can_acu 12 · can_inv 3");
});
