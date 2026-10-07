// static/decode.js under node's test runner (tests/unit/test_inspect.py runs it).
// The contract entries are candef's to_json() of fixtures/candef .def files.
import assert from "node:assert/strict";
import { test } from "node:test";
import { bytesToHex, decodeFrame, encodeField, fieldRange, filterFrames, formatValue, hexId,
         hexToBytes, lookup, lowerBound, parseIdFilter, rawFromPhysical, rawValue,
         signalSeries } from "../../vhil/server/static/decode.js";

const F = (name, be, signed, start, length, factor = 1, offset = 0, unit = "", values) =>
  ({ name, be, signed, start, length, factor, offset, unit, ...(values ? { values } : {}) });

// IFS08-CE-ECU pit_diag_status.def (0x700)
const STATUS = { name: "PitDiag_status", id: 0x700, ext: false, dlc: 8, sender: "VCU", period_ms: 100,
  fields: [F("fsm_state", false, false, 0, 8, 1, 0, "enum", { 5: "Active" }),
           F("inv_state", false, false, 8, 8), F("t11_8_9", false, false, 17, 1, 1, 0, "bit"),
           F("ok_precharge", false, false, 19, 1, 1, 0, "bit"), F("torque_pct", false, false, 24, 8, 1, 0, "%"),
           F("v_cell_min_mV", true, false, 39, 16, 1, 0, "mV"), F("torque_cmd", true, true, 55, 16, 1, 0, "cmd")] };
// IFS08-CE-AMS acu_currents.def (0x135)
const CURRENTS = { name: "ACU_currents", id: 0x135, ext: false, dlc: 4, sender: "AMS", period_ms: 50,
  fields: [F("current_accu_dA", true, true, 7, 16, 0.1, 0, "A"), F("current_dcdc_dA", true, true, 23, 16, 0.1, 0, "A")] };
const CONTRACT = { buses: { can_acu: { [0x700]: STATUS, [0x135]: CURRENTS } } };

test("decodes LE, BE, signed and bit fields like the firmware", () => {
  const d = Object.fromEntries(decodeFrame(STATUS, hexToBytes("05040a320e10ff33")).map((x) => [x.name, x]));
  assert.equal(d.fsm_state.value, 5);
  assert.equal(d.fsm_state.label, "Active");
  assert.equal(d.inv_state.value, 4);
  assert.equal(d.t11_8_9.value, 1);
  assert.equal(d.ok_precharge.value, 1);
  assert.equal(d.torque_pct.value, 50);
  assert.equal(d.v_cell_min_mV.value, 3600);
  assert.equal(d.torque_cmd.value, -205);
});

test("factor and offset give the physical value, rounded to its precision", () => {
  const d = decodeFrame(CURRENTS, hexToBytes("ff9c0032"));
  assert.deepEqual(d.map((x) => x.value), [-10, 5]);
  assert.equal(decodeFrame(CURRENTS, hexToBytes("0003"))[0].value, 0.3);
  assert.equal(formatValue(d[0]), "-10 A");
});

test("a field past the frame's end is null, not garbage", () => {
  const d = decodeFrame(CURRENTS, hexToBytes("0001"));
  assert.equal(d[0].value, 0.1);
  assert.equal(d[1].value, null);
  assert.equal(formatValue(d[1]), "–");
});

test("wide fields stay exact", () => {
  const f = F("big", false, false, 0, 64);
  assert.equal(rawValue(hexToBytes("0100000000000000"), f), 1);
  const s = F("s32", false, true, 0, 32);
  assert.equal(rawValue(hexToBytes("401d1718"), s), 404168000);
  assert.equal(rawValue(hexToBytes("50d8cafd"), s), -37038000);
});

test("lookup keys on bus and id and keeps standard and extended apart", () => {
  assert.equal(lookup(CONTRACT, { bus: "can_acu", id: 0x700, ext: false }), STATUS);
  assert.equal(lookup(CONTRACT, { bus: "can_acu", id: 0x700, ext: true }), undefined);
  assert.equal(lookup(CONTRACT, { bus: "can_inv", id: 0x700, ext: false }), undefined);
  assert.equal(hexId(0x7e0, false), "0x7E0");
  assert.equal(hexId(0x1abc, true), "0x00001ABC");
});

test("id filter: hex ids, ranges and names", () => {
  const p = parseIdFilter("0x100, 700-70D 4a0 status");
  assert.ok(p(0x100) && p(0x700) && p(0x70d) && p(0x4a0));
  assert.ok(!p(0x70e) && !p(0x101));
  assert.ok(p(0x135, "AMS_status"));      // by name, case-insensitive
  assert.ok(!p(0x135, "ACU_currents"));
  assert.ok(parseIdFilter("")(0x123));
  assert.ok(parseIdFilter("0xabc")(0xabc));
  assert.ok(parseIdFilter("abc")(1, "xABCx"));   // no digit, no 0x: a name
  assert.throws(() => parseIdFilter("70D-700"), /empty range/);
  assert.throws(() => parseIdFilter("a+b"), /not an id/);
});

const FRAMES = [
  { t_us: 1000, bus: "can_acu", id: 0x700, ext: false, data: "0500000000000000" },
  { t_us: 2000, bus: "can_inv", id: 0x100, ext: false, data: "00" },
  { t_us: 3000, bus: "can_acu", id: 0x135, ext: false, data: "ff9c0032" },
  { t_us: 4000, bus: "can_acu", id: 0x700, ext: false, data: "0600000000000000" },
];

test("frame filters: bus, ids, time window", () => {
  assert.deepEqual(filterFrames(FRAMES, CONTRACT, {}), [0, 1, 2, 3]);
  assert.deepEqual(filterFrames(FRAMES, CONTRACT, { bus: "can_acu" }), [0, 2, 3]);
  assert.deepEqual(filterFrames(FRAMES, CONTRACT, { ids: parseIdFilter("currents") }), [2]);
  assert.deepEqual(filterFrames(FRAMES, CONTRACT, { fromUs: 2000, toUs: 3000 }), [1, 2]);
});

test("a decoded signal is a time series in seconds", () => {
  assert.deepEqual(signalSeries(FRAMES, CONTRACT, "can_acu/1792/fsm_state"), { x: [0.001, 0.004], y: [5, 6] });
  assert.deepEqual(signalSeries(FRAMES, CONTRACT, "can_acu/1792/nope"), { x: [], y: [] });
});

test("lowerBound finds the first record at or after a time", () => {
  assert.equal(lowerBound(FRAMES, 0), 0);
  assert.equal(lowerBound(FRAMES, 2000), 1);
  assert.equal(lowerBound(FRAMES, 2001), 2);
  assert.equal(lowerBound(FRAMES, 9999), 4);
  assert.equal(lowerBound([0, 2, 3], 2500, FRAMES), 1);   // over filtered indices
});

test("encoding a field is the inverse of decoding it, LE, BE, signed and bits", () => {
  const frame = hexToBytes("05040a320e10ff33");
  for (const msg of [STATUS, CURRENTS]) {
    for (const f of msg.fields) {
      const raw = rawValue(frame, f);
      if (raw === null) continue;
      const again = encodeField(new Uint8Array(frame.length), f, raw);
      assert.equal(rawValue(again, f), raw, f.name);
      // Only the field's own bits are touched.
      assert.equal(bytesToHex(encodeField(frame, f, raw)), "05040a320e10ff33", f.name);
    }
  }
  const torque = STATUS.fields.find((f) => f.name === "torque_cmd");
  assert.equal(rawValue(encodeField(new Uint8Array(8), torque, -205), torque), -205);
});

test("physical values encode rounded and clamped to the field", () => {
  const amps = CURRENTS.fields[0];                       // 0.1 A, signed 16 bits
  assert.equal(rawFromPhysical(-12.34, amps), -123);
  assert.equal(rawFromPhysical(1e9, amps), 32767);
  assert.deepEqual(fieldRange(amps), [-3276.8, 3276.7]);
  const bit = STATUS.fields.find((f) => f.name === "ok_precharge");
  assert.deepEqual(fieldRange(bit), [0, 1]);
  assert.equal(rawFromPhysical(2, bit), 1);
  assert.equal(rawFromPhysical("x", bit), 0);
});

test("a field past the frame's end extends it", () => {
  const cmd = STATUS.fields.find((f) => f.name === "torque_cmd");
  const out = encodeField(new Uint8Array(2), cmd, 1);
  assert.equal(out.length, 8);
  assert.equal(rawValue(out, cmd), 1);
});
