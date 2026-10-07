// A live session's model (src/vhil/live.js) under node's test runner: what
// a session's op records leave in force, the Send panel's ops, bus rates.
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  BusRates, SessionState, clockText, idleText, inputsOf, opText, periodicName, recordedDoc,
  sendOp, sendProblem,
} from "../../editor/pipeline-manager/pipeline_manager/frontend/src/vhil/live.js";

const VCU = { kind: "can_periodic", name: "vcu", bus: "can_acu", id: 0x100, data: "000002", period_ms: 10 };
const op = (t, o, status = "applied") => ({ kind: "op", t_us: t, op_id: t, op: o, status });

test("the op records fold into what is in force", () => {
  const s = new SessionState();
  s.add(op(2_450_000, VCU));
  s.add(op(2_500_000, { kind: "gpio", board: "ams", pin: "PF9", level: true }));
  s.add(op(2_550_000, { kind: "analog", board: "ams", pin: "PF7", volts: 1.65 }));
  s.add(op(2_600_000, { kind: "stop_periodic", periodic: "nope" }, "refused"));
  s.add(op(2_650_000, { ...VCU, name: "vcu-link", data: "640102" }));
  assert.deepEqual(s.running().map((p) => [p.name, p.since]), [["vcu", 2_450_000], ["vcu-link", 2_650_000]]);
  s.add(op(2_700_000, { kind: "stop_periodic", periodic: "vcu" }));
  assert.deepEqual(s.running().map((p) => p.name), ["vcu-link"]);
  assert.equal(s.levels.get("ams.PF9"), true);
  assert.equal(s.volts.get("ams.PF7"), 1.65);
  assert.ok(s.used.has("vcu") && s.used.has("vcu-link"));
  s.add(op(2_750_000, { kind: "pause" }));
  assert.equal(s.paused, true);
  s.add(op(2_750_000, { kind: "resume" }));
  assert.equal(s.paused, false);
  assert.equal(s.applied, 7);
  assert.equal(s.refused, 1);
});

test("a periodic's name is fresh in the session", () => {
  assert.equal(periodicName([], 0x100), "p100");
  assert.equal(periodicName(new Set(["p100"]), 0x100), "p100-2");
  assert.equal(periodicName(["p100", "p100-2"], 0x100), "p100-3");
});

test("the Send panel's ops and what stops them", () => {
  assert.deepEqual(sendOp({ bus: "can_acu", id: 0x100, data: "000002" }),
                   { kind: "can_send", bus: "can_acu", id: 0x100, data: "000002" });
  assert.deepEqual(sendOp({ bus: "can_acu", id: 0x100, data: "000002", periodMs: 10, name: "p100" }),
                   { kind: "can_periodic", bus: "can_acu", id: 0x100, data: "000002", name: "p100", period_ms: 10 });
  assert.deepEqual(sendOp({ bus: "b", id: 0x1234567, ext: true }), { kind: "can_send", bus: "b", id: 0x1234567, data: "", ext: true });
  assert.equal(sendProblem({ bus: "b", id: 0x100, data: "0001" }), null);
  assert.match(sendProblem({ bus: "", id: 1 }), /bus/);
  assert.match(sendProblem({ bus: "b", id: 0x800 }), /extended/);
  assert.match(sendProblem({ bus: "b", id: 1, data: "0" }), /hex/);
  assert.match(sendProblem({ bus: "b", id: 1, periodMs: 0 }), /period/);
});

test("ops read as text", () => {
  assert.equal(opText(VCU), "periodic vcu can_acu 0x100 [00 00 02] every 10 ms");
  assert.equal(opText({ kind: "gpio", board: "ams", pin: "PF9", level: false }), "gpio ams.PF9 = LOW");
  assert.equal(opText({ kind: "watch", board: "ams", symbol: "g_x" }), "watch ams.g_x");
  assert.equal(opText({ kind: "pause" }), "pause");
});

test("bus rates over the last second of virtual time", () => {
  const r = new BusRates();
  for (let t = 0; t <= 3e6; t += 10_000) r.add("can_acu", t);
  r.add("can_inv", 2.5e6);
  assert.deepEqual(r.rates(3e6), { can_acu: 100, can_inv: 1 });
  assert.deepEqual(r.rates(4e6), { can_acu: 0, can_inv: 0 });
});

test("the clock, the idle countdown, inputs and the recording", () => {
  assert.equal(clockText(12_345_678, 0.981), "t=12.346 s · RTF 0.98×");
  assert.equal(clockText(1e6), "t=1.000 s");
  assert.equal(clockText(1e6, 1, true), "t=1.000 s · paused");
  assert.equal(idleText(1799), "");
  assert.equal(idleText(125), "idle: stops in 2 min 5 s");
  assert.equal(idleText(9), "idle: stops in 9 s");
  const contract = { inputs: { ams: [{ pin: "PF9", kind: "gpio", label: "TSMS" }] } };
  assert.deepEqual(inputsOf(contract, "ams").map((p) => p.label), ["TSMS"]);
  assert.deepEqual(inputsOf(contract, "ecu"), []);
  assert.deepEqual(recordedDoc({ kind: "scenario", system: "ams", virtual_ms: 3050, slice_ms: 50, stimuli: [VCU] }),
                   { description: "", watch: [], expect: [], virtual_ms: 3050, slice_ms: 50, stimuli: [VCU] });
});
