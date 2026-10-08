// The Signals tab's model (src/vhil/signals.js) under node's test runner:
// keys, series from REPLAY's FrameStore, samples and StateTrace lanes.
import assert from "node:assert/strict";
import { test } from "node:test";
import { physical, rawValue } from "../../vhil/server/static/decode.js";
import { FrameStore } from "../../editor/pipeline-manager/pipeline_manager/frontend/src/vhil/frames.js";
import { StateTrace } from "../../editor/pipeline-manager/pipeline_manager/frontend/src/vhil/state.js";
import {
  SIGNAL_LIMIT, addKey, choices, frameKey, labelOf, laneKey, laneKeys, parseKey, sampleKey, seriesOf,
} from "../../editor/pipeline-manager/pipeline_manager/frontend/src/vhil/signals.js";

const CONTRACT = {
  buses: {
    can_acu: {
      256: {
        name: "VCU_heartbeat", id: 256, dlc: 2, period_ms: 10, fields: [
          { name: "mode", be: false, signed: false, start: 0, length: 8, factor: 1, offset: 0, unit: "enum",
            values: { 0: "Off", 2: "Drive" } },
          { name: "volts", be: false, signed: false, start: 8, length: 8, factor: 0.5, offset: 0, unit: "V" }],
      },
    },
  },
  state: {
    ams: {
      firmware: "ams", errors: [], items: [
        { label: "State", kind: "state", signal: "symbol:ams.g_state", period_ms: 10,
          values: { 0: "Start", 1: "Precharge", 3: "Run" } },
        { label: "AIR+", kind: "relay", signal: "pin:ams.PB5" }],
    },
  },
  labels: {},
};

const ms = (t) => t * 1000;

function store() {
  const s = new FrameStore(64);
  s.append([
    { t_us: ms(10), bus: "can_acu", id: 256, ext: false, data: "0010" },
    { t_us: ms(20), bus: "can_acu", id: 256, ext: false, data: "0214" },
    { t_us: ms(20), bus: "can_acu", id: 0x7FF, ext: false, data: "00" },   // not in the contract
    { t_us: ms(30), bus: "can_acu", id: 256, ext: true, data: "0299" },    // extended: another id
  ]);
  return s;
}

function trace() {
  const tr = new StateTrace(CONTRACT, { rawOf: rawValue, bootUs: 0 });
  [[0, 0], [10, 0], [20, 1], [30, 3]].forEach(([t, v]) => tr.add({
    kind: "sample", t_us: ms(t), board: "ams", name: "g_state", value: v }));
  tr.add({ kind: "edge", t_us: 0, board: "ams", pin: "PB5", level: 0, initial: true });
  tr.add({ kind: "edge", t_us: ms(25), board: "ams", pin: "PB5", level: 1 });
  tr.end = ms(40);
  return tr;
}

const SAMPLES = [
  { kind: "sample", t_us: ms(10), board: "ams", name: "g_x", value: 4 },
  { kind: "sample", t_us: ms(20), board: "ams", name: "g_x", value: 5 },
  { kind: "sample", t_us: ms(20), board: "ams", name: "g_y", value: 9 },
];

test("keys name a frame field, a sample or a lane", () => {
  assert.deepEqual(parseKey(frameKey("can_acu", 256, "volts")),
                   { kind: "frame", bus: "can_acu", id: 256, field: "volts" });
  assert.deepEqual(parseKey(sampleKey("ams", "g_x")), { kind: "sample", board: "ams", name: "g_x" });
  assert.deepEqual(parseKey(laneKey("ams", "AIR+")), { kind: "lane", board: "ams", label: "AIR+" });
  assert.equal(parseKey("nonsense"), null);
  assert.equal(labelOf(frameKey("can_acu", 256, "volts"), CONTRACT), "can_acu VCU_heartbeat.volts");
  assert.equal(labelOf(frameKey("can_acu", 0x7FF, "x"), CONTRACT), "can_acu 0x7FF.x");
  assert.equal(labelOf(laneKey("ams", "State"), CONTRACT), "ams · State");
});

test("a field's series is decoded from the store, an enum's as labelled raw values", () => {
  const [volts, mode] = seriesOf([frameKey("can_acu", 256, "volts"), frameKey("can_acu", 256, "mode")], {
    store: store(), contract: CONTRACT, samples: [], stateTrace: null, endUs: ms(40),
    rawOf: rawValue, physOf: physical, stepped: false,
  });
  assert.deepEqual([volts.x, volts.y, volts.unit, volts.stepped], [[0.01, 0.02], [8, 10], "V", false]);
  assert.deepEqual([mode.y, mode.levels, mode.format(2), mode.stepped], [[0, 2], [0, 2], "Drive", true]);
});

test("a sample's series, and lanes held to the end with their labels", () => {
  const tr = trace();
  const [x, state, air] = seriesOf([sampleKey("ams", "g_x"), laneKey("ams", "State"), laneKey("ams", "AIR+")], {
    store: store(), contract: CONTRACT, samples: SAMPLES, stateTrace: tr, endUs: ms(40),
    rawOf: rawValue, physOf: physical,
  });
  assert.deepEqual([x.x, x.y], [[0.01, 0.02], [4, 5]]);
  assert.deepEqual([state.x, state.y], [[0, 0.02, 0.03, 0.04], [0, 1, 3, 3]]);
  assert.deepEqual([state.levels, state.format(1), state.format(7)], [[0, 1, 3], "Precharge", "7"]);
  assert.deepEqual([air.x, air.y, air.kind], [[0, 0.025, 0.04], [0, 1, 1], "relay"]);
});

test("what can be added; the default is every board's lanes", () => {
  const tr = trace();
  assert.deepEqual(laneKeys(tr), ["lane:ams/State", "lane:ams/AIR+"]);
  const c = choices(store(), CONTRACT, SAMPLES, tr);
  assert.deepEqual(c.samples, ["sample:ams.g_x", "sample:ams.g_y"]);
  assert.deepEqual(c.messages.map((m) => [m.id, m.msg.name, m.fields]),
                   [["can_acu/256", "VCU_heartbeat", ["mode", "volts"]]]);
  assert.deepEqual(laneKeys(null), []);
});

test("keys are added once, and at most SIGNAL_LIMIT are kept", () => {
  let keys = [];
  for (let i = 0; i < SIGNAL_LIMIT + 3; i += 1) keys = addKey(keys, sampleKey("ams", `g_${i}`));
  assert.equal(keys.length, SIGNAL_LIMIT);
  assert.equal(keys[0], "sample:ams.g_3");
  assert.equal(addKey(keys, keys[1]), keys);
});
