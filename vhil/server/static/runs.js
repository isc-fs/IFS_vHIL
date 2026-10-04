// Runs page (M5.2, #114): start a `run` scenario, list runs, and count each
// running run's frames per bus live over its WebSocket. Rich inspection of a
// run (frame table, plots, JUnit) is #115.
const TERMINAL = new Set(["passed", "failed", "error", "cancelled"]);
const live = new Map();   // run id -> {ws, counts: {bus: n}}

export async function renderRuns(view, { api, esc }) {
  const systems = await api("/api/systems");
  view.innerHTML = `<h2>Runs</h2>
    <form id="run-form">
      <label>System <select name="system">${
        systems.map((s) => `<option value="${esc(s.id)}">${esc(s.id)}</option>`).join("")}</select></label>
      <label>Virtual ms <input name="virtual_ms" type="number" value="1000" min="1" max="600000" required></label>
      <button type="submit">Start run</button>
      <span id="run-msg" class="muted"></span>
    </form>
    <table id="runs"><thead><tr><th>Run</th><th>State</th><th>System</th><th>Scenario</th>
      <th>Virtual time</th><th>Frames per bus</th><th>Created</th><th></th></tr></thead><tbody></tbody></table>`;
  const table = view.querySelector("#runs tbody");
  const msg = view.querySelector("#run-msg");
  const mounted = () => document.body.contains(table);

  view.querySelector("#run-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = new FormData(ev.target);
    const body = { system: f.get("system"),
                   scenario: { kind: "run", virtual_ms: Number(f.get("virtual_ms")) } };
    const r = await fetch("/api/runs", { method: "POST", headers: { "content-type": "application/json" },
                                         body: JSON.stringify(body) });
    const out = await r.json();
    msg.textContent = r.ok ? `queued run ${out.run_id}` : `error: ${JSON.stringify(out.detail)}`;
    msg.className = r.ok ? "muted" : "error";
    refresh();
  });

  table.addEventListener("click", async (ev) => {
    const id = ev.target.dataset.cancel;
    if (id) { await fetch(`/api/runs/${id}/cancel`, { method: "POST" }); refresh(); }
  });

  const counts = (run) => {
    const c = live.get(run.id)?.counts || run.summary?.frames || {};
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
      entry.counts[rec.bus] = (entry.counts[rec.bus] || 0) + 1;
      const cell = table.querySelector(`[data-frames="${run.id}"]`);
      if (cell) cell.textContent = counts(run);
    };
    ws.onclose = () => { if (live.get(run.id) === entry && !mounted()) live.delete(run.id); };
  }

  let timer = null;
  async function refresh() {
    if (!mounted()) return;
    const runs = await api("/api/runs?limit=50");
    table.innerHTML = runs.map((r) => `<tr>
      <td>${r.id}</td><td class="${r.state === "error" || r.state === "failed" ? "error" : ""}">${esc(r.state)}</td>
      <td>${esc(r.system)}</td>
      <td class="muted">${esc(r.scenario.kind === "run" ? `run ${r.scenario.virtual_ms} ms` : `pytest ${r.scenario.select}`)}</td>
      <td>${(r.virtual_us / 1000).toFixed(0)} ms</td>
      <td data-frames="${r.id}">${counts(r)}</td>
      <td class="muted">${esc((r.created || "").replace("T", " ").slice(0, 19))}</td>
      <td>${TERMINAL.has(r.state) ? (r.summary?.error ? `<span class="error">${esc(r.summary.error)}</span>` : "")
             : `<button data-cancel="${r.id}">Cancel</button>`}</td></tr>`).join("");
    runs.filter((r) => r.state === "running").forEach(follow);
    clearTimeout(timer);
    if (runs.some((r) => !TERMINAL.has(r.state))) timer = setTimeout(refresh, 2000);
  }
  await refresh();
}
