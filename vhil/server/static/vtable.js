// A virtualised list: thousands of rows, only the visible window (plus a
// little overscan) in the DOM. Rows have a fixed height. `windowFor` is the
// pure part (tests/js/vtable.test.mjs).

export function windowFor(scrollTop, viewportHeight, rowHeight, count, overscan = 10) {
  const first = Math.floor(Math.max(0, scrollTop) / rowHeight);
  const visible = Math.ceil(viewportHeight / rowHeight) + 1;
  const start = Math.max(0, Math.min(count, first - overscan));
  const end = Math.max(start, Math.min(count, first + visible + overscan));
  return { start, end };
}

// scrollTop that puts row i in view, keeping the current position if it
// already is (`center` centres it instead).
export function scrollFor(i, scrollTop, viewportHeight, rowHeight, center = false) {
  const top = i * rowHeight, bottom = top + rowHeight;
  if (center) return Math.max(0, top - (viewportHeight - rowHeight) / 2);
  if (top < scrollTop) return top;
  if (bottom > scrollTop + viewportHeight) return bottom - viewportHeight;
  return scrollTop;
}

export class VirtualTable {
  // render(i) -> HTML of row i's cells (row i of the current length);
  // header: HTML of the header row; cls: CSS class giving the grid columns.
  constructor(parent, { rowHeight = 22, header = "", cls = "", render, onSelect = null }) {
    this.rowHeight = rowHeight;
    this.render = render;
    this.onSelect = onSelect;
    this.count = 0;
    this.selected = -1;
    this.follow = false;     // keep the last row in view as rows are added
    parent.innerHTML = `<div class="vt ${cls}">
      ${header ? `<div class="vt-head vt-row">${header}</div>` : ""}
      <div class="vt-body"><div class="vt-spacer"></div><div class="vt-rows"></div></div></div>`;
    this.root = parent.firstElementChild;
    this.body = this.root.querySelector(".vt-body");
    this.spacer = this.root.querySelector(".vt-spacer");
    this.rows = this.root.querySelector(".vt-rows");
    this._range = { start: -1, end: -1 };
    this.body.addEventListener("scroll", () => this.draw());
    this.rows.addEventListener("click", (ev) => {
      const row = ev.target.closest("[data-i]");
      if (row) this.select(Number(row.dataset.i), true);
    });
    new ResizeObserver(() => this.draw(true)).observe(this.body);
  }

  setCount(n) {
    const atEnd = this.body.scrollTop + this.body.clientHeight >= this.count * this.rowHeight - 2;
    this.count = n;
    if (this.selected >= n) this.selected = -1;
    this.spacer.style.height = `${n * this.rowHeight}px`;
    if (this.follow && atEnd) this.body.scrollTop = n * this.rowHeight;
    this.draw(true);
  }

  select(i, fromUser = false, center = false) {
    if (i < 0 || i >= this.count) return;
    this.selected = i;
    if (!fromUser) {
      this.body.scrollTop = scrollFor(i, this.body.scrollTop, this.body.clientHeight,
                                      this.rowHeight, center);
    }
    this.draw(true);
    if (this.onSelect) this.onSelect(i, fromUser);
  }

  draw(force = false) {
    const { start, end } = windowFor(this.body.scrollTop, this.body.clientHeight || 400,
                                     this.rowHeight, this.count);
    if (!force && start === this._range.start && end === this._range.end) return;
    this._range = { start, end };
    this.rows.style.transform = `translateY(${start * this.rowHeight}px)`;
    let html = "";
    for (let i = start; i < end; i++) {
      html += `<div class="vt-row${i === this.selected ? " sel" : ""}" data-i="${i}" style="height:${this.rowHeight}px">${this.render(i)}</div>`;
    }
    this.rows.innerHTML = html;
  }
}
