// The Editor page (M5.4, #116): Pipeline Manager in an iframe, driven over its
// postMessage JSON-RPC (graph_change / graph_get / properties_change), with a
// firmware picker, a Save form (commit on a branch), Open PR, and Run: a
// normal run of the saved system through POST /api/runs (editor-run.js),
// followed over its live WebSocket, its state written to the editor's
// terminal and notifications.
// The system file stays the source of truth: the graph is translated to and
// from it by the API (vhil.editor), never kept here.

import { frameCounts, runBlocker, runRequest } from "./editor-run.js";

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

const cookie = (name) => document.cookie.split("; ").find((c) => c.startsWith(`${name}=`))?.slice(name.length + 1);

async function call(method, path, body) {
  const headers = body ? { "Content-Type": "application/json" } : {};
  // The session's CSRF token on writes, as app.js's api() (vhil/server/auth.py).
  const csrf = cookie("vhil_csrf");
  if (method !== "GET" && csrf) headers["X-CSRF-Token"] = decodeURIComponent(csrf);
  const r = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  if (r.status === 401) {
    location.href = `/auth/login?next=${encodeURIComponent(location.pathname + location.hash)}`;
    throw new Error("login required");
  }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const d = data.detail;
    const errors = d?.errors ?? (Array.isArray(d) ? d.map((e) => `${e.loc.join(".")}: ${e.msg}`)
      : [typeof d === "string" ? d : `${r.status} ${r.statusText}`]);
    throw Object.assign(new Error(errors.join("; ")), { errors, status: r.status, detail: d });
  }
  return data;
}

// JSON-RPC to Pipeline Manager's frontend in the iframe.
function rpcClient(frame, origin) {
  let seq = 0;
  return (method, params = {}, timeoutMs = 5000) => new Promise((resolve, reject) => {
    const id = `vhil-${Date.now()}-${++seq}`;
    const done = (fn, v) => { window.removeEventListener("message", onMessage); clearTimeout(timer); fn(v); };
    const onMessage = (ev) => {
      if (ev.source !== frame.contentWindow || ev.origin !== origin || ev.data?.id !== id) return;
      if (ev.data.error) done(reject, new Error(ev.data.error.message || "editor error"));
      else done(resolve, ev.data.result);
    };
    const timer = setTimeout(() => done(reject, new Error(`the editor did not answer ${method}`)), timeoutMs);
    window.addEventListener("message", onMessage);
    frame.contentWindow.postMessage({ jsonrpc: "2.0", id, method, params }, origin);
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// The shell's theme, as the editor takes it (?theme= and vhil_set_theme,
// editor/pipeline-manager/CHANGELOG-VHIL.md): what data-theme on <html> forces,
// else "auto", which follows prefers-color-scheme as tokens.css does.
const shellTheme = () => document.documentElement.dataset.theme || "auto";
function themedUrl(url) {
  const u = new URL(url, location.href);
  u.searchParams.set("theme", shellTheme());
  return u.href;
}

// What `vhil.system validate` warns of (a pin the role's backplane leaves
// unconnected): shown, but it doesn't stop a save.
const warningList = (warnings) => warnings?.length
  ? `<div class="warning"><strong>Warnings:</strong><ul>${warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>`
  : "";
const today = () => new Date().toISOString().slice(0, 10).replace(/-/g, "");

export async function editorPage(view, initialId) {
  const [config, systems, firmware] = await Promise.all([
    call("GET", "/api/config"), call("GET", "/api/systems"), call("GET", "/api/firmware")]);
  const fwById = Object.fromEntries(firmware.map((f) => [f.id, f]));
  const editorUrl = config.editor_url;
  const origin = new URL(editorUrl, location.href).origin;
  // saved: the system file the graph gave when opened or last saved (Run
  // refuses while it gives another); runRef: the commit that file is at
  // ("" for the checked-out tree), which Run runs.
  const state = { id: null, isNew: false, savedBranch: null, saved: null, runRef: "" };

  view.innerHTML = `
    <h2>Editor</h2>
    <div class="ed-bar">
      <label>System <select id="ed-system">${systems.map((s) =>
        `<option value="${esc(s.id)}">${esc(s.id)}</option>`).join("")}</select></label>
      <label>from branch <input id="ed-from" placeholder="checked-out tree" size="18"></label>
      <button id="ed-open">Open</button>
      <button id="ed-new" type="button">New…</button>
      <span id="ed-state" class="muted"></span>
      <label>Virtual ms <input id="ed-ms" type="number" min="1" max="600000" step="100" size="7"
        value="${Number(config.run_virtual_ms)}"
        title="Virtual time from power-on; each board spends its bootloader's 2 s first"></label>
      <button id="ed-run" type="button" title="Run the saved system as a normal run (Runs page)">Run</button>
      <span id="ed-run-state" aria-live="polite"></span>
    </div>
    <div class="ed-grid">
      <iframe id="ed-frame" title="System editor (Pipeline Manager)" src="${esc(themedUrl(editorUrl))}"></iframe>
      <aside>
        <section>
          <h3>Firmware <button id="ed-fw-refresh" type="button" class="link">refresh</button></h3>
          <div id="ed-fw" class="muted">Open a system.</div>
        </section>
        <section>
          <h3>Save</h3>
          <form id="ed-save">
            <label>Branch <input name="branch" required placeholder="feat/…"></label>
            <label>Message <textarea name="message" rows="3" required></textarea></label>
            <div class="row"><button type="button" id="ed-check">Check</button>
              <button type="submit">Save to branch</button></div>
          </form>
        </section>
        <section>
          <h3>Pull request to ${esc(config.base_branch)}</h3>
          <form id="ed-pr">
            <label>Title <input name="title" required></label>
            <label>Body <textarea name="body" rows="3"></textarea></label>
            <button type="submit" ${config.can_open_pr ? "" : "disabled"}>Open PR</button>
            ${config.can_open_pr ? "" : `<p class="muted">No GitHub credentials on the server
              (VHIL_GITHUB_TOKEN; the GitHub App comes with M5.5). Saved branches stay in the
              workspace: push and open the PR by hand.</p>`}
          </form>
        </section>
        <div id="ed-msg" aria-live="polite"></div>
      </aside>
    </div>
    <p class="muted">Editor at <a href="${esc(editorUrl)}" target="_blank" rel="noopener">${esc(editorUrl)}</a>
      (<code>scripts/vhil-docker.sh editor</code>). Changes live in the graph until saved.</p>`;

  const $ = (sel) => view.querySelector(sel);
  const frame = $("#ed-frame");
  const rpc = rpcClient(frame, origin);
  const saveForm = $("#ed-save"), prForm = $("#ed-pr");
  const frameLoaded = new Promise((r) => frame.addEventListener("load", r, { once: true }));
  // The editor switches theme with the shell. The observer ends with the page.
  const themeObserver = new MutationObserver(() => {
    if (!frame.isConnected) { themeObserver.disconnect(); return; }
    rpc("vhil_set_theme", { theme: shellTheme() }).catch(() => {});
  });
  themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

  const show = (html, cls = "") => { $("#ed-msg").innerHTML = html ? `<div class="${cls}">${html}</div>` : ""; };
  const showErrors = (title, errors) => show(`<strong>${esc(title)}</strong><ul>${
    errors.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>`, "error");
  const setState = (t) => { $("#ed-state").textContent = t; };

  // Pipeline Manager takes a graph only once its specification has come from
  // the backend (before that, every node is "unknown" and graph_change
  // reports nothing back): wait for the node types, then load and check.
  async function loadGraph(dataflow) {
    await frameLoaded;
    const want = dataflow.graphs[0].nodes.length;
    let last;
    for (let i = 0; i < 30; i++) {
      try {
        const { specification } = await rpc("frontend_specification_get", {}, 3000);
        if (specification?.nodes?.length) {
          await rpc("graph_change", { dataflow, loadingScreen: false }, 5000);
          const got = await currentGraph();
          const g = got.graphs.find((x) => x.id === got.entryGraph) || got.graphs[0];
          if (g.nodes.length === want) return;
          last = new Error(`the editor took ${g.nodes.length} of ${want} nodes`);
        } else last = new Error("no specification from the backend yet");
      } catch (e) { last = e; }
      await sleep(1000);
    }
    throw new Error(`could not load the graph into the editor (${last?.message}). Is it running at ${editorUrl}?`);
  }

  // Pipeline Manager drops the graph's additionalData (the file's id,
  // description, bench wiring and source text): put back what was opened.
  async function currentGraph() {
    const { dataflow } = await rpc("graph_get");
    const g = dataflow.graphs.find((x) => x.id === dataflow.entryGraph) || dataflow.graphs[0];
    if (state.extra && !g.additionalData?.vhil) g.additionalData = { ...(g.additionalData || {}), vhil: state.extra };
    return dataflow;
  }

  async function open(id, { branch = "", isNew = false } = {}) {
    setState(`opening ${id}…`);
    show("");
    const q = new URLSearchParams();
    if (branch) q.set("branch", branch);
    if (isNew) q.set("new", "true");
    const s = await call("GET", `/api/systems/${encodeURIComponent(id)}/dataflow?${q}`);
    await loadGraph(s.dataflow);
    Object.assign(state, { id, isNew: !s.exists, savedBranch: null, saved: null,
      runRef: branch ? s.ref : "", extra: s.dataflow.graphs[0].additionalData?.vhil || null });
    if (s.exists) state.saved = await previewYaml();
    saveForm.elements.branch.value ||= branch || `feat/system-${id}-${today()}`;
    saveForm.elements.message.value = s.exists ? `feat(systems): update ${id}` : `feat(systems): add ${id}`;
    prForm.elements.title.value = saveForm.elements.message.value;
    setState(`${id}${s.exists ? "" : " (new)"} · ${branch || "checked-out tree"} @ ${(s.ref || "").slice(0, 8)}`);
    if (s.errors.length) showErrors(`${id} does not validate as it is:`, s.errors);
    else if (s.warnings?.length) show(`${esc(id)} is valid.${warningList(s.warnings)}`);
    await refreshFirmware();
  }

  // One refs request per firmware per panel refresh (the API caches ls-remote).
  const refsCache = new Map();
  const refsOf = (id) => {
    if (!refsCache.has(id)) refsCache.set(id, call("GET", `/api/firmware/${encodeURIComponent(id)}/refs`));
    return refsCache.get(id);
  };

  // The firmware panel: per board, the app's branch or tag and the
  // bootloader's tag. The role sets which firmware (a node property the
  // editor shows read-only); the panel picks only refs. A ref equal to the
  // catalogue's is written as no ref, so the file keeps no firmware_ref.
  async function refreshFirmware() {
    const box = $("#ed-fw");
    refsCache.clear();
    let graph;
    try { graph = await currentGraph(); } catch (e) { box.textContent = e.message; return; }
    const g = graph.graphs.find((x) => x.id === graph.entryGraph) || graph.graphs[0];
    const boards = g.nodes.filter((n) => n.properties?.some((p) => p.name === "firmware"));
    if (!boards.length) { box.textContent = "No boards in the graph."; return; }
    box.classList.remove("muted");
    box.innerHTML = "";
    for (const node of boards) {
      const prop = (name) => node.properties.find((p) => p.name === name)?.value ?? "";
      const name = node.instanceName || node.id;
      for (const [what, fwProp, refProp, kinds] of [
        ["app", "firmware", "firmware_ref", ["branches", "tags"]],
        ["bootloader", "bootloader", "bootloader_ref", ["tags"]]]) {
        // "udv (not in the catalogue yet)" -> udv: the role names it all the same.
        const fwId = String(prop(fwProp)).split(" ")[0];
        if (!fwId) continue;
        const fw = fwById[fwId];
        const row = document.createElement("div");
        row.className = "ed-fw-row";
        const sel = document.createElement("select");
        sel.setAttribute("aria-label", `${name} ${what} ref`);
        const info = document.createElement("span");
        info.className = "muted ed-fw-info";
        row.innerHTML = `<span><strong>${esc(name)}</strong> ${esc(what)}
          <span class="muted">${esc(fwId)}${fw ? ` · ${esc(fw.repo)}` : ""}</span></span>`;
        row.append(sel, info);
        box.append(row);
        if (!fw) {
          // The role names a firmware the catalogue lacks (the uDV's).
          sel.disabled = true;
          sel.innerHTML = `<option>none</option>`;
          info.textContent = `No ${fwId} firmware in the catalogue yet: this role can't run.`;
          continue;
        }
        const current = prop(refProp);
        sel.innerHTML = `<option value="">${esc(fw.ref)} (catalogue)</option>${
          current ? `<option value="${esc(current)}" selected>${esc(current)}</option>` : ""}`;
        let byName = new Map();
        const describe = () => {
          const r = byName.get(sel.value || fw.ref);
          if (!r) { info.textContent = sel.value ? "not on the remote" : ""; return; }
          info.innerHTML = `<code title="${esc(r.sha)}">${esc(r.sha.slice(0, 8))}</code>${
            r.built ? "" : ' <span class="ed-fw-unbuilt">not built yet — the first run builds it</span>'}`;
        };
        refsOf(fw.id).then((refs) => {
          byName = new Map(kinds.flatMap((k) => refs[k]).map((r) => [r.name, r]));
          const opt = (r) => r.name === fw.ref ? "" :
            `<option value="${esc(r.name)}" ${r.name === current ? "selected" : ""}>${esc(r.name)}</option>`;
          sel.innerHTML = `<option value="">${esc(fw.ref)} (catalogue)</option>${kinds.map((k) =>
            `<optgroup label="${k}">${refs[k].map(opt).join("")}</optgroup>`).join("")}`;
          if (current && current !== fw.ref && !byName.has(current)) {
            sel.insertAdjacentHTML("beforeend",
              `<option value="${esc(current)}" selected>${esc(current)} (not found)</option>`);
          }
          describe();
        }).catch((e) => { info.textContent = `refs of ${fw.repo}: ${e.message}`; });
        sel.addEventListener("change", async () => {
          describe();
          try {
            await rpc("properties_change", { graph_id: g.id, node_id: node.id,
              properties: [{ name: refProp, new_value: sel.value }] });
            show(`${esc(name)} ${esc(what)} → ${esc(sel.value || `${fw.ref} (catalogue)`)}. Save to keep it.`);
          } catch (e) { showErrors("could not set the ref in the editor", [e.message]); }
        });
      }
    }
  }

  // The system file the graph in the editor gives (as Check shows it).
  async function previewYaml() {
    const p = await call("POST", `/api/systems/${encodeURIComponent(state.id)}/preview`,
      { dataflow: await currentGraph() });
    return p.yaml;
  }

  // Run's state, in the editor's terminal and notifications and next to
  // the button, with a link to the run's page.
  const term = (line) => rpc("terminal_write", { name: "Terminal", message: line }).catch(() => {});
  const notify = (type, title, details) =>
    rpc("notification_send", { type, title, details }).catch(() => {});
  const runState = (html) => { $("#ed-run-state").innerHTML = html; };

  async function run() {
    const virtualMs = Number($("#ed-ms").value);
    let dataflow = null, current = null;
    if (state.id && !state.isNew) {
      dataflow = await currentGraph();
      current = await previewYaml();
    }
    const why = runBlocker({ id: state.id, isNew: state.isNew, saved: state.saved, current, virtualMs });
    if (why) {
      show(esc(why), "error");
      notify("warning", "Not run", why);
      return;
    }
    const body = runRequest({ system: state.id, ref: state.runRef, dataflow, virtualMs });
    let out;
    try {
      out = await call("POST", "/api/runs", body);
    } catch (e) {
      notify("error", "Run refused", e.message);
      term(`run refused: ${e.message}`);
      throw e;
    }
    follow(out.run_id, body);
  }

  // Follow a run over /api/runs/{id}/live until it ends: frames counted per
  // bus (not the scenario's own), its log lines into the terminal.
  function follow(id, body) {
    const page = `#/runs/${id}`;
    const link = `<a href="${page}">run ${id}</a>`;
    const at = body.ref ? ` @ ${body.ref.slice(0, 8)}` : "";
    const fw = Object.entries(body.firmware).map(([k, v]) => `${k}=${v}`).join(", ");
    const what = `${body.system}${at}, ${body.scenario.virtual_ms} ms${fw ? `, ${fw}` : ""}`;
    $("#ed-run").disabled = true;
    runState(`${link} <span class="badge state-queued">queued</span>`);
    term(`run ${id} queued: ${what} (${location.origin}/${page})`);
    notify("info", `Run ${id} queued`, what);
    const counts = {};
    let started = false, ended = false, painted = 0;
    const paint = (st, force = false) => {
      if (!force && Date.now() - painted < 500) return;
      painted = Date.now();
      runState(`${link} <span class="badge state-${esc(st)}">${esc(st)}</span>
        <span class="muted">${esc(frameCounts(counts))}</span>`);
    };
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/api/runs/${id}/live?kinds=frame,log`);
    ws.onmessage = (ev) => {
      if (!frame.isConnected) { ws.close(); return; }
      const rec = JSON.parse(ev.data);
      if (rec.kind === "end") {
        ended = true;
        $("#ed-run").disabled = false;
        paint(rec.state || "unknown", true);
        term(`run ${id} ${rec.state}: ${frameCounts(counts)}`);
        notify(rec.state === "passed" ? "info" : rec.state === "cancelled" ? "warning" : "error",
          `Run ${id} ${rec.state}`, `${frameCounts(counts)}. ${location.origin}/${page}`);
        return;
      }
      if (!started) { started = true; term(`run ${id} running`); paint("running", true); }
      if (rec.kind === "log") term(`  ${(rec.t_us / 1e6).toFixed(3)} s  ${rec.text}`);
      else if (!rec.src) counts[rec.bus] = (counts[rec.bus] || 0) + 1;
      paint("running");
    };
    ws.onclose = () => {
      if (ended || !frame.isConnected) return;
      $("#ed-run").disabled = false;
      runState(`${link} <span class="muted">live view lost: see its page</span>`);
    };
  }

  async function guarded(fn) {
    try { await fn(); } catch (e) { showErrors(e.status === 422 ? "Not valid:" : "Failed:", e.errors || [e.message]); }
  }

  $("#ed-open").addEventListener("click", () => guarded(() => open($("#ed-system").value, { branch: $("#ed-from").value.trim() })));
  $("#ed-new").addEventListener("click", () => guarded(async () => {
    const id = prompt("New system id (lowercase letters, digits, '-'):");
    if (!id) return;
    if (systems.some((s) => s.id === id)) throw new Error(`'${id}' exists: open it instead`);
    saveForm.elements.branch.value = "";
    await open(id, { isNew: true });
  }));
  $("#ed-fw-refresh").addEventListener("click", () => guarded(refreshFirmware));
  $("#ed-run").addEventListener("click", () => guarded(run));

  $("#ed-check").addEventListener("click", () => guarded(async () => {
    if (!state.id) throw new Error("open a system first");
    const p = await call("POST", `/api/systems/${encodeURIComponent(state.id)}/preview`, { dataflow: await currentGraph() });
    const yaml = `<details><summary>systems/${esc(state.id)}.yaml</summary><pre>${esc(p.yaml)}</pre></details>`;
    if (p.errors.length) show(`<strong>Not valid:</strong><ul>${p.errors.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>${yaml}`, "error");
    else show(`Valid.${warningList(p.warnings)} ${yaml}`);
  }));

  saveForm.addEventListener("submit", (ev) => { ev.preventDefault(); guarded(async () => {
    if (!state.id) throw new Error("open a system first");
    const body = { dataflow: await currentGraph(), branch: saveForm.elements.branch.value.trim(),
      message: saveForm.elements.message.value };
    const send = (b) => state.isNew
      ? call("POST", "/api/systems", { id: state.id, ...b })
      : call("PUT", `/api/systems/${encodeURIComponent(state.id)}`, b);
    let out;
    try {
      out = await send(body);
    } catch (e) {
      // Another member saved this branch last (vhil/server/systems_write.py):
      // moving it is a takeover, which the server logs.
      if (e.status !== 409 || !e.detail?.takeover) throw e;
      if (!confirm(`${e.message}\n\nTake the branch over? This is logged.`)) throw e;
      out = await send({ ...body, takeover: true });
    }
    state.isNew = false;
    Object.assign(state, { runRef: out.ref, saved: await previewYaml() });
    if (out.changed) {
      state.savedBranch = out.branch;
      show(`Saved <code>${esc(out.path)}</code> on <code>${esc(out.branch)}</code> @ <code>${esc(out.ref.slice(0, 8))}</code>.${warningList(out.warnings)}`);
    } else {
      show(`Nothing to save: identical to <code>${esc(out.ref.slice(0, 8))}</code>.${warningList(out.warnings)}`);
    }
  }); });

  prForm.addEventListener("submit", (ev) => { ev.preventDefault(); guarded(async () => {
    if (!state.id) throw new Error("open a system first");
    const branch = state.savedBranch || saveForm.elements.branch.value.trim();
    const out = await call("POST", `/api/systems/${encodeURIComponent(state.id)}/pr`,
      { branch, title: prForm.elements.title.value, body: prForm.elements.body.value });
    show(`PR: <a href="${esc(out.url)}" target="_blank" rel="noopener">${esc(out.url)}</a>`);
  }); });

  if (initialId) {
    $("#ed-system").value = initialId;
    await guarded(() => open(initialId));
  }
}
