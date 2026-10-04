// node tests/js/crosscheck.mjs <cases.json>
// Each case: {msg (candef Message.to_json()), data (hex), expect ({field:
// value} from vhil/candef.py)}. Decodes with static/decode.js and fails on
// any difference (tests/unit/test_inspect.py writes the cases).
import { readFileSync } from "node:fs";
import { decodeFrame, hexToBytes } from "../../vhil/server/static/decode.js";

const cases = JSON.parse(readFileSync(process.argv[2], "utf8"));
const bad = [];
for (const c of cases) {
  const got = Object.fromEntries(decodeFrame(c.msg, hexToBytes(c.data)).filter((d) => d.value !== null)
    .map((d) => [d.name, d.value]));
  for (const k of new Set([...Object.keys(got), ...Object.keys(c.expect)])) {
    const a = got[k], b = c.expect[k];
    if (a === undefined || b === undefined || Math.abs(a - b) > 1e-9 * Math.max(1, Math.abs(b))) {
      bad.push(`${c.msg.name}.${k} [${c.data}]: js ${a} python ${b}`);
    }
  }
}
if (bad.length) {
  console.log(bad.slice(0, 20).join("\n"));
  process.exit(1);
}
console.log(`${cases.length} frames agree`);
