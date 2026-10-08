// Time plots over virtual time with the vendored uPlot (static/vendor/uplot,
// 1.6.32), for the editor workspace's Signals tab (its build copies this file
// and uPlot into src/vhil/shell/: docker/editor.Dockerfile). One plot per
// series, stacked, in a group that shares the x range (zoom/pan one, all
// follow), the hover cursor, and a marker at the time selected anywhere in
// the workspace (the scrubber, a click on a plot).
//
//   drag: zoom to a range · ctrl/cmd-wheel or pinch: zoom around the cursor
//   shift-drag: pan
//   double-click: whole run · click: select that time
import uPlot from "./vendor/uplot/uPlot.esm.js";

let cssLoaded = false;
// The page bundles uPlot's sheet itself (the editor's build): no <link>.
export function cssProvided() { cssLoaded = true; }
function loadCss() {
  if (cssLoaded) return;
  cssLoaded = true;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = "/static/vendor/uplot/uPlot.min.css";
  document.head.appendChild(link);
}

const token = (name, fallback) =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;

export const palette = () => [1, 2, 3, 4, 5, 6].map((i) => token(`--s${i}`, "#3366cc"));

export class PlotGroup {
  // onPick(t_us): a click on a plot; the group's marker moves there too.
  constructor({ onPick = null } = {}) {
    this.plots = [];
    this.key = `g${Math.random().toString(36).slice(2)}`;
    this.markerS = null;
    this.range = null;          // [min_s, max_s] shared by every plot, null = auto
    this.onPick = onPick;
    this._syncing = false;
  }

  setMarker(tUs) {
    this.markerS = tUs === null ? null : tUs / 1e6;
    for (const u of this.plots) u.redraw(false, false);
  }

  setRange(range) {
    this.range = range;
    let [min, max] = range || [Infinity, -Infinity];
    if (!range) {
      for (const u of this.plots) {
        const x = u.data[0];
        if (x.length) { min = Math.min(min, x[0]); max = Math.max(max, x[x.length - 1]); }
      }
      if (!(max > min)) [min, max] = Number.isFinite(min) ? [min - 0.5, min + 0.5] : [0, 1];
    }
    this._syncing = true;
    for (const u of this.plots) u.setScale("x", { min, max });
    this._syncing = false;
  }

  destroy() {
    for (const u of this.plots) u.destroy();
    this.plots = [];
  }

  // A plot of one series {x: [t_s], y: [v]} in `el`. `format(v)`: how a
  // value reads (an enum's label), on the y axis at `levels` and in the legend.
  add(el, { label, unit = "", x, y, stepped = true, color = palette()[0], height = 140,
            format = null, levels = null }) {
    loadCss();
    const group = this;
    const fg = token("--fg", "#222"), muted = token("--fg-muted", "#888"), line = token("--line", "#ddd");
    const axis = { stroke: muted, grid: { stroke: line, width: 1 }, ticks: { stroke: line } };
    // Labelled levels (sorted): the y axis holds them all, ticked at each.
    const stepLevels = format && levels?.length ? levels : null;
    const marker = {
      hooks: {
        draw: [(u) => {
          if (group.markerS === null) return;
          const px = u.valToPos(group.markerS, "x", true);
          if (px < u.bbox.left || px > u.bbox.left + u.bbox.width) return;
          const ctx = u.ctx;
          ctx.save();
          ctx.strokeStyle = token("--focus", "#4c8dff");
          ctx.lineWidth = 1.5 * devicePixelRatio;
          ctx.beginPath();
          ctx.moveTo(px, u.bbox.top);
          ctx.lineTo(px, u.bbox.top + u.bbox.height);
          ctx.stroke();
          ctx.restore();
        }],
        setScale: [(u, key) => {
          if (key !== "x" || group._syncing) return;
          group._syncing = true;
          const { min, max } = u.scales.x;
          group.range = [min, max];
          for (const o of group.plots) if (o !== u) o.setScale("x", { min, max });
          group._syncing = false;
        }],
      },
    };
    const opts = {
      width: Math.max(200, el.clientWidth || 600),
      height,
      legend: { show: true, live: true },
      cursor: { sync: { key: this.key, setSeries: false }, drag: { x: true, y: false },
                bind: { dblclick: () => null } },   // ours: reset the whole group
      scales: { x: { time: false },
                ...(stepLevels ? { y: { range: [levels[0] - 0.5, levels[levels.length - 1] + 0.5] } } : {}) },
      axes: [
        { ...axis, label: "virtual time (s)", labelSize: 18, size: 36, font: "11px system-ui", labelFont: "11px system-ui", stroke: muted },
        { ...axis, size: format ? 96 : 60, font: "11px system-ui", stroke: muted,
          ...(format ? { values: (u, splits) => splits.map((v) => format(v)) } : {}),
          ...(stepLevels ? { splits: () => levels } : {}) },
      ],
      series: [
        { label: "t (s)", value: (u, v) => (v == null ? "–" : v.toFixed(6)) },
        { label: unit ? `${label} [${unit}]` : label, stroke: color, width: 1.5,
          ...(format ? { value: (u, v) => (v == null ? "–" : format(v)) } : {}),
          fill: color + "18", spanGaps: true, points: { show: x.length < 200, size: 4 },
          paths: stepped ? uPlot.paths.stepped({ align: 1 }) : undefined },
      ],
      plugins: [marker],
    };
    el.innerHTML = "";
    el.style.color = fg;
    const u = new uPlot(opts, [x, y], el);
    this.plots.push(u);
    if (this.range) {
      this._syncing = true;
      u.setScale("x", { min: this.range[0], max: this.range[1] });
      this._syncing = false;
    }
    this._interact(u);
    new ResizeObserver(() => {
      if (el.clientWidth && Math.abs(el.clientWidth - u.width) > 2) u.setSize({ width: el.clientWidth, height });
    }).observe(el);
    return u;
  }

  _interact(u) {
    const over = u.over;
    let down = null;
    over.addEventListener("wheel", (ev) => {
      // Ctrl/Cmd (a trackpad pinch sends ctrlKey): a bare wheel scrolls the page.
      if (!ev.ctrlKey && !ev.metaKey) return;
      ev.preventDefault();
      const { min, max } = u.scales.x;
      const at = u.posToVal(ev.offsetX, "x");
      const k = ev.deltaY < 0 ? 0.8 : 1.25;
      this.setRange([at - (at - min) * k, at + (max - at) * k]);
    }, { passive: false });
    over.addEventListener("mousedown", (ev) => {
      down = { x: ev.clientX, y: ev.clientY };
      if (!ev.shiftKey) return;
      ev.stopPropagation();           // no drag-zoom while panning
      const { min, max } = u.scales.x;
      const perPx = (max - min) / u.bbox.width * devicePixelRatio;
      const x0 = ev.clientX;
      const move = (e) => this.setRange([min - (e.clientX - x0) * perPx, max - (e.clientX - x0) * perPx]);
      const up = () => { removeEventListener("mousemove", move); removeEventListener("mouseup", up); };
      addEventListener("mousemove", move);
      addEventListener("mouseup", up);
    }, true);
    over.addEventListener("click", (ev) => {
      if (!down || Math.abs(ev.clientX - down.x) > 3 || Math.abs(ev.clientY - down.y) > 3) return;
      const t = u.posToVal(ev.offsetX, "x");
      this.setMarker(t * 1e6);
      if (this.onPick) this.onPick(Math.round(t * 1e6));
    });
    over.addEventListener("dblclick", () => this.setRange(null));
  }
}
