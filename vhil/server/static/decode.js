// Frame decoding against a run's contract (GET /api/runs/{id}/contract,
// vhil/server/decode.py), and the frame filters. Pure functions, no DOM:
// tests/js/decode.test.mjs runs them under node.
//
// Bit numbering is the firmware's (Core/Inc/can/can_dsl.hpp, mirrored in
// vhil/candef.py): an LE field's value bit i is frame bit start + i; a BE
// field starts at its MSB and walks the Motorola sawtooth (down within a
// byte, then bit 7 of the next). physical = raw * factor + offset.

export function hexToBytes(hex) {
  const out = new Uint8Array(hex.length >> 1);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.substr(2 * i, 2), 16);
  return out;
}

export function fieldBits(f) {
  const bits = [];
  if (!f.be) {
    for (let i = 0; i < f.length; i++) bits.push(f.start + i);
  } else {
    let bit = f.start;
    for (let i = 0; i < f.length; i++) {
      bits.push(bit);
      bit = (bit & 7) === 0 ? bit + 15 : bit - 1;
    }
  }
  return bits;   // LE: LSB first; BE: MSB first
}

// The field's raw (sign-extended) value, or null when the frame is too short.
export function rawValue(bytes, f) {
  const bits = f._bits || (f._bits = fieldBits(f));
  const n = bits.length;
  for (const b of bits) if (b >> 3 >= bytes.length) return null;
  const bit = (b) => (bytes[b >> 3] >> (b & 7)) & 1;
  if (n > 52) {   // beyond exact Number integers
    let v = 0n;
    if (f.be) for (const b of bits) v = (v << 1n) | BigInt(bit(b));
    else for (let i = n - 1; i >= 0; i--) v = (v << 1n) | BigInt(bit(bits[i]));
    if (f.signed && v >> BigInt(n - 1)) v -= 1n << BigInt(n);
    return Number(v);
  }
  let v = 0;
  if (f.be) for (const b of bits) v = v * 2 + bit(b);
  else for (let i = n - 1; i >= 0; i--) v = v * 2 + bit(bits[i]);
  if (f.signed && v >= 2 ** (n - 1)) v -= 2 ** n;
  return v;
}

export function physical(raw, f) {
  if (raw === null) return null;
  const v = raw * f.factor + f.offset;
  // 0.1 * 3 = 0.30000000000000004: round to the factor's own precision.
  return Number.isInteger(f.factor) && Number.isInteger(f.offset) ? v : Number(v.toPrecision(12));
}

// The contract entry for a frame record, or undefined. A standard id never
// matches an extended frame (and the reverse).
export function lookup(contract, rec) {
  const msg = contract?.buses?.[rec.bus]?.[rec.id];
  return msg && !!msg.ext === !!rec.ext ? msg : undefined;
}

// [{name, raw, value, unit, label}] for every field of msg.
export function decodeFrame(msg, bytes) {
  return msg.fields.map((f) => {
    const raw = rawValue(bytes, f);
    return { name: f.name, raw, value: physical(raw, f), unit: f.unit,
             label: raw !== null && f.values ? f.values[raw] : undefined };
  });
}

export function formatValue(d) {
  if (d.value === null) return "–";
  const v = Number.isInteger(d.value) ? String(d.value) : String(Number(d.value.toPrecision(7)));
  return d.label !== undefined ? `${d.label} (${v})` : (d.unit && !["enum", "bit", "bool"].includes(d.unit) ? `${v} ${d.unit}` : v);
}

export function hexId(id, ext) {
  return "0x" + id.toString(16).toUpperCase().padStart(ext ? 8 : 3, "0");
}

// "0x100, 700-70D, ams_status" -> predicate(id, name) or throws. Tokens are
// hex ids (0x optional), hex ranges a-b (inclusive), or a case-insensitive
// substring of the decoded message name. A hex id with no digit needs its
// 0x ("0xabc"; "abc" is a name). Empty text matches everything.
export function parseIdFilter(text) {
  const tokens = String(text || "").split(/[\s,]+/).filter(Boolean);
  if (!tokens.length) return () => true;
  const ranges = [], names = [];
  const hex = (s) => {
    const m = /^(?:0x)?([0-9a-f]{1,8})$/i.exec(s);
    return m ? parseInt(m[1], 16) : null;
  };
  for (const t of tokens) {
    const r = /^([^-]+)-([^-]+)$/.exec(t);
    if (r && hex(r[1]) !== null && hex(r[2]) !== null) {
      const [a, b] = [hex(r[1]), hex(r[2])];
      if (a > b) throw new Error(`empty range ${t}`);
      ranges.push([a, b]);
    } else if (hex(t) !== null && (/^0x/i.test(t) || /\d/.test(t))) {
      ranges.push([hex(t), hex(t)]);
    } else if (/^\w+$/.test(t)) {
      names.push(t.toLowerCase());
    } else {
      throw new Error(`not an id, range or name: ${t}`);
    }
  }
  return (id, name) => ranges.some(([a, b]) => id >= a && id <= b) ||
    (!!name && names.some((n) => name.toLowerCase().includes(n)));
}

// Indices of the frames that pass {bus, ids (predicate), fromUs, toUs}.
// `contract` gives names to the id predicate.
export function filterFrames(frames, contract, { bus = "", ids = () => true, fromUs = null, toUs = null } = {}) {
  const out = [];
  for (let i = 0; i < frames.length; i++) {
    const f = frames[i];
    if (bus && f.bus !== bus) continue;
    if (fromUs !== null && f.t_us < fromUs) continue;
    if (toUs !== null && f.t_us > toUs) continue;
    if (!ids(f.id, lookup(contract, f)?.name)) continue;
    out.push(i);
  }
  return out;
}

// One decoded signal as {x: [t_s], y: [value]}: key = "bus/id/field".
export function signalSeries(frames, contract, key) {
  const [bus, idText, name] = key.split("/");
  const id = Number(idText);
  const msg = contract?.buses?.[bus]?.[id];
  const field = msg?.fields.find((f) => f.name === name);
  const x = [], y = [];
  if (!field) return { x, y };
  for (const f of frames) {
    if (f.bus !== bus || f.id !== id || !!f.ext !== !!msg.ext) continue;
    const v = physical(rawValue(f.bytes || (f.bytes = hexToBytes(f.data)), field), field);
    if (v === null) continue;
    x.push(f.t_us / 1e6);
    y.push(v);
  }
  return { x, y };
}

// First index in a t_us-sorted array of records (or of indices into
// `records`) whose t_us >= t.
export function lowerBound(list, t, records = null) {
  let lo = 0, hi = list.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    const r = records ? records[list[mid]] : list[mid];
    if (r.t_us < t) lo = mid + 1; else hi = mid;
  }
  return lo;
}
