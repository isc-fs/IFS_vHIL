// The state panel's model (src/vhil/state.js) under node's test runner: a
// board's card at a virtual time from samples, edges and frames.
import assert from "node:assert/strict";
import { test } from "node:test";
import { rawValue } from "../../vhil/server/static/decode.js";
import {
  BOOT_WINDOW_US, Series, StateTrace, duration, formatNumber, parseSignal,
} from "../../editor/pipeline-manager/pipeline_manager/frontend/src/vhil/state.js";

// As vhil/server/decode.py system_contract gives it (shortened).
const CONTRACT = {
  buses: {
    can_acu: {
      [0x4A0]: {
        name: "AMS_status", id: 0x4A0, dlc: 8, period_ms: 500, fields: [
          { name: "min_cell_mV", be: true, signed: false, start: 39, length: 16, factor: 1, offset: 0, unit: "mV" }],
      },
      [0x135]: {
        name: "ACU_currents", id: 0x135, dlc: 4, period_ms: 50, fields: [
          { name: "current_accu_dA", be: true, signed: true, start: 7, length: 16, factor: 0.1, offset: 0, unit: "A" }],
      },
    },
  },
  state: {
    ams: {
      firmware: "ams",
      errors: [],
      items: [
        { label: "State", kind: "state", signal: "symbol:ams.g_state", period_ms: 10,
          values: { 0: "Start", 1: "Precharge", 2: "Transition", 3: "Run", 5: "Error" } },
        { label: "AIR+", kind: "relay", signal: "pin:ams.PB5" },
        { label: "PRE", kind: "relay", signal: "pin:ams.PB7" },
        { label: "Fault", kind: "fault", signal: "symbol:ams.g_fault", period_ms: 10,
          values: { 0: "None", 4: "CellUnderVoltage", 12: "FsmError" } },
        { label: "Flag", kind: "fault", signal: "symbol:ams.g_flag", period_ms: 10 },
        { label: "Min cell", kind: "value", signal: "frame:can_acu.AMS_status.min_cell_mV", unit: "mV" },
        { label: "Pack current", kind: "value", signal: "frame:can_acu.ACU_currents.current_accu_dA", unit: "A" },
        { label: "Mode", kind: "value", signal: "symbol:ams.g_mode", period_ms: 50 },
      ],
    },
  },
  labels: { "symbol:ams.g_mode": { 1: "Car" } },
};

const ms = (t) => t * 1000;
const sample = (t, name, value) => ({ kind: "sample", t_us: ms(t), board: "ams", name, value });
const edge = (t, pin, level, initial) => ({ kind: "edge", t_us: ms(t), board: "ams", pin, level, ...(initial ? { initial } : {}) });

// These traces start at the app's start: no bootloader window before it.
function trace() {
  const tr = new StateTrace(CONTRACT, { rawOf: rawValue, bootUs: 0 });
  for (let t = 0; t <= 300; t += 10) {
    tr.add(sample(t, "g_state", t < 100 ? 0 : t < 200 ? 1 : 3));
    tr.add(sample(t, "g_fault", t >= 250 && t < 280 ? 4 : 0));
    tr.add(sample(t, "g_flag", t >= 270 ? 1 : 0));
  }
  for (let t = 0; t <= 300; t += 50) tr.add(sample(t, "g_mode", t >= 100 ? 1 : 0));
  tr.add(edge(0, "PB5", 0, true));
  tr.add(edge(0, "PB7", 0, true));
  tr.add(edge(100, "PB7", 1));
  tr.add(edge(200, "PB5", 1));
  tr.add(edge(200, "PB7", 0));
  // AMS_status at 0x4A0: min cell 3750 mV (BE at bytes 4-5); the
  // scenario's own copy of it doesn't count.
  tr.add({ kind: "frame", t_us: ms(150), bus: "can_acu", id: 0x4A0, data: "000000000ea60000" });
  tr.add({ kind: "frame", t_us: ms(160), bus: "can_acu", id: 0x4A0, data: "0000000000010000", src: "stimulus" });
  tr.add({ kind: "frame", t_us: ms(150), bus: "can_acu", id: 0x135, data: "ff9c0000" });  // -10.0 A
  return tr;
}

test("a series answers the value at a time and its changes", () => {
  const s = new Series();
  [[0, 1], [10, 1], [30, 2], [20, 1], [40, 2]].forEach(([t, v]) => s.push(t, v));
  assert.deepEqual(s.t, [0, 10, 20, 30, 40]);
  assert.equal(s.indexAt(25), 2);
  assert.equal(s.indexAt(-1), -1);
  assert.deepEqual(s.transitions(), [0, 3]);
  assert.equal(s.changeAt(29), 0);
  assert.equal(s.changeAt(30), 1);
});

test("the card at a time: the state, for how long, and the one before", () => {
  const card = trace().cardAt("ams", ms(250));
  assert.equal(card.firmware, "ams");
  assert.equal(card.state.text, "Run");
  assert.equal(card.state.raw, 3);
  assert.equal(card.state.since, ms(200));
  assert.equal(card.state.prev.text, "Precharge");
  assert.deepEqual(card.state.history.map((h) => [h.t, h.text, h.current]),
                   [[0, "Start", false], [ms(100), "Precharge", false], [ms(200), "Run", true]]);
  const early = trace().cardAt("ams", ms(150));
  assert.equal(early.state.text, "Precharge");
  assert.deepEqual(early.state.history.map((h) => h.future), [false, false, true]);
});

test("relays are on or off by their pins, unknown before their first edge", () => {
  const tr = trace();
  assert.deepEqual(tr.cardAt("ams", ms(150)).relays.map((r) => [r.label, r.on]),
                   [["AIR+", false], ["PRE", true]]);
  assert.deepEqual(tr.cardAt("ams", ms(250)).relays.map((r) => [r.label, r.on]),
                   [["AIR+", true], ["PRE", false]]);
  const fresh = new StateTrace(CONTRACT, { rawOf: rawValue, bootUs: 0 });
  assert.deepEqual(fresh.cardAt("ams", 0).relays.map((r) => r.on), [null, null]);
  assert.equal(fresh.cardAt("ams", 0).state.text, "no data");
  assert.ok(fresh.cardAt("ams", 0).stale);
});

test("faults: active with their reason and age, then latched-cleared", () => {
  const tr = trace();
  let card = tr.cardAt("ams", ms(260));
  assert.deepEqual(card.active.map((f) => [f.label, f.reason, f.since]),
                   [["Fault", "CellUnderVoltage", ms(250)]]);
  assert.ok(card.faulted);
  card = tr.cardAt("ams", ms(290));
  // The flag needs no reason; the cleared fault is outlined, with its reason.
  assert.deepEqual(card.active.map((f) => [f.label, f.reason]), [["Flag", ""]]);
  assert.deepEqual(card.cleared.map((f) => [f.label, f.reason, f.since, f.clearedAt]),
                   [["Fault", "CellUnderVoltage", ms(250), ms(280)]]);
  card = tr.cardAt("ams", ms(100));
  assert.equal(card.faulted, false);
  assert.deepEqual([card.active.length, card.cleared.length], [0, 0]);
});

test("key values: decoded fields at their resolution, labels, units", () => {
  const card = trace().cardAt("ams", ms(170));
  const by = Object.fromEntries(card.values.map((v) => [v.label, v]));
  assert.equal(by["Min cell"].text, "3750");            // the stimulus copy ignored
  assert.equal(by["Min cell"].unit, "mV");
  assert.equal(by["Pack current"].text, "-10.0");
  assert.equal(by.Mode.text, "Car");                     // the contract's labels
  assert.equal(by.Mode.unit, "");
  assert.equal(trace().cardAt("ams", ms(100)).values[0].text, "no data");
});

test("a source silent for three periods is stale", () => {
  const tr = trace();
  // AMS_status every 500 ms: fresh 1.4 s after, stale 1.6 s after.
  const at = (t) => tr.cardAt("ams", ms(t)).values.find((v) => v.label === "Min cell").stale;
  assert.equal(at(1550), false);
  assert.equal(at(1700), true);
  // The state is sampled every 10 ms: past 30 ms of silence the card is stale.
  assert.equal(tr.cardAt("ams", ms(320)).stale, false);
  assert.equal(tr.cardAt("ams", ms(340)).stale, true);
});

test("the node pill: the board and its state, with its faults", () => {
  const tr = trace();
  assert.deepEqual(tr.pillAt("ams", ms(150)), { text: "AMS · Precharge", boot: false, faulted: false, faults: [], stale: false });
  assert.deepEqual(tr.pillAt("ams", ms(260)).faults, ["Fault: CellUnderVoltage"]);
  assert.equal(tr.pillAt("nope", 0).text, "NOPE");
});

test("in the bootloader's 2 s window a card says so, not the app's first state", () => {
  // A run from power-on: the app's RAM reads 0 (Start) until its bootloader
  // jumps to it at 2 s (CLAUDE.md invariant 5).
  const tr = new StateTrace(CONTRACT, { rawOf: rawValue });
  for (let t = 0; t <= 2600; t += 10) tr.add(sample(t, "g_state", t < 2400 ? 0 : 1));
  assert.equal(BOOT_WINDOW_US, 2e6);
  assert.equal(tr.cardAt("ams", ms(1500)).boot, true);
  assert.equal(tr.pillAt("ams", ms(1500)).text, "AMS · bootloader");
  assert.equal(tr.cardAt("ams", ms(2100)).boot, false);
  assert.equal(tr.pillAt("ams", ms(2100)).text, "AMS · Start");
  assert.equal(tr.pillAt("ams", ms(2500)).text, "AMS · Precharge");
});

test("an unlabelled state shows its raw value and says it has no enum", () => {
  const contract = { state: { b: { firmware: "x", items: [{ label: "State", kind: "state", signal: "symbol:b.s" }] } } };
  const tr = new StateTrace(contract);
  tr.add({ kind: "sample", t_us: 0, board: "b", name: "s", value: 7 });
  const card = tr.cardAt("b", 0);
  assert.equal(card.state.text, "7");
  assert.ok(card.state.noEnum);
});

test("history lanes: the state's and each relay's segments", () => {
  const lanes = trace().lanesOf("ams", ms(300));
  assert.deepEqual(lanes.map((l) => [l.label, l.kind]), [["State", "state"], ["AIR+", "relay"], ["PRE", "relay"]]);
  assert.deepEqual(lanes[0].segments.map((s) => [s.t0, s.t1, s.text]),
                   [[0, ms(100), "Start"], [ms(100), ms(200), "Precharge"], [ms(200), ms(300), "Run"]]);
  assert.deepEqual(lanes[2].segments.map((s) => [s.t0, s.t1, s.raw]),
                   [[0, ms(100), 0], [ms(100), ms(200), 1], [ms(200), ms(300), 0]]);
});

test("formatting", () => {
  assert.equal(formatNumber(3750), "3750");
  assert.equal(formatNumber(-10, 0.1), "-10.0");
  assert.equal(formatNumber(1.23456, 0.001), "1.235");
  assert.equal(duration(312000), "312 ms");
  assert.equal(duration(4210000), "4.21 s");
  assert.equal(duration(63000000), "1 min 3 s");
  assert.deepEqual(parseSignal("pin:ams.PB5"), { kind: "pin", owner: "ams", item: "PB5", field: "" });
});
