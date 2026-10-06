// static/tokens.css held to WCAG 2.x AA in both themes: 4.5:1 for text,
// 3:1 for control borders, focus rings, wires and plot lines.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const css = readFileSync(new URL("../../vhil/server/static/tokens.css", import.meta.url), "utf8")
  .replace(/\/\*[\s\S]*?\*\//g, "");

// The custom properties declared in the block that opens at `selector {`.
function block(selector, from = 0) {
  const start = css.indexOf(`${selector} {`, from);
  assert.ok(start >= 0, `no ${selector} block`);
  const body = css.slice(css.indexOf("{", start) + 1, css.indexOf("}", start));
  return Object.fromEntries([...body.matchAll(/(--[\w-]+):\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()]));
}

const dark = block(":root", css.lastIndexOf(":root {", css.indexOf("color-scheme: dark")));
const light = block(':root[data-theme="light"]');
const lightMedia = block(':root:not([data-theme="dark"])');

function luminance(hex) {
  const m = /^#([0-9a-f]{6})$/i.exec(hex);
  assert.ok(m, `not a #rrggbb colour: ${hex}`);
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(m[1].slice(i, i + 2), 16) / 255)
    .map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
function contrast(a, b) {
  const [x, y] = [luminance(a), luminance(b)].sort((p, q) => q - p);
  return (x + 0.05) / (y + 0.05);
}

const TEXT = ["--fg", "--fg-muted", "--accent", "--brand-text", "--status-queued", "--status-building",
  "--status-running", "--status-ok", "--status-warn", "--status-failed", "--status-error",
  "--status-cancelled", "--status-stale", "--status-replay"];
const STROKES = ["--border-control", "--focus", "--s1", "--s2", "--s3", "--s4", "--s5", "--s6",
  "--wire-can", "--wire-spi", "--wire-i2c", "--wire-sdmmc", "--wire-uart", "--wire-gpio",
  "--wire-analog", "--can-1", "--can-2", "--can-3", "--can-4", "--role-ecu", "--role-ams", "--role-udv"];
const SURFACES = ["--bg-0", "--bg-1", "--bg-2", "--bg-3"];

for (const [name, theme] of [["dark", dark], ["light", light]]) {
  test(`${name}: text tokens reach 4.5:1 on every surface`, () => {
    const bad = [];
    for (const fg of TEXT) for (const bg of SURFACES) {
      const r = contrast(theme[fg], theme[bg]);
      if (r < 4.5) bad.push(`${fg} on ${bg}: ${r.toFixed(2)}`);
    }
    assert.deepEqual(bad, []);
  });

  test(`${name}: borders, focus, wires and series reach 3:1 on the surfaces`, () => {
    const bad = [];
    for (const fg of STROKES) for (const bg of ["--bg-0", "--bg-1"]) {
      const r = contrast(theme[fg], theme[bg]);
      if (r < 3) bad.push(`${fg} on ${bg}: ${r.toFixed(2)}`);
    }
    assert.deepEqual(bad, []);
  });

  test(`${name}: text on the brand fill reaches 4.5:1`, () => {
    assert.ok(contrast(theme["--on-brand"], theme["--brand"]) >= 4.5);
  });

  test(`${name}: brand, accent and both failure colours are distinct`, () => {
    const roles = ["--brand", "--accent", "--status-failed", "--status-error"].map((t) => theme[t]);
    assert.equal(new Set(roles).size, roles.length);
  });
}

test("the light theme reads the same by preference or by data-theme", () => {
  assert.deepEqual(lightMedia, light);
});

test("both themes define the same tokens", () => {
  assert.deepEqual(Object.keys(light).sort(), Object.keys(dark).sort());
});
