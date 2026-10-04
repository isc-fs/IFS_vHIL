// The Editor page (M5.4, #116): Pipeline Manager in an iframe, driven over its
// postMessage JSON-RPC (graph_change / graph_get / properties_change), with a
// firmware picker, a Save form (commit on a branch) and Open PR.
// The system file stays the source of truth: the graph is translated to and
// from it by the API (vhil.editor), never kept here.

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
    throw Object.assign(new Error(errors.join("; ")), { errors, status: r.status });
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
const today = () => new Date().toISOString().slice(0, 10).replace(/-/g, "");

export async function editorPage(view, initialId) {
  const [config, systems, firmware] = await Promise.all([
    call("GET", "/api/config"), call("GET", "/api/systems"), call("GET", "/api/firmware")]);
  const fwById = Object.fromEntries(firmware.map((f) => [f.id, f]));
  const editorUrl = config.editor_url;
  const origin = new URL(editorUrl, location.href).origin;
  const state = { id: null, isNew: false, savedBranch: null };

  view.innerHTML = `
    <h2>Editor</h2>
    <div class="ed-bar">
      <label>System <select id="ed-system">${systems.map((s) =>
        `<option value="${esc(s.id)}">${esc(s.id)}</option>`).join("")}</select></label>
      <label>from branch <input id="ed-from" placeholder="checked-out tree" size="18"></label>
      <button id="ed-open">Open</button>
      <button id="ed-new" type="button">New…</button>
      <span id="ed-state" class="muted"></span>
    </div>
    <div class="ed-grid">
      <iframe id="ed-frame" title="System editor (Pipeline Manager)" src="${esc(editorUrl)}"></iframe>
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
    Object.assign(state, { id, isNew: !s.exists, savedBranch: null,
      extra: s.dataflow.graphs[0].additionalData?.vhil || null });
    saveForm.elements.branch.value ||= branch || `feat/system-${id}-${today()}`;
    saveForm.elements.message.value = s.exists ? `feat(systems): update ${id}` : `feat(systems): add ${id}`;
    prForm.elements.title.value = saveForm.elements.message.value;
    setState(`${id}${s.exists ? "" : " (new)"} · ${branch || "checked-out tree"} @ ${(s.ref || "").slice(0, 8)}`);
    if (s.errors.length) showErrors(`${id} does not validate as it is:`, s.errors);
    await refreshFirmware();
  }

  async function refreshFirmware() {
    const box = $("#ed-fw");
    let graph;
    try { graph = await currentGraph(); } catch (e) { box.textContent = e.message; return; }
    const g = graph.graphs.find((x) => x.id === graph.entryGraph) || graph.graphs[0];
    const boards = g.nodes.filter((n) => n.properties?.some((p) => p.name === "firmware"));
    if (!boards.length) { box.textContent = "No boards in the graph."; return; }
    box.classList.remove("muted");
    box.innerHTML = "";
    for (const node of boards) {
      const prop = (name) => node.properties.find((p) => p.name === name)?.value ?? "";
      for (const [fwProp, refProp] of [["firmware", "firmware_ref"], ["bootloader", "bootloader_ref"]]) {
        const fw = fwById[prop(fwProp)];
        if (!fw) continue;
        const row = document.createElement("label");
        row.className = "ed-fw-row";
        row.innerHTML = `<span><strong>${esc(node.instanceName || node.id)}</strong> ${esc(fwProp)}
          <span class="muted">${esc(fw.id)} · ${esc(fw.repo)}</span></span>`;
        const sel = document.createElement("select");
        const current = prop(refProp);
        sel.innerHTML = `<option value="">catalogue: ${esc(fw.ref)}</option>${
          current ? `<option value="${esc(current)}" selected>${esc(current)}</option>` : ""}`;
        sel.addEventListener("focus", async () => {
          if (sel.dataset.loaded) return;
          sel.dataset.loaded = "1";
          try {
            const refs = await call("GET", `/api/firmware/${encodeURIComponent(fw.id)}/refs`);
            const opt = (r) => `<option value="${esc(r)}" ${r === current ? "selected" : ""}>${esc(r)}</option>`;
            sel.innerHTML = `<option value="">catalogue: ${esc(fw.ref)}</option>
              <optgroup label="branches">${refs.branches.map((r) => opt(r)).join("")}</optgroup>
              <optgroup label="tags">${refs.tags.map((r) => opt(r)).join("")}</optgroup>`;
            if (current && ![...refs.branches, ...refs.tags].includes(current)) {
              sel.insertAdjacentHTML("beforeend", `<option value="${esc(current)}" selected>${esc(current)} (not found)</option>`);
            }
          } catch (e) { showErrors(`refs of ${fw.repo}`, e.errors || [e.message]); }
        });
        sel.addEventListener("change", async () => {
          try {
            await rpc("properties_change", { graph_id: g.id, node_id: node.id,
              properties: [{ name: refProp, new_value: sel.value }] });
            show(`${esc(node.instanceName)} ${esc(fwProp)} → ${esc(sel.value || `catalogue (${fw.ref})`)}. Save to keep it.`);
          } catch (e) { showErrors("could not set the ref in the editor", [e.message]); }
        });
        row.append(sel);
        box.append(row);
      }
    }
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

  $("#ed-check").addEventListener("click", () => guarded(async () => {
    if (!state.id) throw new Error("open a system first");
    const p = await call("POST", `/api/systems/${encodeURIComponent(state.id)}/preview`, { dataflow: await currentGraph() });
    const yaml = `<details><summary>systems/${esc(state.id)}.yaml</summary><pre>${esc(p.yaml)}</pre></details>`;
    if (p.errors.length) show(`<strong>Not valid:</strong><ul>${p.errors.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>${yaml}`, "error");
    else show(`Valid. ${yaml}`);
  }));

  saveForm.addEventListener("submit", (ev) => { ev.preventDefault(); guarded(async () => {
    if (!state.id) throw new Error("open a system first");
    const body = { dataflow: await currentGraph(), branch: saveForm.elements.branch.value.trim(),
      message: saveForm.elements.message.value };
    const out = state.isNew
      ? await call("POST", "/api/systems", { id: state.id, ...body })
      : await call("PUT", `/api/systems/${encodeURIComponent(state.id)}`, body);
    state.isNew = false;
    if (out.changed) {
      state.savedBranch = out.branch;
      show(`Saved <code>${esc(out.path)}</code> on <code>${esc(out.branch)}</code> @ <code>${esc(out.ref.slice(0, 8))}</code>.`);
    } else {
      show(`Nothing to save: identical to <code>${esc(out.ref.slice(0, 8))}</code>.`);
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
