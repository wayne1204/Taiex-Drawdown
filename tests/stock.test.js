/**
 * Browser-integration test for stock.html.
 *
 * Boots the real page in jsdom with real ECharts (SVG renderer) against a
 * small synthetic stock, so it runs without data/stocks/ and pins down the
 * page's own behaviour: KPIs, the chart, broker tables and the period toggle.
 * Same canvas/layout stub idea as render.test.js.
 */
"use strict";

const fs = require("fs");
const path = require("path");

const PROJ = path.join(__dirname, "..");
const read = p => fs.readFileSync(path.join(PROJ, p), "utf8");
const sleep = ms => new Promise(r => setTimeout(r, ms));

function fixture() {
  const daily = [];
  const d0 = Date.UTC(2026, 6, 1);
  let px = 500;
  for (let i = 0; daily.length < 80; i++) {
    const dt = new Date(d0 + i * 864e5);
    if (dt.getUTCDay() === 0 || dt.getUTCDay() === 6) continue;
    const o = px, c = px + ((i % 5) - 2) * 3;
    px = c;
    const n = daily.length;
    daily.push({
      date: dt.toISOString().slice(0, 10), open: o, high: Math.max(o, c) + 2,
      low: Math.min(o, c) - 2, close: c, volume: 1000 + n,
      foreign: n % 2 ? 300 : -200, trust: 50, dealer: n === 40 ? null : -10,
      marginBalance: 5000 + n, marginChange: n ? 1 : null,
      // Main force only covers the tail, as from the live source
      mainForce: n >= 35 ? (n % 3 ? 120 : -80) : null
    });
  }
  const fridays = daily.filter(r => new Date(r.date).getUTCDay() === 5);
  const holders = fridays.map((r, k) => ({
    date: r.date, holders: 100000 + k * 100, avgLots: 5, big400: 300 - k,
    big400Pct: 70 - k / 10, big1000: 150, big1000Pct: 60
  }));
  const side = s => Array.from({ length: 15 }, (_, k) =>
    ({ name: `${s}${k}`, buy: 100, sell: 10, net: 90 - k, pct: 0.5 }));
  const stock = {
    code: "9999", name: "測試股", generatedAt: "2026-10-18T08:00:00",
    start: daily[0].date, end: daily[daily.length - 1].date, daily, holders,
    brokers: { "1": { updated: daily[daily.length - 1].date, buy: side("B"), sell: side("S") },
               "5": { updated: daily[daily.length - 1].date, buy: side("B5"), sell: side("S5").slice(0, 3) } }
  };
  // A past session's own broker table, as fetch_stocks.py stores it (arrays)
  stock.brokerDaily = { [daily[60].date]: { buy: [["過去券商", 10, 1, 9, 0.1]], sell: [["賣方", 1, 5, 4, 0.1]],
                                            totalBuy: 9, totalSell: 4 } };
  return { stock, list: [{ code: "9999", name: "測試股", end: stock.end }] };
}

function buildHtml({ stock, list }) {
  let html = read("stock.html");
  const STUB = `<script>
    HTMLCanvasElement.prototype.getContext = function(){
      const noop = function(){ return this; };
      return new Proxy({ measureText: t => ({ width: String(t).length * 6.6 }),
        canvas:{width:300,height:150} }, { get:(o,k)=> k in o ? o[k] : noop });
    };
    var vis = function(el){ for (var n=el;n&&n.nodeType===1;n=n.parentElement) if(n.hidden) return false; return true; };
    var size = function(el){ return el.id==='chart' && vis(el) ? {w:1400,h:1200} : {w:0,h:0}; };
    ['clientWidth','offsetWidth'].forEach(k=>Object.defineProperty(HTMLElement.prototype,k,{get(){return size(this).w;}}));
    ['clientHeight','offsetHeight'].forEach(k=>Object.defineProperty(HTMLElement.prototype,k,{get(){return size(this).h;}}));
    HTMLElement.prototype.getBoundingClientRect = function(){ var s=size(this);
      return {width:s.w,height:s.h,top:0,left:0,right:s.w,bottom:s.h,x:0,y:0}; };
  </script>`;
  html = html.replace("<title>", STUB + "<title>");
  html = html.replace(/<script src="https:\/\/cdn[^"]*"><\/script>/,
    `<script>${fs.readFileSync(require.resolve("echarts/dist/echarts.min.js"), "utf8")}</script>`);
  html = html.replace('<script src="data/stocks/index.js"></script>',
    `<script>window.STOCK_LIST=${JSON.stringify(list)};` +
    `window.STOCK_DATA={"${stock.code}":${JSON.stringify(stock)}};</script>`);
  return html.replace("renderer:'canvas'", "renderer:'svg'");
}

module.exports = {
  name: "stock.html — render & interaction",

  needs() {
    try { require.resolve("jsdom"); require.resolve("echarts/dist/echarts.min.js"); }
    catch { return "jsdom/echarts not installed — run: npm install"; }
    return null;
  },

  async run(t) {
    const { JSDOM, VirtualConsole } = require("jsdom");
    const errors = [];
    const vc = new VirtualConsole();
    vc.on("jsdomError", e => errors.push(e.message));
    vc.on("error", (...a) => errors.push(a.join(" ")));
    vc.on("warn", (...a) => errors.push(a.join(" ")));

    const fx = fixture();
    const dom = new JSDOM(buildHtml(fx), { runScripts: "dangerously", pretendToBeVisual: true,
      virtualConsole: vc, url: "http://localhost/stock.html" });
    const { window } = dom, doc = window.document;
    try {
      await sleep(1200);
      t.ok("no uncaught errors or console noise", errors.length === 0, errors.slice(0, 3).join(" || ") || "clean");
      t.ok("error banner hidden", doc.getElementById("error").hidden);
      t.eq("title names the stock", doc.getElementById("title").textContent, "9999 測試股");
      t.eq("a plain visit leaves the URL alone", window.location.hash, "");
      t.eq("date box starts on the latest session", doc.getElementById("d-pick").value, fx.stock.end);

      const kpis = [...doc.querySelectorAll("#kpis .kpi")];
      t.eq("8 KPI cards", kpis.length, 8);
      const last = fx.stock.daily[fx.stock.daily.length - 1];
      const kv = k => [...doc.querySelectorAll("#kpis .kpi")].find(x => x.querySelector(".k").textContent.startsWith(k)).querySelector(".v").textContent;
      t.eq("foreign KPI is the last day's net", kv("外資"), (last.foreign > 0 ? "+" : "") + last.foreign.toLocaleString("en-US"));
      t.eq("margin KPI is the last balance", kv("融資餘額"), last.marginBalance.toLocaleString("en-US"));

      const chart = window.echarts.getInstanceByDom(doc.getElementById("chart"));
      const opt = chart.getOption();
      t.eq("8 stacked panels", opt.grid.length, 8);
      const series = name => opt.series.find(s => s.name === name);
      t.ok("candlestick present", series("日K") && series("日K").type === "candlestick");
      t.ok("missing dealer day is a gap, not 0", series("自營商").data[40] === "-");
      t.ok("main force before coverage is a gap", series("主力買賣超").data[0] === "-");
      const hold = series("股東人數").data.filter(v => v !== "-").length;
      t.eq("every in-range holder week is plotted", hold, fx.stock.holders.length);
      const dz = opt.dataZoom[0];
      t.eq("default range is 6M (or all when shorter)", [dz.start, dz.end], [0, 100]);

      t.eq("15 buy rows for 1 day", doc.querySelectorAll("#brokers .tbl:first-child tbody tr").length, 15);
      doc.querySelector('#seg-period button[data-v="5"]').click();
      t.eq("period toggle swaps tables", doc.querySelectorAll("#brokers .tbl:last-child tbody tr").length, 3);
      doc.querySelector('#seg-period button[data-v="60"]').click();
      t.ok("period without data says so", /沒有分點資料/.test(doc.getElementById("br-sum").textContent));

      // ── Date box ──
      const pickDate = v => { const el = doc.getElementById("d-pick"); el.value = v;
        el.dispatchEvent(new window.Event("change")); };
      const d60 = fx.stock.daily[60];
      pickDate(d60.date);
      t.ok("KPIs follow the picked date", kv("收盤").includes(d60.close.toLocaleString("en-US")) &&
        kpis.length && doc.querySelector("#kpis .kpi .k").textContent.includes(d60.date.replace(/-/g, "/")),
        doc.querySelector("#kpis .kpi .k").textContent);
      doc.querySelector('#seg-period button[data-v="1"]').click();
      t.ok("1-day brokers come from that day's own table",
        doc.querySelector("#brokers tbody td").textContent === "過去券商");
      t.ok("5/20/60 disabled on a past date",
        doc.querySelector('#seg-period button[data-v="5"]').disabled);
      const sat = new Date(Date.parse(d60.date) + 864e5 * ((6 - new Date(d60.date).getUTCDay() + 7) % 7 || 7));
      pickDate(sat.toISOString().slice(0, 10));
      t.ok("a non-trading day snaps to the session before it",
        fx.stock.daily.some(r => r.date === doc.getElementById("d-pick").value) &&
        doc.getElementById("d-pick").value <= sat.toISOString().slice(0, 10), doc.getElementById("d-pick").value);
      doc.getElementById("d-latest").click();
      t.eq("最新 returns to the last session", doc.getElementById("d-pick").value, fx.stock.end);
      t.ok("5-day toggle enabled again", !doc.querySelector('#seg-period button[data-v="5"]').disabled);

      // ── 法人 toggles ──
      const instNames = () => chart.getOption().series.filter(x => x.xAxisIndex === 2 && x.data && x.data.length)
        .map(x => x.name);
      const tog = k => doc.querySelector(`#seg-inst button[data-v="${k}"]`).click();
      t.eq("all four on by default", instNames(), ["外資", "投信", "自營商", "三大法人合計"]);
      tog("trust"); tog("dealer"); tog("total");
      t.eq("only 外資 left: one red/green bar series", instNames(), ["外資買賣超"]);
      tog("foreign");
      t.eq("the last one cannot be switched off", instNames(), ["外資買賣超"]);
      tog("total"); tog("foreign");
      t.eq("合計 alone", instNames(), ["合計買賣超"]);

      doc.querySelector('#seg-range button[data-v="21"]').click();
      await sleep(100);
      const z = chart.getOption().dataZoom[0];
      t.eq("1M range zooms to the last 21 sessions", [z.startValue, z.endValue], [59, 79]);
    } finally {
      window.close();
    }
  }
};
