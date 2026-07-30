/**
 * Browser-integration test for index.html.
 *
 * Loads the real page into jsdom with the real ECharts library (SVG renderer,
 * since jsdom cannot rasterise canvas) and drives it exactly like a user
 * would: clicking segmented controls, table rows and column headers. This
 * catches classes of bug that detect.test.js cannot, because detect.test.js
 * only exercises the pure detection function — it never boots the page, so
 * it would happily pass even if ECharts silently rendered an empty or
 * squashed chart.
 *
 * jsdom and echarts are dev dependencies used only by this file. If either is
 * missing, needs() reports how to install them and the suite skips this file
 * instead of crashing the whole run.
 */
"use strict";

const fs = require("fs");
const path = require("path");

const PROJ = path.join(__dirname, "..");
const read = p => fs.readFileSync(path.join(PROJ, p), "utf8");

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

/**
 * Assemble index.html with the CDN script and local <script src> tags inlined
 * (so jsdom needs no network or subresource loading), the renderer switched
 * to svg, and a canvas/layout stub prepended so it runs before any page
 * script — including the one that calls echarts.init().
 */
function buildHtml() {
  let html = read("index.html");

  // ECharts measures text via canvas even under the SVG renderer, and jsdom
  // has no canvas. Stub a 2d context before any script runs; only
  // measureText actually matters for layout.
  //
  // jsdom also has no layout engine, so give #chart a real box — but ONLY
  // when it is actually visible. Reporting a size for a hidden element would
  // mask exactly the bug this suite exists to catch: a real bug in this
  // project was ECharts initialising while its container was still inside
  // <div id="app" hidden>, measuring 0x0, silently falling back to a 100px
  // default width, and cramming all ~1350 sessions into the left edge of the
  // chart. This stub must run BEFORE echarts.init(), not be patched onto a
  // finished DOM afterwards — patching it in after construction would report
  // a perfectly healthy chart on a page that was visibly broken.
  const CANVAS_STUB = `<script>
    HTMLCanvasElement.prototype.getContext = function(){
      const noop = function(){ return this; };
      return new Proxy({
        measureText: t => ({ width: String(t).length * 6.6,
                             actualBoundingBoxAscent: 8, actualBoundingBoxDescent: 3 }),
        font:'', textAlign:'', textBaseline:'', fillStyle:'', strokeStyle:'',
        canvas: { width: 300, height: 150 }
      }, { get:(o,k)=> k in o ? o[k] : noop });
    };

    var visible = function(el){
      for (var n = el; n && n.nodeType === 1; n = n.parentElement) {
        if (n.hidden) return false;
      }
      return true;
    };
    var sizeOf = function(el){
      return (el.id === 'chart' && visible(el)) ? {w:1400, h:900} : {w:0, h:0};
    };
    Object.defineProperty(HTMLElement.prototype, 'clientWidth',
      { get: function(){ return sizeOf(this).w; } });
    Object.defineProperty(HTMLElement.prototype, 'clientHeight',
      { get: function(){ return sizeOf(this).h; } });
    Object.defineProperty(HTMLElement.prototype, 'offsetWidth',
      { get: function(){ return sizeOf(this).w; } });
    Object.defineProperty(HTMLElement.prototype, 'offsetHeight',
      { get: function(){ return sizeOf(this).h; } });
    HTMLElement.prototype.getBoundingClientRect = function(){
      var s = sizeOf(this);
      return {width:s.w, height:s.h, top:0, left:0, right:s.w, bottom:s.h, x:0, y:0};
    };
  </script>`;
  html = html.replace("<title>", CANVAS_STUB + "<title>");

  const echartsSrc = fs.readFileSync(
    require.resolve("echarts/dist/echarts.min.js"), "utf8");
  html = html.replace(/<script src="https:\/\/cdn[^"]*"><\/script>/,
    `<script>${echartsSrc}</script>`);
  html = html.replace('<script src="data/taiex-daily.js"></script>',
    `<script>${read("data/taiex-daily.js")}</script>`);
  html = html.replace('<script src="detect.js"></script>',
    `<script>${read("detect.js")}</script>`);

  // jsdom has no canvas; ECharts' SVG renderer needs none.
  html = html.replace("renderer:'canvas'", "renderer:'svg'");

  return html;
}

/** Read echarts' dataZoom range in whichever shape it happens to report. */
function zoomOf(chart) {
  const dz = (chart.getOption().dataZoom || [])[0];
  return dz ? { s: dz.startValue, e: dz.endValue, start: dz.start, end: dz.end } : null;
}
const spanOf = z => (typeof z.s === "number" && typeof z.e === "number")
  ? z.e - z.s : (z.end - z.start) / 100 * 1350;

module.exports = {
  name: "index.html — render & interaction",

  needs() {
    try {
      require.resolve("jsdom");
    } catch {
      return "jsdom is not installed — run: npm install --save-dev jsdom echarts";
    }
    try {
      require.resolve("echarts/dist/echarts.min.js");
    } catch {
      return "echarts is not installed — run: npm install --save-dev jsdom echarts";
    }
    return null;
  },

  async run(t) {
    const { JSDOM, VirtualConsole } = require("jsdom");

    const errors = [];
    const vc = new VirtualConsole();
    vc.on("jsdomError", e => errors.push("jsdomError: " + (e.stack || e.message)));
    vc.on("error", (...a) => errors.push("console.error: " + a.join(" ")));
    vc.on("warn", (...a) => errors.push("console.warn: " + a.join(" ")));

    const dom = new JSDOM(buildHtml(), {
      runScripts: "dangerously",
      pretendToBeVisual: true,
      virtualConsole: vc
    });
    const { window } = dom;
    const doc = window.document;

    try {
      // ECharts renders asynchronously; give it a beat before asserting.
      await sleep(1500);

      // ── 1. No uncaught errors or console noise ─────────────────────────
      t.ok("no uncaught errors or console errors/warnings", errors.length === 0,
        errors.length ? errors.slice(0, 4).join(" || ") : "clean");

      // ── 2. Error banner hidden, app revealed ────────────────────────────
      t.ok("error banner is hidden", doc.getElementById("error").hidden);
      t.ok("app container is revealed", doc.getElementById("app").hidden === false);

      // ── 3. Exactly 5 KPI cards, all with non-empty values ───────────────
      const kpis = doc.querySelectorAll("#kpis .kpi");
      t.eq("exactly 5 KPI cards render", kpis.length, 5);
      t.ok("every KPI card has a non-empty value",
        [...kpis].every(k => {
          const v = k.querySelector(".v");
          return !!v && v.textContent.trim().length > 0;
        }),
        [...kpis].map(k => `${k.querySelector(".k").textContent}=${k.querySelector(".v").textContent.trim()}`).join(" | "));

      // ── 4. Table shape ───────────────────────────────────────────────────
      const ths = doc.querySelectorAll("#thead th");
      let trs = doc.querySelectorAll("#tbody tr");
      t.eq("table has 15 column headers", ths.length, 15);
      t.ok("table body has at least one row", trs.length > 0, `${trs.length} rows`);
      t.ok("every row has a full cell set (matches header count)",
        [...trs].every(tr => tr.children.length === ths.length),
        `${ths.length} headers`);

      // ── 5. Chart SVG is produced ─────────────────────────────────────────
      const svg = doc.querySelector("#chart svg");
      t.ok("an SVG is produced inside #chart", !!svg);

      // ── 6. Regression guards: the size-while-hidden bug ─────────────────
      if (svg) {
        const vw = parseFloat(svg.getAttribute("width"));
        t.ok("svg width is not the ~100px hidden-container fallback (>= 1300)",
          vw >= 1300, `svg width ${vw}px`);

        const ds = [...svg.querySelectorAll("path")].map(p => p.getAttribute("d") || "");
        const longest = ds.sort((a, b) => b.length - a.length)[0] || "";
        const xs = [...longest.matchAll(/[ML]\s*([\d.\-]+)[, ]/g)]
          .map(m => parseFloat(m[1])).filter(n => !isNaN(n));
        const span = xs.length ? Math.max(...xs) - Math.min(...xs) : 0;
        t.ok("longest series path spans the plot area, not crammed at the left edge (>= 1200 of ~1304px)",
          span >= 1200, `longest path spans ${span.toFixed(0)}px`);

        // ── 7. All five subplot titles are present ────────────────────────
        const labels = [...svg.querySelectorAll("text")].map(tn => tn.textContent);
        for (const want of ["加權指數", "成交金額（億元）", "外資買賣超（億元）",
                            "融資餘額（億元）"]) {
          t.ok(`panel title "${want}" is rendered in the SVG`, labels.includes(want));
        }
        // This one carries the rebase date, so it is a prefix match by design.
        t.ok('panel title "外資累計買賣超" is rendered in the SVG',
          labels.some(l => l.startsWith("外資累計買賣超")),
          labels.find(l => l.startsWith("外資累計買賣超")) || "not found");
      } else {
        t.skip("svg width regression guard", "no svg was produced to inspect");
        t.skip("series geometry span regression guard", "no svg was produced to inspect");
        t.skip("subplot titles present", "no svg was produced to inspect");
      }

      // ── 8. Threshold / price-basis controls re-render the table ────────
      // The page defaults to 7% on the CLOSE basis, so the basis toggle has to
      // move to intraday to prove it does anything.
      const rowCount = () => doc.querySelectorAll("#tbody tr").length;
      const before = rowCount();
      t.eq("default basis button is 收盤價",
        doc.querySelector("#seg-mode button[aria-pressed='true']").dataset.v, "close");
      t.eq("default threshold button is 7%",
        doc.querySelector("#seg-threshold button[aria-pressed='true']").dataset.v, "0.07");
      t.eq("default recovery tolerance is 3%",
        doc.querySelector("#seg-tol button[aria-pressed='true']").dataset.v, "0.03");
      t.eq("default split rule is new-high",
        doc.querySelector("#seg-split button[aria-pressed='true']").dataset.v, "newhigh");

      doc.querySelector("#seg-threshold button[data-v='0.05']").click();
      const after5pct = rowCount();
      doc.querySelector("#seg-threshold button[data-v='0.07']").click();

      doc.querySelector("#seg-mode button[data-v='intraday']").click();
      const afterIntraday = rowCount();
      doc.querySelector("#seg-mode button[data-v='close']").click();   // back to the default
      const back = rowCount();

      t.ok("lowering the threshold to 5% never finds fewer episodes than 7%",
        after5pct >= before, `7%:${before} rows → 5%:${after5pct} rows`);
      // Intraday extremes reach the threshold on days the close never does, so the
      // intraday basis can only find the same number of episodes or more.
      t.ok("switching to the intraday basis re-renders and finds >= as many episodes",
        afterIntraday >= before, `close:${before} rows → intraday:${afterIntraday} rows`);
      t.eq("returning to the 7% / close default restores the original row count", back, before);

      // ── 12. Every column header sorts without error ─────────────────────
      doc.querySelectorAll("#thead th").forEach(th => th.click());
      t.ok("clicking every column header sorts without error",
        errors.length === 0, errors.slice(0, 3).join(" || "));

      // ── Chart instance + row-click zoom (9, 10, 11) ─────────────────────
      const chart = window.echarts.getInstanceByDom(doc.getElementById("chart"));
      t.ok("chart instance is reachable via echarts.getInstanceByDom", !!chart);

      if (chart) {
        // Derive the expected episode list from the controls the page is actually
        // showing, rather than hardcoding defaults here — otherwise changing a
        // default silently desynchronises this test from the table it checks.
        const pressed = id => {
          const b = doc.querySelector(`#${id} button[aria-pressed='true']`);
          return b ? b.dataset.v : null;
        };
        const EPS = window.TaiexDetect.detectEpisodes(
          window.TAIEX_DATA,
          parseFloat(pressed("seg-threshold")),
          pressed("seg-mode"),
          { rebound: pressed("seg-split") === "newhigh" ? null : parseFloat(pressed("seg-split")),
            tol: parseFloat(pressed("seg-tol")) });
        t.eq("expected-episode list matches the rendered row count",
             EPS.length, doc.querySelectorAll("#tbody tr").length);
        const rowAt = n => doc.querySelectorAll("#tbody tr")[n]; // re-query: tbody is replaced wholesale on every render

        const full = zoomOf(chart);
        const target = rowAt(0);
        // The table may be sorted by now, so the first visible row is not
        // necessarily episode 0 — trust data-i, not row position.
        const epi = EPS[+target.dataset.i];
        target.click();
        const zoomed = zoomOf(chart);

        t.ok("row click narrows the x-range materially (< 90% of full span)",
          spanOf(zoomed) < spanOf(full) * 0.9,
          `full span ${spanOf(full).toFixed(0)} → zoomed span ${spanOf(zoomed).toFixed(0)} sessions`);
        t.ok("zoom window brackets the selected episode's peak..trough",
          zoomed.s <= epi.peakIdx && zoomed.e >= epi.troughIdx,
          `zoom [${zoomed.s}, ${zoomed.e}] vs episode peak/trough [${epi.peakIdx}, ${epi.troughIdx}]`);
        t.ok("zoom pads about 40 sessions of lead-in before the peak",
          Math.abs((epi.peakIdx - zoomed.s) - 40) <= 1 || zoomed.s === 0,
          `${epi.peakIdx - zoomed.s} sessions of lead-in`);

        rowAt(0).click(); // re-query, then click again to deselect
        const restored = zoomOf(chart);
        t.ok("clicking the same row again restores the full x-range",
          spanOf(restored) > spanOf(full) * 0.9,
          `span back to ${spanOf(restored).toFixed(0)}`);

        // ── 9. Chart-type toggle ──────────────────────────────────────────
        const typeOfMain = () => (chart.getOption().series || [])[0].type;
        t.eq("main series starts as a line", typeOfMain(), "line");

        doc.querySelector("#seg-chart button[data-v='candle']").click();
        t.eq("chart-type toggle flips the main series to candlestick", typeOfMain(), "candlestick");

        const candles = (chart.getOption().series[0].data || []).filter(d => Array.isArray(d));
        t.ok("candlestick series carries OHLC array data for > 1300 sessions",
          candles.length > 1300, `${candles.length} candles`);
        t.eq("all 6 series survive the chart-type toggle",
          (chart.getOption().series || []).length, 6);
        t.ok("no errors after toggling chart type", errors.length === 0,
          errors.slice(0, 3).join(" || "));

        // ── 11. Zoom survives a chart-type rebuild ─────────────────────────
        rowAt(0).click();
        const zoomedCandle = zoomOf(chart);
        t.ok("row click still zooms while in candlestick mode",
          spanOf(zoomedCandle) < spanOf(full) * 0.9, `span ${spanOf(zoomedCandle).toFixed(0)}`);

        doc.querySelector("#seg-chart button[data-v='line']").click();
        const zoomedLine = zoomOf(chart);
        t.ok("zoom span is unchanged across a candle -> line rebuild",
          Math.abs(spanOf(zoomedCandle) - spanOf(zoomedLine)) < 2,
          `candle span ${spanOf(zoomedCandle).toFixed(0)} → line span ${spanOf(zoomedLine).toFixed(0)}`);
        t.eq("chart-type toggle flips back to line", typeOfMain(), "line");

        // ── Current-episode context strip ───────────────────────────────────
        // The KPI row states the live drawdown; this section is what makes it
        // mean anything. Its ranks must be derived from the same episode list
        // the table shows, or the page contradicts itself.
        {
          const ctx = doc.getElementById("ctx");
          t.ok("context section is shown while a correction is open",
            ctx && !ctx.hidden, ctx ? `hidden=${ctx.hidden}` : "missing");

          const ctxRows = [...ctx.querySelectorAll(".ctx-row")];
          t.eq("one strip per tracked metric", ctxRows.length, 5);

          const eps = window.TaiexDetect.detectEpisodes(
            window.TAIEX_DATA, 0.07, "close", { tol: 0.03 });
          const open = eps.find(e => e.status === "ongoing");
          t.ok("the detection really does have an open episode", !!open,
            open ? `${open.peakDate} → ${open.troughDate}` : "none");

          t.ok("every strip plots one dot per episode and marks exactly one",
            ctxRows.every(r => r.querySelectorAll(".ctx-dot").length === eps.length &&
                               r.querySelectorAll(".ctx-dot.on").length === 1),
            ctxRows.map(r => r.querySelectorAll(".ctx-dot").length).join("/"));

          // Rank shown for 跌幅 must equal the count of deeper episodes, +1.
          const deeper = eps.filter(e => e.drawdownPct < open.drawdownPct).length;
          const rankText = ctxRows[0].querySelector(".ctx-r").textContent;
          t.ok("跌幅 rank counts the episodes that went deeper",
            rankText.includes(`第 ${deeper + 1} `) && rankText.includes(`共 ${eps.length} `),
            `${rankText.trim()} (deeper: ${deeper})`);

          t.ok("caption warns the open episode's figures are still accruing",
            /仍在累積/.test(ctx.querySelector(".ctx-sub").textContent),
            ctx.querySelector(".ctx-sub").textContent.trim().slice(-24));
        }

        // ── Drawdown curve agrees with the table ────────────────────────────
        // The curve exists to show the same number the table reports, as a
        // continuous series. If its trough inside an episode's window differed
        // from that episode's 跌幅, the page would be stating two versions of
        // one fact and the reader could not tell which to believe.
        {
          const uw = (chart.getOption().series || [])[1].data;
          t.eq("drawdown panel is a line over the full series", uw.length,
            window.TAIEX_DATA.length);
          t.ok("drawdown is never positive — it is distance below the prior peak",
            uw.every(v => v === "-" || v <= 0),
            `max ${Math.max(...uw.filter(v => v !== "-"))}`);

          const troughs = tol => window.TaiexDetect
            .detectEpisodes(window.TAIEX_DATA, 0.07, "close", { tol })
            .map(e => ({
              want: e.drawdownPct,
              got: Math.min(...uw.slice(e.peakIdx, e.troughIdx + 1).filter(v => v !== "-"))
            }));

          // Under 嚴格 the episode peak IS the running maximum, so the two
          // readings of one number must be identical.
          const strict = troughs(0);
          const off = strict.filter(w => Math.abs(w.want - w.got) > 0.01);
          t.ok("strict mode: each episode's trough equals its 跌幅 in the table",
            off.length === 0,
            off.length ? off.map(w => `${w.want.toFixed(2)} vs ${w.got.toFixed(2)}`).join(", ")
                       : `${strict.length} episodes agree to 0.01pp`);

          // With a tolerance the table measures from a lower high that counted as
          // a recovery, so the curve — which never ratchets its peak down — may
          // read deeper. It must never read shallower.
          t.ok("3% tolerance: the curve is never shallower than the table",
            troughs(0.03).every(w => w.got <= w.want + 0.01),
            troughs(0.03).filter(w => w.got < w.want - 0.01)
              .map(w => `${w.got.toFixed(2)} vs ${w.want.toFixed(2)}`).join(", ") || "all equal");
        }

        // ── Cumulative foreign net rebases to the visible window ─────────
        // Anchored to 2021-01, the line inside a zoomed episode would just be a
        // slice of a five-year running total: the level would be dominated by
        // history and the episode's own shape squeezed flat. It must restart at
        // zero on the left edge of whatever window is in view.
        {
          // Panel 4 is the cumulative-flow plot (0 index, 1 drawdown, 2 turnover,
          // 3 daily flow, 4 cumulative, 5 margin).
          const cumData  = () => (chart.getOption().series || [])[4].data;
          const cumTitle = () => String(((chart.getOption().title || [])[4] || {}).text || "");
          // Earlier assertions leave a row selected; start from a known state.
          const clearSelection = () => {
            const sel = doc.querySelector('#tbody tr[aria-selected="true"]');
            if (sel) sel.click();
          };
          const flow = i => window.TAIEX_DATA[i].foreignNetYi || 0;

          clearSelection();
          t.ok("full range: cumulative starts at the first session",
            typeof cumData()[0] === "number",
            `first value ${JSON.stringify(cumData()[0])}`);

          const target2 = rowAt(0);
          const epNo = +target2.dataset.i + 1;
          target2.click();                       // nothing was selected, so this selects
          const z2 = zoomOf(chart);
          const d2 = cumData();

          t.ok("zoom actually engaged before checking the rebase",
            spanOf(z2) < spanOf(full) * 0.9 && z2.s > 0,
            `window [${z2.s}, ${z2.e}]`);
          t.ok("zoomed: sessions before the window are blanked",
            d2[z2.s - 1] === "-",
            `index ${z2.s - 1} = ${JSON.stringify(d2[z2.s - 1])}`);
          t.ok("zoomed: the window's first session restarts the running total",
            Math.abs(d2[z2.s] - flow(z2.s)) < 0.02,
            `first in-window value ${d2[z2.s]} vs that day's flow ${flow(z2.s)}`);

          // The end-of-window total must equal the sum over the window only —
          // and must differ from the all-time total, which is the whole point.
          let windowSum = 0, allTime = 0;
          for (let i = 0; i <= z2.e; i++) {
            allTime += flow(i);
            if (i >= z2.s) windowSum += flow(i);
          }
          t.ok("zoomed: end-of-window total equals the sum over the window",
            Math.abs(d2[z2.e] - windowSum) < 0.05,
            `series ${d2[z2.e].toFixed(2)} vs window sum ${windowSum.toFixed(2)}`);
          t.ok("zoomed: it is NOT the all-time cumulative",
            Math.abs(d2[z2.e] - allTime) > 1,
            `window ${d2[z2.e].toFixed(0)} vs all-time ${allTime.toFixed(0)} (episode #${epNo})`);
          t.ok("zoomed: the panel title states the rebase date",
            cumTitle().includes("起算") && !cumTitle().includes("期初"), cumTitle());

          clearSelection();
          t.ok("deselect: cumulative returns to the full range",
            typeof cumData()[0] === "number",
            `first value ${JSON.stringify(cumData()[0])}`);
          t.ok("deselect: the title reverts to the period-start wording",
            cumTitle().includes("期初"), cumTitle());
        }

      } else {
        t.skip("chart-type toggle", "no chart instance reachable");
        t.skip("row click zoom", "no chart instance reachable");
        t.skip("zoom survives chart-type rebuild", "no chart instance reachable");
      }

      // ── Responsive layout at phone width ────────────────────────────────
      // The chart's grid geometry, axis fonts and label formats are computed from
      // window.innerWidth, so crossing the 720px breakpoint must trigger a full
      // re-render — a bare resize() would leave desktop margins on a phone, and a
      // 70px y-axis gutter eats a fifth of a 390px screen.
      if (chart) {
        const optOf = () => chart.getOption();
        const wideLeft = optOf().grid[0].left;

        Object.defineProperty(window, "innerWidth", { value: 390, configurable: true });
        window.dispatchEvent(new window.Event("resize"));
        await sleep(500);                       // resize handler is debounced 150ms

        const g = optOf().grid;
        t.ok("narrow: y-axis gutter shrinks", g[0].left < wideLeft,
          `${wideLeft}px → ${g[0].left}px`);
        t.eq("narrow: still six panels", g.length, 6);
        t.ok("narrow: panels remain vertically disjoint",
          g.every((x, i) => i === 0 || x.top >= g[i - 1].top + g[i - 1].height),
          g.map(x => `${x.top}+${x.height}`).join(" "));
        t.ok("narrow: chart container was re-heighted",
          parseInt(doc.getElementById("chart").style.height, 10) > 0,
          doc.getElementById("chart").style.height);
        t.ok("narrow: x labels drop the century to fit",
          optOf().xAxis[5].axisLabel.formatter("2026-07-29") === "26/07",
          optOf().xAxis[5].axisLabel.formatter("2026-07-29"));
        t.ok("narrow: touch panning is disabled so the page can scroll",
          optOf().dataZoom[0].moveOnMouseMove === false,
          `moveOnMouseMove=${optOf().dataZoom[0].moveOnMouseMove}`);
        t.ok("narrow: no console errors after the re-render", errors.length === 0,
          errors.slice(0, 3).join(" || "));

        // The card view relies on every cell carrying its column name.
        const cells = [...doc.querySelectorAll("#tbody tr")[0].children];
        t.ok("every cell carries data-label for the card view",
          cells.every(c => (c.getAttribute("data-label") || "").length > 0),
          `${cells.length} cells labelled`);
        t.ok("a sort control exists for when headers are hidden",
          !!doc.getElementById("sort-field") && !!doc.getElementById("sort-dir"));

        // Sorting through the mobile control must match the header-click path.
        doc.getElementById("sort-field").value = "drawdownPct";
        doc.getElementById("sort-field").dispatchEvent(new window.Event("change"));
        const ddCol = () => [...doc.querySelectorAll("#tbody tr")].map(
          r => parseFloat(r.children[4].textContent));
        // Sorting defaults to descending. Drawdowns are negative, so descending
        // runs shallowest (-7%) to deepest (-32%) — non-increasing numerically.
        const desc = ddCol();
        t.ok("mobile sort control orders the rows (descending by default)",
          desc.every((v, i) => i === 0 || v <= desc[i - 1] + 1e-9),
          desc.map(v => v.toFixed(1)).join(", "));

        doc.getElementById("sort-dir").click();
        const asc = ddCol();
        t.ok("sort direction toggle reverses the order",
          asc.every((v, i) => i === 0 || v >= asc[i - 1] - 1e-9),
          asc.map(v => v.toFixed(1)).join(", "));
        t.ok("toggle label reflects the direction",
          doc.getElementById("sort-dir").textContent.includes("由小到大"),
          doc.getElementById("sort-dir").textContent);
        doc.getElementById("sort-dir").click();   // back to descending

        // Back to desktop for anything that follows.
        doc.getElementById("sort-field").value = "";
        doc.getElementById("sort-field").dispatchEvent(new window.Event("change"));
        Object.defineProperty(window, "innerWidth", { value: 1024, configurable: true });
        window.dispatchEvent(new window.Event("resize"));
        await sleep(500);
        t.ok("desktop: gutter restored on returning to a wide viewport",
          optOf().grid[0].left === wideLeft, `${optOf().grid[0].left}px`);
      }

      // ── 13. CSV export runs without throwing ────────────────────────────
      // jsdom has no URL.createObjectURL / Blob download machinery; stub just
      // enough of it so the click handler can run to completion.
      if (typeof window.URL.createObjectURL !== "function") {
        window.URL.createObjectURL = () => "blob:stub";
      }
      if (typeof window.URL.revokeObjectURL !== "function") {
        window.URL.revokeObjectURL = () => {};
      }
      let csvError = null;
      try {
        doc.getElementById("btn-csv").click();
      } catch (e) {
        csvError = e;
      }
      t.ok("CSV export button click does not throw",
        csvError === null, csvError ? String(csvError) : "ok");

    } finally {
      dom.window.close();
    }
  }
};
