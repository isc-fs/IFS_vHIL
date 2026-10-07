// The Scenario tab's model (src/vhil/scenario.js) under node's test runner:
// rows from a scenario's lists, editing them in place, and what each shows.
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  addRow, emptyScenario, expectText, findMessage, looseSignal, messagesByRow, moveItem, parseSignal,
  periodicNames, removeRow, resultsByRow, rowsOf, signalText, snap, targetOf, valueOf,
} from "../../editor/pipeline-manager/pipeline_manager/frontend/src/vhil/scenario.js";

const CONTRACT = { buses: { can_acu: { 256: { name: "VCU_heartbeat", id: 256, ext: false, dlc: 3, fields: [] },
                                       1184: { name: "AMS_status", id: 1184, ext: false, dlc: 8, fields: [] } } } };

const doc = () => ({
  ...emptyScenario(6000),
  stimuli: [
    { kind: "gpio", at_ms: 5000, board: "ams", pin: "PF9", level: true },
    { kind: "can_periodic", name: "vcu", at_ms: 2500, bus: "can_acu", id: 0x100, data: "000002", period_ms: 10 },
    { kind: "stop_periodic", at_ms: 5300, periodic: "vcu" },
  ],
  watch: [{ kind: "symbol", board: "ams", name: "g_state_telemetry", size: 1, period_ms: 10 }],
  expect: [{ check: "eventually", at_ms: 5000, until_ms: 5600, signal: "frame:can_acu.AMS_status.fsm_state",
             op: "==", value: "Precharge" }],
});

test("rows are every item, watches first, then by time", () => {
  const rows = rowsOf(doc());
  assert.deepEqual(rows.map((r) => [r.key, r.action, r.t]), [
    ["watch[0]", "watch", null], ["stimuli[1]", "periodic", 2500], ["stimuli[0]", "gpio", 5000],
    ["expect[0]", "expect", 5000], ["stimuli[2]", "stop", 5300]]);
});

test("a row shows its target and its value, decoded names from the contract", () => {
  const rows = Object.fromEntries(rowsOf(doc()).map((r) => [r.key, r]));
  assert.equal(targetOf(rows["stimuli[1]"], CONTRACT), "can_acu 0x100 VCU_heartbeat");
  assert.equal(valueOf(rows["stimuli[1]"]), "[00 00 02] every 10 ms · vcu");
  assert.equal(targetOf(rows["stimuli[0]"], CONTRACT), "ams.PF9");
  assert.equal(valueOf(rows["stimuli[0]"]), "HIGH");
  assert.equal(valueOf(rows["watch[0]"]), "1 B every 10 ms");
  assert.equal(valueOf(rows["expect[0]"]), "eventually == Precharge by 5600 ms");
  assert.equal(expectText({ check: "period", max_ms: 12, min_ms: 8 }), "period ≥ 8 ms, ≤ 12 ms");
  assert.equal(expectText({ check: "count", max: 0, until_ms: 9 }), "count ≤ 0 by 9 ms");
});

test("adding and removing rows edits the lists; a periodic's stop goes with it", () => {
  const d = doc();
  assert.equal(addRow(d, "expect", { t: 12.4, boards: ["ams"] }), "expect[1]");
  assert.deepEqual(d.expect[1], { check: "eventually", at_ms: 12, signal: "pin:ams.", op: "==", value: 1 });
  assert.equal(addRow(d, "periodic", { buses: ["can_acu"], periodics: periodicNames(d) }), "stimuli[3]");
  assert.equal(d.stimuli[3].name, "p1");
  removeRow(d, "stimuli[1]");
  assert.deepEqual(d.stimuli.map((s) => s.kind), ["gpio", "can_periodic"]);
});

test("moving an item moves its window", () => {
  const e = { at_ms: 100, until_ms: 300 };
  moveItem(e, 150);
  assert.deepEqual(e, { at_ms: 150, until_ms: 350 });
  moveItem(e, -5);
  assert.deepEqual(e, { at_ms: 0, until_ms: 200 });
  assert.equal(snap(12.6, 5), 15);
  assert.equal(snap(-3, 1), 0);
});

test("signals parse and print as the server's grammar", () => {
  assert.deepEqual(parseSignal("frame:can_acu.0x4A0.fsm_state"),
                   { kind: "frame", owner: "can_acu", item: "0x4A0", field: "fsm_state" });
  assert.equal(parseSignal("pin:ams.PB5.x"), null);
  assert.equal(parseSignal("nope"), null);
  // One being built keeps its source and owner.
  assert.deepEqual(looseSignal("pin:ams."), { kind: "pin", owner: "ams", item: "", field: "" });
  assert.deepEqual(looseSignal("frame:can_acu.AMS_status."),
                   { kind: "frame", owner: "can_acu", item: "AMS_status", field: "" });
  assert.equal(looseSignal("wire:x"), null);
  assert.equal(signalText({ kind: "pin", owner: "ams", item: "PB5", field: "" }), "pin:ams.PB5");
  assert.equal(findMessage(CONTRACT, "can_acu", "0x4A0").name, "AMS_status");
  assert.equal(findMessage(CONTRACT, "can_acu", "VCU_heartbeat").id, 256);
});

test("the server's messages and a run's results land on their rows", () => {
  const by = messagesByRow(["stimuli[2]: no bus 'x'", "stimuli[2].bus: bad", "expect[0]: nope", "kind: wrong"]);
  assert.deepEqual([...by.keys()], ["stimuli[2]", "expect[0]", ""]);
  assert.equal(by.get("stimuli[2]").length, 2);
  const results = resultsByRow({ expects: [{ index: 1, passed: false }] });
  assert.equal(results.get("expect[1]").passed, false);
});
