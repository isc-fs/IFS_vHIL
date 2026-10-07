// Runs page (M5.2, #114): start a `run` scenario, list runs, and count each
// running run's frames per bus live over its WebSocket. The history filters
// by system and state; a run's own page (frames, plots, JUnit) is inspect.js
// (#115).
import { duration, wallSeconds } from "./inspect.js";

const STATES = ["queued", "running", "passed", "failed", "error", "cancelled"];
const TERMINAL = new Set(["passed", "failed", "error", "cancelled"]);
const live = new Map();   // run id -> {ws, counts: {bus: n}}

export async function renderRuns(view, { api, esc }) {
  // The default virtual time is the server's (runs.py DEFAULT_VIRTUAL_MS).
  const [systems, config] = await Promise.all([api("/api/systems"), api("/api/config")]);
  view.innerHTML = `<h2>Runs</h2>
    <form id="run-form">
      <label>System <select name="system">${
        systems.map((s) => `<option value="${esc(s.id)}">${esc(s.id)}</option>`).join("")}</select></label>
      <label>Ref <input name="ref" list="run-refs" placeholder="HEAD" size="14" autocomplete="off"
        title="A branch, tag or commit of the workspace: the system file as saved there (empty: the workspace's HEAD)"></label>
      <datalist id="run-refs"></datalist>
      <label>Scenario <select name="kind"><option value="run">run</option><option value="pytest">pytest</option></select></label>
      <label data-kind="run">Virtual ms <input name="virtual_ms" type="number" value="${Number(config.run_virtual_ms)}" min="1" max="600000" required></label>
      <label data-kind="pytest" class="grow" hidden>Test <input name="select" list="run-tests" placeholder="tests/sim/test_x.py[::test_y]"
        autocomplete="off" disabled required></label>
      <label data-kind="pytest" hidden>Timeout s <input name="timeout_s" type="number" value="3600" min="10" max="21600" disabled required></label>
      <datalist id="run-tests"></datalist>
      <button type="submit">Start run</button>
      <span id="run-msg" class="muted"></span>
    </form>
    <h3>History</h3>
    <form id="run-filter" class="filters">
      <label>System <select name="system"><option value="">all</option>${
        systems.map((s) => `<option value="${esc(s.id)}">${esc(s.id)}</option>`).join("")}</select></label>
      <label>State <select name="state"><option value="">all</option>${
        STATES.map((s) => `<option>${s}</option>`).join("")}</select></label>
    </form>
    <div class="scroll-x"><table id="runs"><thead><tr><th class="num">Run</th><th>State</th><th>System</th><th>Owner</th><th>Scenario</th>
      <th class="num">Virtual</th><th class="num">Wall</th><th class="num">Frames / tests</th><th>Created</th><th></th></tr></thead><tbody></tbody></table></div>`;
  const table = view.querySelector("#runs tbody");
  const msg = view.querySelector("#run-msg");
  const mounted = () => document.body.contains(table);

  const form = view.querySelector("#run-form");
  // The pytest picker: test files and node ids under tests/sim, as the
  // server collects them (GET /api/tests, cached there), fetched once the
  // kind is first switched to pytest. Free text works too.
  let tests = null;
  async function loadTests() {
    if (tests) return;
    tests = api("/api/tests");
    try {
      const t = await tests;
      view.querySelector("#run-tests").innerHTML = [...t.files, ...t.tests]
        .map((id) => `<option value="${esc(id)}"></option>`).join("");
      if (t.error) { msg.textContent = `test list: ${t.error}`; msg.className = "error"; }
    } catch (e) {
      tests = null;
      msg.textContent = `test list: ${e.message}`;
      msg.className = "error";
    }
  }
  // The ref picker: the workspace's branches (where the editor saves) and tags.
  api("/api/workspace/refs").then((refs) => {
    view.querySelector("#run-refs").innerHTML = [...refs.branches, ...refs.tags]
      .map((r) => `<option value="${esc(r)}"></option>`).join("");
  }).catch(() => {});
  form.kind.addEventListener("change", () => {
    for (const el of form.querySelectorAll("[data-kind]")) {
      const on = el.dataset.kind === form.kind.value;
      el.hidden = !on;
      el.querySelectorAll("input").forEach((i) => { i.disabled = !on; });
    }
    if (form.kind.value === "pytest") loadTests();
  });

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = new FormData(ev.target);
    const scenario = f.get("kind") === "pytest"
      ? { kind: "pytest", select: f.get("select").trim(), timeout_s: Number(f.get("timeout_s")) }
      : { kind: "run", virtual_ms: Number(f.get("virtual_ms")) };
    const body = { system: f.get("system"), scenario };
    if (f.get("ref").trim()) body.ref = f.get("ref").trim();
    try {
      const out = await api("/api/runs", { method: "POST", headers: { "content-type": "application/json" },
                                           body: JSON.stringify(body) });
      msg.innerHTML = `queued <a href="#/runs/${out.run_id}">run ${out.run_id}</a>`;
      msg.className = "muted";
    } catch (e) {
      msg.textContent = `error: ${e.message}`;
      msg.className = "error";
    }
    refresh();
  });

  table.addEventListener("click", async (ev) => {
    const id = ev.target.dataset.cancel;
    if (!id) return;
    try { await api(`/api/runs/${id}/cancel`, { method: "POST" }); } catch (e) { msg.textContent = `error: ${e.message}`; msg.className = "error"; }
    refresh();
  });
  const filter = view.querySelector("#run-filter");
  filter.addEventListener("change", () => refresh());

  const counts = (run) => {
    const s = run.summary || {};
    if (!live.has(run.id) && s.tests !== undefined) {
      return `${s.tests} tests, ${s.failures + s.errors} failed`;
    }
    const c = live.get(run.id)?.counts || s.frames || {};
    return Object.entries(c).map(([bus, n]) => `${esc(bus)} ${n}`).join(" · ");
  };

  function follow(run) {
    if (live.has(run.id)) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/api/runs/${run.id}/live?kinds=frame`);
    const entry = { ws, counts: {} };
    live.set(run.id, entry);
    ws.onmessage = (ev) => {
      if (!mounted()) { ws.close(); return; }
      const rec = JSON.parse(ev.data);
      if (rec.kind === "end") { live.delete(run.id); refresh(); return; }
      if (rec.src) return;   // the scenario's own frames: summary.sent, not frames
      entry.counts[rec.bus] = (entry.counts[rec.bus] || 0) + 1;
      const cell = table.querySelector(`[data-frames="${run.id}"]`);
      if (cell) cell.textContent = counts(run);
    };
    ws.onclose = () => { if (live.get(run.id) === entry && !mounted()) live.delete(run.id); };
  }

  let timer = null;
  async function refresh() {
    if (!mounted()) return;
    const q = new URLSearchParams({ limit: 100 });
    for (const [k, v] of new FormData(filter)) if (v) q.set(k, v);
    const runs = await api(`/api/runs?${q}`);
    if (!mounted()) return;
    table.innerHTML = runs.map((r) => `<tr>
      <td class="num"><a href="#/runs/${r.id}">${r.id}</a></td><td><span class="badge state-${esc(r.state)}">${esc(r.state)}</span></td>
      <td>${esc(r.system)}${r.ref_name ? ` <span class="muted">@ ${esc(r.ref_name)}</span>` : ""}</td>
      <td>${esc(r.owner || "")}</td>
      <td class="muted">${esc(r.scenario.kind === "run" ? `run ${r.scenario.virtual_ms} ms` : `pytest ${r.scenario.select}`)}</td>
      <td class="num">${(r.virtual_us / 1000).toFixed(0)} ms</td><td class="num">${duration(wallSeconds(r))}</td>
      <td class="num" data-frames="${r.id}">${counts(r)}</td>
      <td class="muted">${esc((r.created || "").replace("T", " ").slice(0, 19))}</td>
      <td>${TERMINAL.has(r.state) ? (r.summary?.error ? `<span class="error">${esc(r.summary.error)}</span>` : "")
             : r.can_cancel ? `<button data-cancel="${r.id}">Cancel</button>` : ""}</td></tr>`).join("");
    runs.filter((r) => r.state === "running").forEach(follow);
    clearTimeout(timer);
    if (runs.some((r) => !TERMINAL.has(r.state))) timer = setTimeout(refresh, 2000);
  }
  await refresh();
}
