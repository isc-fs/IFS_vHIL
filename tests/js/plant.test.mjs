// A system's plants in the editor (src/vhil/plant.js) under node's test
// runner: the state card from `plant` records, what a plant may be set to
// (the contract's `plants`), and the op or row that sets it.
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  PlantTrace, defaultSet, firstValue, keyChoices, paramType, parseSetting, plantOp, plantsOf,
  plantValueText, renameKey, setText, settableOf, settingsOf,
} from "../../editor/pipeline-manager/pipeline_manager/frontend/src/vhil/plant.js";

// As vhil/server/decode.py plants gives it for ecu-ams (shortened).
const CONTRACT = {
  plants: {
    ts: {
      plant: "tractive-system",
      settable: {
        faults: [
          { name: "air_external_open", type: "bool", default: false },
          { name: "c_scale", type: "number", default: 1.0 },
          { name: "pre_open_circuit", type: "bool", default: false },
          { name: "r_leak", type: "number", default: null },
        ],
        inputs: [],
        params: {
          below_floor: { value: "hold", unit: null, status: "uncommissioned" },
          c_link: { value: 400e-6, unit: "F", status: "cited" },
          cells_per_chip: { value: [9, 10], unit: "cells", status: "cited" },
          r_inverter_bleed: { value: null, unit: "ohm", status: "uncommissioned" },
        },
      },
    },
  },
};

const rec = (t, name, value, plant = "ts") => ({ kind: "plant", t_us: t, plant, name, value });

test("a plant's card at a time: its last value of each key", () => {
  const tr = new PlantTrace();
  assert.equal(tr.add({ kind: "sample", t_us: 1, board: "ams", name: "x", value: 1 }), false);
  [rec(100_000, "v_link", 0.0), rec(100_000, "v_pack", 351.5), rec(100_000, "air_pos_closed", 0),
    rec(100_000, "sdc_closed", 1), rec(100_000, "discharge", 0), rec(100_000, "i_pack", 0),
    rec(5_500_000, "v_link", 334.2), rec(5_600_000, "air_pos_close_ratio", 0.9532),
    rec(5_600_000, "air_pos_closed", 1)].forEach((r) => assert.ok(tr.add(r)));
  assert.deepEqual(tr.plants(), ["ts"]);
  assert.equal(tr.end, 5_600_000);
  const early = tr.cardAt("ts", 200_000);
  assert.ok(early.has);
  assert.deepEqual(early.values.map((v) => [v.label, v.text, v.has]),
                   [["V_link", "0.0", true], ["V_pack", "351.5", true], ["i_pack", "0", true]]);
  const pills = Object.fromEntries(early.pills.map((p) => [p.label, [p.on, p.word]]));
  assert.deepEqual(pills["AIR+"], [false, "open"]);
  assert.equal(pills["AIR−"], undefined, "a key the plant never records is not shown");
  assert.deepEqual(pills["SDC (coil supply)"], [true, "closed"]);
  assert.deepEqual(pills.Discharge, [false, "off"]);
  assert.deepEqual(early.extra, []);
  const late = tr.cardAt("ts", 6_000_000);
  assert.equal(late.values[0].text, "334.2");
  assert.equal(late.pills[0].on, true);
  // A key the card doesn't know is shown as name=value.
  assert.deepEqual(late.extra, [{ key: "air_pos_close_ratio", text: "air_pos_close_ratio=0.9532" }]);
  assert.equal(tr.cardAt("ts", 0).has, false);
  assert.equal(tr.cardAt("other", 6_000_000).has, false);
});

test("the shutdown circuit's card shows its own keys, not the link's", () => {
  const tr = new PlantTrace();
  [rec(0, "sdc_closed", 0, "sdc"), rec(0, "imd_latch", 0, "sdc"), rec(0, "ams_latch", 0, "sdc"),
    rec(100_000, "sdc_estop", 1, "sdc"), rec(5_400_000, "ams_latch", 1, "sdc")]
    .forEach((r) => tr.add(r));
  const card = tr.cardAt("sdc", 6_000_000);
  assert.deepEqual(card.values, []);
  assert.deepEqual(card.pills.map((p) => [p.label, p.word]),
                   [["SDC (coil supply)", "open"], ["IMD latch (K4)", "open"], ["AMS latch (K5)", "closed"]]);
  assert.deepEqual(card.extra, [{ key: "sdc_estop", text: "sdc_estop=1" }]);
});

test("the contract's plants as the controls list them", () => {
  const [ts] = plantsOf(CONTRACT);
  assert.equal(ts.name, "ts");
  assert.equal(ts.plant, "tractive-system");
  assert.deepEqual(ts.faults.map((e) => [e.name, e.type, e.value, e.nullable]), [
    ["air_external_open", "bool", false, false], ["c_scale", "number", 1.0, false],
    ["pre_open_circuit", "bool", false, false], ["r_leak", "number", null, true]]);
  const params = Object.fromEntries(ts.params.map((e) => [e.name, e]));
  assert.equal(params.below_floor.type, "text");
  assert.deepEqual([params.c_link.type, params.c_link.unit, params.c_link.status], ["number", "F", "cited"]);
  assert.equal(params.cells_per_chip.type, "fixed");
  assert.deepEqual([params.r_inverter_bleed.type, params.r_inverter_bleed.nullable], ["number", true]);
  assert.equal(settingsOf(CONTRACT, "ts").length, 8);
  assert.deepEqual(settableOf(CONTRACT, "ts").map((e) => e.name).includes("cells_per_chip"), false);
  assert.deepEqual(plantsOf({}), []);
  assert.deepEqual(settingsOf(CONTRACT, "nope"), []);
  assert.equal(paramType({ value: true }), "bool");
});

test("a new row sets the first fault on; a value parses by its type", () => {
  assert.deepEqual(defaultSet(CONTRACT, "ts"), { air_external_open: true });
  assert.deepEqual(defaultSet(CONTRACT, "nope"), {});
  const [, cScale, , rLeak] = plantsOf(CONTRACT)[0].faults;
  assert.equal(firstValue(cScale), 1.0);
  assert.equal(parseSetting(cScale, "10"), 10);
  assert.equal(parseSetting(cScale, ""), undefined);           // not nullable
  assert.equal(parseSetting(rLeak, ""), null);                 // none
  assert.equal(parseSetting(rLeak, "abc"), undefined);
  assert.equal(parseSetting({ type: "bool" }, "true"), true);
  assert.equal(parseSetting({ type: "text" }, "silent"), "silent");
  assert.equal(parseSetting({ type: "text" }, "a;b"), undefined);
});

test("the op, a set's text, and a row's key choices", () => {
  assert.deepEqual(plantOp("ts", "pre_open_circuit", true),
                   { kind: "plant", plant: "ts", set: { pre_open_circuit: true } });
  assert.equal(setText({ pre_open_circuit: true, r_leak: null }), "pre_open_circuit = true, r_leak = none");
  assert.equal(plantValueText(undefined), "–");
  const names = ["c_scale", "r_leak", "sense_floor"];
  assert.deepEqual(keyChoices(names, { c_scale: 2, r_leak: 1 }, "r_leak"), ["r_leak", "sense_floor"]);
  assert.deepEqual(keyChoices(names, { warp: 1 }, "warp"), ["warp", "c_scale", "r_leak", "sense_floor"]);
  assert.deepEqual(Object.keys(renameKey({ a: 1, b: 2, c: 3 }, "b", "x", 9)), ["a", "x", "c"]);
});
