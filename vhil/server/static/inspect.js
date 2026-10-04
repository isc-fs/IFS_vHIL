// The run page (M5.3, #115): #/runs/<id>[/<tab>].
//
// Header (system, ref, state, times, summary, cancel) and five tabs over the
// run's trace (frames, edges, samples, log records; virtual µs) and its
// artifacts. A run still queued or running streams over its WebSocket (which
// replays the trace from the start, then follows it); a finished one loads
// through /trace in pages, so a large trace never arrives as one response.
// Frames decode against the firmware's own contract
// (GET /api/runs/<id>/contract, vhil/server/decode.py).
import * as dec from "./decode.js";
import { PlotGroup, palette } from "./plot.js";
import { VirtualTable } from "./vtable.js";

const TERMINAL = new Set(["passed", "failed", "error", "cancelled"]);
const TABS = ["frames", "signals", "edges", "log", "artifacts"];
const PAGE = 50000;
const ms = (us) => (us / 1000).toFixed(3);
const artifactUrl = (id, name) => `/api/runs/${id}/artifacts/${name.split("/").map(encodeURIComponent).join("/")}`;

export function wallSeconds(run, now = Date.now()) {
  if (!run.started) return null;
  const end = run.finished ? Date.parse(run.finished) : now;
  return Math.max(0, (end - Date.parse(run.started)) / 1000);
}

export function duration(s) {
  if (s === null || s === undefined) return "–";
  if (s < 60) return `${s.toFixed(s < 10 ? 2 : 1)} s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m} min ${Math.round(s % 60)} s` : `${Math.floor(m / 60)} h ${m % 60} min`;
}

// "ecu@dev, ecu.bootloader@v1.7.0": the images a run used (the summary's ELF
// paths sit in <fw-dir>/<firmware>@<ref>/), else the refs it asked for.
function firmware(run) {
  const used = run.summary?.firmware;
  if (used && Object.keys(used).length) {
    return Object.entries(used).map(([k, p]) => {
      const dir = p.split("/").find((s) => s.includes("@"));
      return dir ? `${k}: ${dir}` : `${k}: ${p}`;
    }).join(", ");
  }
  return Object.entries(run.firmware || {}).map(([k, v]) => `${k}@${v ?? "default"}`).join(", ");
}

const store = {
  get(k) { try { return JSON.parse(localStorage.getItem(k)); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } },
};

export async function renderRun(view, { api, esc }, id, tab) {
  tab = TABS.includes(tab) ? tab : "frames";
  const run = await api(`/api/runs/${id}`);
  const S = {
    run, tab, contract: { buses: {} }, contractNote: "",
    rec: { frame: [], edge: [], sample: [], log: [] },
    loading: true, cursorUs: null, ws: null, timers: [], built: {},
    signals: store.get(`vhil.signals.${run.system}`) || [],
    stepped: true,
  };
  view.innerHTML = `<div class="run" id="run-page">
    <div class="run-head"></div>
    <nav class="tabs" role="tablist">${TABS.map((t) =>
      `<button role="tab" data-tab="${t}">${t[0].toUpperCase() + t.slice(1)} <span class="muted" data-count="${t}"></span></button>`).join("")}</nav>
    <p class="muted run-status">loading…</p>
    ${TABS.map((t) => `<section data-panel="${t}" hidden></section>`).join("")}
  </div>`;
  const root = view.querySelector("#run-page");
  const mounted = () => document.body.contains(root);
  const panel = (t) => root.querySelector(`[data-panel="${t}"]`);
  const status = (text) => { root.querySelector(".run-status").textContent = text; };
  const cleanup = () => { S.ws?.close(); S.timers.forEach(clearInterval); S.group?.destroy(); S.edgeGroup?.destroy(); };
  addEventListener("hashchange", cleanup, { once: true });

  // -- header ------------------------------------------------------------------
  function header() {
    const r = S.run;
    const sc = r.scenario || {};
    const sum = r.summary || {};
    const frames = sum.frames ? Object.entries(sum.frames).map(([b, n]) => `${esc(b)} ${n}`).join(" · ") : "";
    const junit = sum.tests !== undefined
      ? `${sum.tests} tests · ${sum.failures} failed · ${sum.errors} errors · ${sum.skipped} skipped` : "";
    root.querySelector(".run-head").innerHTML = `
      <h2>Run ${r.id} <span class="badge state-${esc(r.state)}">${esc(r.state)}</span>
        ${TERMINAL.has(r.state) ? "" : `<button data-cancel>Cancel</button>`}</h2>
      <dl class="meta">
        <div><dt>System</dt><dd><a href="#/systems/${esc(r.system)}">${esc(r.system)}</a></dd></div>
        <div><dt>Ref</dt><dd>${r.ref_name ? `${esc(r.ref_name)} ` : ""}<code>${esc((r.ref || "working tree").slice(0, 12))}</code></dd></div>
        <div><dt>Scenario</dt><dd>${esc(sc.kind === "run" ? `run ${sc.virtual_ms} ms` : `pytest ${sc.select || ""}`)}</dd></div>
        <div><dt>Virtual</dt><dd>${ms(r.virtual_us)} ms</dd></div>
        <div><dt>Wall</dt><dd>${duration(wallSeconds(r))}</dd></div>
        <div><dt>Created</dt><dd>${esc((r.created || "").replace("T", " ").slice(0, 19))}</dd></div>
        ${r.worker ? `<div><dt>Worker</dt><dd class="muted">${esc(r.worker)}</dd></div>` : ""}
        ${frames ? `<div><dt>Frames</dt><dd>${frames}</dd></div>` : ""}
        ${junit ? `<div><dt>Tests</dt><dd>${esc(junit)}</dd></div>` : ""}
        ${firmware(r) ? `<div><dt>Firmware</dt><dd>${esc(firmware(r))}</dd></div>` : ""}
      </dl>
      ${sum.error ? `<p class="error">${esc(sum.error)}</p>` : ""}`;
    const cancel = root.querySelector("[data-cancel]");
    if (cancel) cancel.onclick = async () => { S.run = await api(`/api/runs/${id}/cancel`, { method: "POST" }); header(); };
  }
  header();

  // -- tabs --------------------------------------------------------------------
  function show(t) {
    S.tab = t;
    history.replaceState(null, "", `#/runs/${id}/${t}`);
    for (const b of root.querySelectorAll("[data-tab]")) b.setAttribute("aria-selected", b.dataset.tab === t);
    for (const p of root.querySelectorAll("[data-panel]")) p.hidden = p.dataset.panel !== t;
    if (!S.built[t]) { S.built[t] = true; builders[t](panel(t)); }
    refresh(t, true);
    if (t === "frames") frameCursor();
  }
  root.querySelector(".tabs").addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-tab]");
    if (b) show(b.dataset.tab);
  });

  function counts() {
    const n = (k) => S.rec[k].length.toLocaleString();
    root.querySelector('[data-count="frames"]').textContent = n("frame");
    root.querySelector('[data-count="edges"]').textContent = n("edge");
    root.querySelector('[data-count="log"]').textContent = n("log");
    root.querySelector('[data-count="signals"]').textContent = S.signals.length || "";
  }

  // -- shared time cursor --------------------------------------------------------
  function setCursor(tUs, from) {
    S.cursorUs = tUs;
    if (from !== "plot") { S.group?.setMarker(tUs); S.edgeGroup?.setMarker(tUs); }
    if (from !== "frames") {
      S.frameCursorPending = true;   // a hidden table can't scroll: applied when shown
      if (S.tab === "frames") frameCursor();
    }
  }

  function frameCursor() {
    if (!S.frameCursorPending || !S.frames || S.cursorUs === null) return;
    S.frameCursorPending = false;
    const i = Math.min(dec.lowerBound(S.frames.idx, S.cursorUs, S.rec.frame), S.frames.idx.length - 1);
    if (i >= 0) S.frames.table.select(i, false, true);
  }

  // -- frames --------------------------------------------------------------------
  const builders = {};
  builders.frames = (el) => {
    el.innerHTML = `<form class="filters" onsubmit="return false">
        <label>Bus <select name="bus"><option value="">all</option></select></label>
        <label class="grow">IDs <input name="ids" placeholder="0x100, 700-70D, status" autocomplete="off"></label>
        <label>From ms <input name="from" type="number" step="any" min="0"></label>
        <label>To ms <input name="to" type="number" step="any" min="0"></label>
        <label><input name="follow" type="checkbox"> follow</label>
        <span class="muted" data-shown></span>
      </form>
      <p class="muted contract-note"></p>
      <div class="split"><div class="frames-table"></div><aside class="frame-detail muted">Select a frame.</aside></div>`;
    const form = el.querySelector("form");
    // idx: the record indices that pass the filters; selRec: the selected
    // record, kept across a rescan (new filters, the contract arriving).
    const F = S.frames = { idx: [], scanned: 0, opts: {}, form, selRec: null, rebuilt: false,
                           rescan() { F.idx = []; F.scanned = 0; F.rebuilt = true; } };
    const decoded = (f) => {
      if (f._d === undefined) {
        const msg = dec.lookup(S.contract, f);
        f._d = msg ? { msg, fields: dec.decodeFrame(msg, f.bytes || (f.bytes = dec.hexToBytes(f.data))) } : null;
      }
      return f._d;
    };
    F.table = new VirtualTable(el.querySelector(".frames-table"), {
      cls: "vt-frames",
      header: "<span>t (ms)</span><span>bus</span><span>id</span><span>dlc</span><span>data</span><span>decoded</span>",
      render: (i) => {
        const f = S.rec.frame[F.idx[i]];
        const d = decoded(f);
        const text = d ? `${d.msg.name}  ${d.fields.slice(0, 6).map((x) => `${x.name}=${dec.formatValue(x)}`).join("  ")}` : "";
        return `<span>${ms(f.t_us)}</span><span>${f.src ? `<i class="muted" title="${esc(f.src)}: sent by the run's scenario">⇢</i> ` : ""}${esc(f.bus)}</span><span>${dec.hexId(f.id, f.ext)}${f.ext ? "x" : ""}</span>` +
          `<span>${f.data.length / 2}</span><span class="mono">${f.data.replace(/(..)(?!$)/g, "$1 ")}</span>` +
          `<span class="dec" title="${esc(text)}">${d ? `<b>${esc(d.msg.name)}</b> ${esc(text.slice(d.msg.name.length))}` : ""}</span>`;
      },
      onSelect: (i, fromUser) => {
        F.selRec = F.idx[i];
        const f = S.rec.frame[F.selRec];
        detail(f, decoded(f));
        if (fromUser) setCursor(f.t_us, "frames");
      },
    });
    const apply = () => {
      const v = Object.fromEntries(new FormData(form));
      let ids;
      try {
        ids = dec.parseIdFilter(v.ids);
        form.ids.setCustomValidity("");
      } catch (e) {
        form.ids.setCustomValidity(e.message);
        form.ids.reportValidity();
        return;
      }
      F.opts = { bus: v.bus, ids, fromUs: v.from === "" ? null : Number(v.from) * 1000,
                 toUs: v.to === "" ? null : Number(v.to) * 1000 };
      F.rescan();
      refresh("frames", true);
    };
    form.addEventListener("input", (ev) => { if (ev.target.name === "follow") F.table.follow = ev.target.checked; else apply(); });
    F.table.follow = !TERMINAL.has(S.run.state);
    form.follow.checked = F.table.follow;
    F.apply = apply;
    apply();

    function detail(f, d) {
      const aside = el.querySelector(".frame-detail");
      aside.classList.remove("muted");
      const head = `<p><b>${ms(f.t_us)} ms</b> · ${esc(f.bus)} · ${dec.hexId(f.id, f.ext)}${f.ext ? " (ext)" : ""} · ${f.data.length / 2} bytes${f.src ? ` · ${esc(f.src)} (sent by the scenario)` : ""}</p>
        <p class="mono">${f.data.replace(/(..)(?!$)/g, "$1 ") || "(no data)"}</p>`;
      if (!d) {
        aside.innerHTML = head + `<p class="muted">No declaration of this id on ${esc(f.bus)} in the run's contract.</p>`;
        return;
      }
      const m = d.msg;
      aside.innerHTML = head + `<p><b>${esc(m.name)}</b> <span class="muted">from ${esc(m.sender)}, ${m.period_ms ? `every ${m.period_ms} ms` : "event"}, dlc ${m.dlc} · ${esc(m.board)} ${esc(m.source)}</span></p>
        ${f.data.length / 2 !== m.dlc ? `<p class="error">DLC ${f.data.length / 2}, declared ${m.dlc}</p>` : ""}
        <table class="fields"><tr><th>field</th><th>value</th><th>raw</th><th></th></tr>${d.fields.map((x) => {
          const key = `frame:${f.bus}/${f.id}/${x.name}`;
          return `<tr><td>${esc(x.name)}</td><td>${esc(dec.formatValue(x))}</td><td class="muted">${x.raw ?? "–"}</td>
            <td><button type="button" class="small" data-plot="${esc(key)}" ${S.signals.includes(key) ? "disabled" : ""} title="Plot in Signals">plot</button></td></tr>`;
        }).join("")}</table>`;
      aside.querySelectorAll("[data-plot]").forEach((b) => b.addEventListener("click", () => {
        addSignal(b.dataset.plot);
        b.disabled = true;
      }));
    }
  };

  function refreshFrames() {
    const F = S.frames;
    const all = S.rec.frame;
    if (F.scanned < all.length) {
      const tail = all.slice(F.scanned);
      for (const i of dec.filterFrames(tail, S.contract, F.opts)) F.idx.push(F.scanned + i);
      F.scanned = all.length;
    }
    if (F.rebuilt) {
      F.rebuilt = false;
      F.table.selected = F.selRec === null ? -1 : F.idx.indexOf(F.selRec);
    }
    const buses = [...new Set([...Object.keys(S.contract.buses || {}), ...all.slice(-5000).map((f) => f.bus)])].sort();
    const sel = F.form.bus;
    if (sel.options.length - 1 !== buses.length) {
      const cur = sel.value;
      sel.innerHTML = `<option value="">all</option>` + buses.map((b) => `<option>${esc(b)}</option>`).join("");
      sel.value = cur;
    }
    F.form.querySelector("[data-shown]").textContent = `${F.idx.length.toLocaleString()} of ${all.length.toLocaleString()}`;
    panel("frames").querySelector(".contract-note").textContent = S.contractNote;
    F.table.setCount(F.idx.length);
  }

  // -- signals -------------------------------------------------------------------
  function addSignal(key) {
    if (S.signals.includes(key)) return;
    S.signals = [...S.signals, key].slice(-16);
    store.set(`vhil.signals.${S.run.system}`, S.signals);
    counts();
    if (S.built.signals) refresh("signals", true);
  }

  function signalLabel(key) {
    if (key.startsWith("sample:")) return key.slice(7);
    const [bus, idText, field] = key.slice(6).split("/");
    const msg = S.contract.buses?.[bus]?.[idText];
    return `${bus} ${msg ? msg.name : dec.hexId(Number(idText), false)}.${field}`;
  }

  function signalData(key) {
    if (key.startsWith("sample:")) {
      const name = key.slice(7);
      const x = [], y = [];
      for (const s of S.rec.sample) {
        if (`${s.board}.${s.name}` === name) { x.push(s.t_us / 1e6); y.push(s.value); }
      }
      return { x, y, unit: "" };
    }
    const [bus, idText, field] = key.slice(6).split("/");
    const f = S.contract.buses?.[bus]?.[idText]?.fields.find((g) => g.name === field);
    return { ...dec.signalSeries(S.rec.frame, S.contract, key.slice(6)), unit: f?.unit || "" };
  }

  builders.signals = (el) => {
    el.innerHTML = `<div class="sig-pick">
        <label>Sampled <select name="sample"><option value="">add a watched symbol…</option></select></label>
        <label>Message <select name="msg"><option value="">decoded message…</option></select></label>
        <label>Field <select name="field"></select></label>
        <button type="button" data-add>Add</button>
        <label><input type="checkbox" name="stepped" checked> steps</label>
      </div>
      <div class="chips"></div>
      <p class="muted sig-help">Drag to zoom · ctrl-wheel or pinch to zoom · shift-drag to pan · double-click for the whole run · click to select a time (the frame table follows).</p>
      <div class="plots"></div>`;
    const msgSel = el.querySelector("[name=msg]"), fieldSel = el.querySelector("[name=field]");
    msgSel.addEventListener("change", () => {
      const [bus, idText] = msgSel.value.split("/");
      const msg = S.contract.buses?.[bus]?.[idText];
      fieldSel.innerHTML = msg ? msg.fields.map((f) => `<option>${esc(f.name)}</option>`).join("") : "";
    });
    el.querySelector("[data-add]").addEventListener("click", () => {
      if (msgSel.value && fieldSel.value) addSignal(`frame:${msgSel.value}/${fieldSel.value}`);
    });
    el.querySelector("[name=sample]").addEventListener("change", (ev) => {
      if (ev.target.value) addSignal(`sample:${ev.target.value}`);
      ev.target.value = "";
    });
    el.querySelector("[name=stepped]").addEventListener("change", (ev) => { S.stepped = ev.target.checked; refresh("signals", true); });
    el.querySelector(".chips").addEventListener("click", (ev) => {
      const key = ev.target.dataset.remove;
      if (!key) return;
      S.signals = S.signals.filter((k) => k !== key);
      store.set(`vhil.signals.${S.run.system}`, S.signals);
      counts();
      refresh("signals", true);
    });
  };

  function refreshSignals() {
    const el = panel("signals");
    // Pickers: the sample series and the decoded messages the trace holds.
    const samples = [...new Set(S.rec.sample.map((s) => `${s.board}.${s.name}`))].sort();
    const sSel = el.querySelector("[name=sample]");
    if (sSel.options.length - 1 !== samples.length) {
      sSel.innerHTML = `<option value="">${samples.length ? "add a watched symbol…" : "no samples in this run"}</option>` +
        samples.map((s) => `<option>${esc(s)}</option>`).join("");
    }
    const seen = new Map();
    for (const f of S.rec.frame) {
      const k = `${f.bus}/${f.id}`;
      if (!seen.has(k)) { const m = dec.lookup(S.contract, f); if (m) seen.set(k, m); }
    }
    const mSel = el.querySelector("[name=msg]");
    if (mSel.options.length - 1 !== seen.size) {
      const cur = mSel.value;
      mSel.innerHTML = `<option value="">decoded message…</option>` + [...seen].sort(([a], [b]) => a.localeCompare(b, undefined, { numeric: true }))
        .map(([k, m]) => `<option value="${esc(k)}">${esc(k.split("/")[0])} · ${dec.hexId(m.id, m.ext)} ${esc(m.name)}</option>`).join("");
      mSel.value = cur;
    }
    el.querySelector(".chips").innerHTML = S.signals.map((k) =>
      `<span class="chip">${esc(signalLabel(k))} <button type="button" data-remove="${esc(k)}" title="Remove">×</button></span>`).join("");
    // Plots: one per signal, rebuilt (uPlot is fast; a live run throttles this).
    const range = S.group?.range ?? null;
    S.group?.destroy();
    S.group = new PlotGroup({ onPick: (t) => setCursor(t, "plot") });
    S.group.range = range;
    const box = el.querySelector(".plots");
    box.innerHTML = S.signals.length ? "" : `<p class="muted">Pick sampled symbols (a run's <code>watch</code>) or decoded fields above, or press "plot" next to a field in the frame detail.</p>`;
    const colors = palette();
    S.signals.forEach((key, n) => {
      const { x, y, unit } = signalData(key);
      const div = document.createElement("div");
      div.className = "plot";
      box.appendChild(div);
      if (!x.length) { div.innerHTML = `<p class="muted">${esc(signalLabel(key))}: ${S.loading ? "loading…" : TERMINAL.has(S.run.state) ? "no data in this run" : "no data yet"}</p>`; return; }
      S.group.add(div, { label: signalLabel(key), unit, x, y, stepped: S.stepped, color: colors[n % colors.length] });
    });
    S.group.setRange(range);
    S.group.setMarker(S.cursorUs);
  }

  // -- edges ---------------------------------------------------------------------
  builders.edges = (el) => {
    el.innerHTML = `<div class="plots"></div><div class="edges-table"></div>`;
    S.edgeTable = new VirtualTable(el.querySelector(".edges-table"), {
      cls: "vt-edges",
      header: "<span>t (ms)</span><span>board</span><span>pin</span><span>level</span>",
      render: (i) => {
        const e = S.rec.edge[i];
        return `<span>${ms(e.t_us)}</span><span>${esc(e.board)}</span><span>${esc(e.pin)}</span><span>${e.level}</span>`;
      },
      onSelect: (i, fromUser) => { if (fromUser) setCursor(S.rec.edge[i].t_us, "edges"); },
    });
  };

  function refreshEdges(plots) {
    const el = panel("edges");
    S.edgeTable.setCount(S.rec.edge.length);
    if (!plots) return;
    const range = S.edgeGroup?.range ?? null;
    S.edgeGroup?.destroy();
    S.edgeGroup = new PlotGroup({ onPick: (t) => setCursor(t, "plot") });
    const box = el.querySelector(".plots");
    const pins = new Map();
    for (const e of S.rec.edge) {
      const k = `${e.board}.${e.pin}`;
      if (!pins.has(k)) pins.set(k, { x: [], y: [] });
      pins.get(k).x.push(e.t_us / 1e6);
      pins.get(k).y.push(e.level);
    }
    const end = Math.max(S.run.virtual_us, S.rec.edge.at(-1)?.t_us || 0) / 1e6;
    box.innerHTML = pins.size ? "" : `<p class="muted">No GPIO edges: a run records the pins its scenario watches (<code>watch: [{kind: pin, …}]</code>).</p>`;
    const colors = palette();
    [...pins].forEach(([k, s], n) => {
      if (s.x.at(-1) < end) { s.x.push(end); s.y.push(s.y.at(-1)); }   // hold to the end
      const div = document.createElement("div");
      div.className = "plot";
      box.appendChild(div);
      S.edgeGroup.add(div, { label: k, x: s.x, y: s.y, stepped: true, color: colors[n % colors.length], height: 110 });
    });
    S.edgeGroup.setRange(range);
    S.edgeGroup.setMarker(S.cursorUs);
  }

  // -- log -------------------------------------------------------------------------
  builders.log = (el) => {
    el.innerHTML = `<div class="log-table"></div><pre class="log-full" hidden></pre>`;
    const full = el.querySelector(".log-full");
    S.logTable = new VirtualTable(el.querySelector(".log-table"), {
      cls: "vt-log",
      header: "<span>t (ms)</span><span>text</span>",
      render: (i) => {
        const r = S.rec.log[i];
        return `<span>${ms(r.t_us)}</span><span class="dec" title="${esc(r.text)}">${esc(r.text)}</span>`;
      },
      onSelect: (i, fromUser) => {
        full.hidden = false;
        full.textContent = S.rec.log[i].text;
        if (fromUser) setCursor(S.rec.log[i].t_us, "log");
      },
    });
    S.logTable.follow = !TERMINAL.has(S.run.state);
  };

  // -- artifacts -----------------------------------------------------------------
  builders.artifacts = (el) => {
    el.innerHTML = `<p class="muted">Loading…</p>`;
    el.addEventListener("click", (ev) => {   // not a hash link: that would route away
      const a = ev.target.closest("[data-snap]");
      if (!a) return;
      ev.preventDefault();
      const d = el.querySelector(`#snap-${CSS.escape(a.dataset.snap)}`);
      if (d) { d.open = true; d.scrollIntoView({ block: "start" }); }
    });
  };

  async function refreshArtifacts() {
    const el = panel("artifacts");
    const names = await api(`/api/runs/${id}/artifacts`);
    if (!mounted()) return;
    const snaps = names.filter((n) => n.startsWith("sim-logs/failures/"));
    const groups = new Map();
    for (const n of snaps) {
      const dir = n.split("/").slice(0, 3).join("/");
      if (!groups.has(dir)) groups.set(dir, []);
      groups.get(dir).push(n);
    }
    el.innerHTML = `
      <div class="junit"></div>
      ${groups.size ? `<h3>Failure snapshots</h3>${[...groups].map(([dir, files]) => `
        <details class="snap" id="snap-${esc(dir.split("/")[2])}"><summary>${esc(dir.split("/")[2])}</summary>
          ${files.map((f) => `<p><a href="${artifactUrl(id, f)}" target="_blank" rel="noopener">${esc(f.split("/").pop())}</a></p><pre data-src="${esc(f)}"></pre>`).join("")}
        </details>`).join("")}` : ""}
      ${names.includes("worker-error.txt") ? `<h3>Worker error</h3><pre data-src="worker-error.txt" data-eager></pre>` : ""}
      ${names.includes("pytest.txt") ? `<details open><summary><b>pytest.txt</b></summary><pre data-src="pytest.txt" data-eager data-tail="400000"></pre></details>` : ""}
      <h3>Files</h3>
      ${names.length ? `<ul class="files">${names.map((n) => `<li><a href="${artifactUrl(id, n)}" target="_blank" rel="noopener">${esc(n)}</a></li>`).join("")}</ul>`
        : `<p class="muted">No artifacts${TERMINAL.has(S.run.state) ? "" : " yet"}.</p>`}`;
    const load = async (pre) => {
      if (pre.dataset.loaded) return;
      pre.dataset.loaded = "1";
      let text = await api(artifactUrl(id, pre.dataset.src), { as: "text" });
      const tail = Number(pre.dataset.tail || 0);
      if (tail && text.length > tail) text = `… (${(text.length - tail).toLocaleString()} characters cut; the file link has all of it)\n` + text.slice(-tail);
      pre.textContent = text;   // text, never markup
      if (tail) pre.scrollTop = pre.scrollHeight;   // pytest's summary is at the end
    };
    el.querySelectorAll("pre[data-eager]").forEach(load);

    el.querySelectorAll("details.snap").forEach((d) => d.addEventListener("toggle", () => {
      if (d.open) d.querySelectorAll("pre[data-src]").forEach(load);
    }));
    if (names.includes("junit.xml")) {
      const xml = await api(artifactUrl(id, "junit.xml"), { as: "text" });
      if (!mounted()) return;
      el.querySelector(".junit").innerHTML = junitTable(parseJunit(xml), [...groups.keys()].map((d) => d.split("/")[2]), esc);
    }
  }

  // -- loading ---------------------------------------------------------------------
  let pending = false;
  let lastHeavy = 0;
  function refresh(t = S.tab, force = false) {
    if (!S.built[t]) return;
    if (t === "frames") refreshFrames();
    if (t === "log") S.logTable.setCount(S.rec.log.length);
    // Plots and artifacts rebuild whole: at most once a second while streaming.
    const now = performance.now();
    if (t === "signals" && (force || now - lastHeavy > 1000)) { lastHeavy = now; refreshSignals(); }
    if (t === "edges") {
      const heavy = force || now - lastHeavy > 1000;
      if (heavy) lastHeavy = now;
      refreshEdges(heavy);
    }
    if (t === "artifacts" && force) refreshArtifacts().catch((e) => { panel("artifacts").innerHTML = `<p class="error">${esc(e.message)}</p>`; });
  }

  function ingest(records) {
    for (const r of records) S.rec[r.kind]?.push(r);
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => {
      pending = false;
      if (!mounted()) return;
      counts();
      refresh();
    });
  }

  function finished(state) {
    S.loading = false;
    status(`${S.rec.frame.length.toLocaleString()} frames, ${S.rec.edge.length.toLocaleString()} edges, ` +
           `${S.rec.sample.length.toLocaleString()} samples, ${S.rec.log.length.toLocaleString()} log records` +
           (state ? ` · run ${state}` : ""));
    counts();
    refresh(S.tab, true);
  }

  async function loadPages() {
    // Each page resumes where the last one ended: the server's cursor
    // (X-Trace-Cursor) is a position in the trace file, so a page costs what
    // it returns however far into a long trace it is.
    let cursor = "";
    for (;;) {
      const r = await api(`/api/runs/${id}/trace?limit=${PAGE}${cursor ? `&cursor=${cursor}` : ""}`,
                          { as: "response" });
      const page = await r.json();
      if (!mounted()) return;
      ingest(page);
      status(`loading… ${(S.rec.frame.length + S.rec.edge.length + S.rec.sample.length + S.rec.log.length).toLocaleString()} records`);
      const next = r.headers.get("X-Trace-Cursor");
      if (page.length < PAGE || !next || next === cursor) break;
      cursor = next;
    }
    finished();
  }

  function follow() {
    S.loading = false;   // streaming: what is not there yet may still come
    status("live…");
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = S.ws = new WebSocket(`${proto}://${location.host}/api/runs/${id}/live`);
    let ended = false;
    ws.onmessage = (ev) => {
      if (!mounted()) { ws.close(); return; }
      const rec = JSON.parse(ev.data);
      if (rec.kind === "end") {
        ended = true;
        api(`/api/runs/${id}`).then((r) => { S.run = r; header(); finished(r.state); });
        return;
      }
      ingest([rec]);
    };
    ws.onclose = () => { if (!ended && mounted()) status("live connection closed: reload the page to resume"); };
    S.timers.push(setInterval(async () => {
      if (!mounted() || TERMINAL.has(S.run.state)) return;
      S.run = await api(`/api/runs/${id}`);
      if (mounted()) header();
    }, 2000));
  }

  api(`/api/runs/${id}/contract`).then((c) => {
    S.contract = c;
    const boards = Object.entries(c.boards || {});
    const bad = boards.filter(([, b]) => b.error).map(([n, b]) => `${n}: ${b.error}`);
    const ok = boards.filter(([, b]) => !b.error).map(([n, b]) => `${n} ${b.messages} messages on ${(b.buses || []).join(", ") || "no bus"}`);
    S.contractNote = (c.error ? `No decoding: ${c.error}. ` : "") +
      (ok.length ? `Decoded against the firmware's .def contract (${ok.join("; ")}), on the buses its catalogue entry says it rides.` : "") +
      (bad.length ? ` Not decoded: ${bad.join("; ")}.` : "") +
      (c.conflicts?.length ? ` ${c.conflicts.length} id(s) declared differently by two boards: ${c.conflicts.map((x) => `${x.bus} ${dec.hexId(x.id)} (${x.kept} kept)`).join(", ")}.` : "");
    for (const f of S.rec.frame) f._d = undefined;
    S.frames?.rescan();
    if (mounted()) refresh(S.tab, true);
  }).catch((e) => { S.contractNote = `No decoding: ${e.message}`; });

  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    if (mounted() && (S.tab === "signals" || S.tab === "edges")) refresh(S.tab, true);
  });

  show(tab);
  if (TERMINAL.has(run.state)) await loadPages(); else follow();
}

// -- JUnit -----------------------------------------------------------------------

export function parseJunit(xml) {
  const doc = new DOMParser().parseFromString(xml, "application/xml");
  return [...doc.getElementsByTagName("testcase")].map((tc) => {
    const node = ["failure", "error", "skipped"].map((k) => tc.getElementsByTagName(k)[0]).find(Boolean);
    return { classname: tc.getAttribute("classname") || "", name: tc.getAttribute("name") || "",
             time: Number(tc.getAttribute("time") || 0),
             outcome: node ? (node.tagName === "skipped" ? "skipped" : node.tagName === "error" ? "error" : "failed") : "passed",
             message: node?.getAttribute("message") || "", text: node?.textContent || "" };
  });
}

// The snapshot directory of a test: <nodeid with non-word runs as _>-<when>
// (tests/sim/conftest.py _snapshot_report).
function snapFor(tc, dirs) {
  const safe = (s) => s.replace(/[^\w.-]+/g, "_");
  const mod = tc.classname.split(".").pop();
  return dirs.filter((d) => d.includes(safe(tc.name)) && d.includes(mod));
}

export function junitTable(cases, snapDirs, esc) {
  if (!cases.length) return `<p class="muted">junit.xml has no test cases.</p>`;
  const n = (o) => cases.filter((c) => c.outcome === o).length;
  return `<h3>Tests <span class="muted">${n("passed")} passed · ${n("failed")} failed · ${n("error")} errors · ${n("skipped")} skipped</span></h3>
    <table class="junit-table"><tr><th></th><th>test</th><th>time</th></tr>${cases.map((c) => {
      const snaps = c.outcome === "failed" || c.outcome === "error" ? snapFor(c, snapDirs) : [];
      return `<tr class="${c.outcome}"><td><span class="badge state-${c.outcome === "passed" ? "passed" : c.outcome === "skipped" ? "cancelled" : "failed"}">${c.outcome}</span></td>
        <td><span class="muted">${esc(c.classname)}</span>::${esc(c.name)}
          ${c.message || c.text ? `<details><summary>${esc(c.message.slice(0, 200) || "details")}</summary><pre>${esc(c.text)}</pre></details>` : ""}
          ${snaps.map((d) => `<a href="" data-snap="${esc(d)}">snapshot</a>`).join(" ")}</td>
        <td>${c.time.toFixed(2)} s</td></tr>`;
    }).join("")}</table>`;
}

