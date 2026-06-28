const $ = (id) => document.getElementById(id);
const kChart = echarts.init($("kchart"), "dark");
const eChart = echarts.init($("echart"), "dark");
window.addEventListener("resize", () => { kChart.resize(); eChart.resize(); });

// ===== 市场 / 币种：按代码前缀自适应（A股¥ / 港股HK$ / 美股US$）=====
function marketOf(sym) {
  const s = (sym || "").trim().toLowerCase();
  if (s.startsWith("hk")) return "hk";
  if (s.startsWith("us")) return "us";
  return "cn";
}
const CCY = { cn: "¥", hk: "HK$", us: "US$" };
function ccyOf(sym) { return CCY[marketOf(sym)] || "¥"; }
let _curCcy = "¥";   // 当前回测标的的币种符号（渲染金额时使用）

function fmtMoney(v) {
  return _curCcy + Number(v).toLocaleString("zh-CN", { maximumFractionDigits: 0 });
}
function signClass(v) { return v >= 0 ? "pos" : "neg"; }

function renderStats(stats) {
  const cards = [
    { k: "策略总回报", v: stats.total_return + "%", c: signClass(stats.total_return) },
    { k: "年化收益", v: stats.annualized + "%", c: signClass(stats.annualized) },
    { k: "同期买入持有", v: stats.buy_hold_return + "%", c: signClass(stats.buy_hold_return) },
    { k: "期末资金", v: fmtMoney(stats.final_equity), c: signClass(stats.total_return) },
    { k: "交易次数", v: stats.num_trades, c: "" },
    { k: "胜率", v: stats.win_rate + "%", c: "" },
    { k: `超短交易(≤${stats.short_holding_days || 3}日)`, v: `${stats.short_trade_rate || 0}% (${stats.short_trade_count || 0}笔)`, c: (stats.short_trade_count || 0) > 0 ? "neg" : "" },
    { k: "单笔平均收益", v: stats.avg_return + "%", c: signClass(stats.avg_return) },
    { k: "最大回撤", v: stats.max_drawdown + "%", c: "neg" },
  ];
  if (stats.total_interest && stats.total_interest > 0) {
    cards.push({ k: `融资利息(${stats.margin_rate}%)`, v: "-" + fmtMoney(stats.total_interest), c: "neg" });
  }
  if (stats.total_commission != null) {
    cards.push({ k: "交易手续费", v: "-" + fmtMoney(stats.total_commission), c: "neg" });
  }
  if (stats.deleverage_count) {
    cards.push({ k: "减仓次数", v: stats.deleverage_count, c: "" });
  }
  $("stats").innerHTML = cards.map(c =>
    `<div class="stat-card"><div class="k">${c.k}</div><div class="v ${c.c}">${c.v}</div></div>`
  ).join("");
}

function renderKChart(data) {
  const { dates, kline, volumes, markers, meta } = data;
  const buyPoints = markers.buys.map(b => ({
    coord: [b.date, b.price], value: "买",
    itemStyle: { color: "#f85149" },
    symbol: "pin", symbolSize: 36, symbolRotate: 0,
    label: { show: true, formatter: "买", color: "#fff", fontSize: 10 }
  }));
  const sellPoints = markers.sells.filter(s => s.kind !== "deleverage").map(s => ({
    coord: [s.date, s.price], value: "卖",
    itemStyle: { color: "#3fb950" },
    symbol: "pin", symbolSize: 36,
    label: { show: true, formatter: "卖", color: "#fff", fontSize: 10 }
  }));
  const delevPoints = markers.sells.filter(s => s.kind === "deleverage").map(s => ({
    coord: [s.date, s.price], value: "减",
    itemStyle: { color: "#d29922" },
    symbol: "triangle", symbolSize: 16, symbolRotate: 180,
    label: { show: true, formatter: "减", color: "#fff", fontSize: 9, position: "bottom" }
  }));

  kChart.setOption({
    backgroundColor: "transparent",
    title: { text: `${meta.symbol} 日K线 + 买卖点`, left: 10, top: 6, textStyle: { fontSize: 13 } },
    tooltip: { trigger: "axis", axisPointer: { type: "cross" } },
    legend: { data: ["K线", "成交量"], top: 6, right: 10 },
    grid: [
      { left: 50, right: 20, top: 50, height: "58%" },
      { left: 50, right: 20, top: "74%", height: "16%" }
    ],
    xAxis: [
      { type: "category", data: dates, scale: true, boundaryGap: true, axisLine: { lineStyle: { color: "#555" } } },
      { type: "category", gridIndex: 1, data: dates, axisLabel: { show: false }, axisLine: { lineStyle: { color: "#555" } } }
    ],
    yAxis: [
      { scale: true, splitLine: { lineStyle: { color: "#21262d" } } },
      { gridIndex: 1, splitNumber: 2, axisLabel: { show: false }, splitLine: { show: false } }
    ],
    dataZoom: [
      { type: "inside", xAxisIndex: [0, 1], start: 0, end: 100 },
      { type: "slider", xAxisIndex: [0, 1], bottom: 6, height: 16, start: 0, end: 100 }
    ],
    series: [
      {
        name: "K线", type: "candlestick", data: kline,
        itemStyle: { color: "#f85149", color0: "#3fb950", borderColor: "#f85149", borderColor0: "#3fb950" },
        markPoint: { symbolSize: 36, data: [...buyPoints, ...sellPoints, ...delevPoints], label: { show: true } }
      },
      { name: "成交量", type: "bar", xAxisIndex: 1, yAxisIndex: 1, data: volumes, itemStyle: { color: "#3a4a63" } }
    ]
  }, true);
}

function renderEquityChart(data) {
  const { equity, benchmark } = data;
  eChart.setOption({
    backgroundColor: "transparent",
    title: { text: "资金曲线 vs 买入持有", left: 10, top: 6, textStyle: { fontSize: 13 } },
    tooltip: { trigger: "axis" },
    legend: { data: ["策略资金", "买入持有"], top: 6, right: 10 },
    grid: { left: 60, right: 20, top: 50, bottom: 40 },
    xAxis: { type: "category", data: equity.map(e => e[0]), axisLine: { lineStyle: { color: "#555" } } },
    yAxis: { scale: true, splitLine: { lineStyle: { color: "#21262d" } } },
    series: [
      { name: "策略资金", type: "line", showSymbol: false, data: equity.map(e => e[1]), lineStyle: { color: "#58a6ff", width: 2 }, areaStyle: { color: "rgba(88,166,255,0.12)" } },
      { name: "买入持有", type: "line", showSymbol: false, data: benchmark.map(e => e[1]), lineStyle: { color: "#8b949e", width: 1, type: "dashed" } }
    ]
  }, true);
}

function applyScan5yPreset() {
  applyConfig(["donchian_breakout", "ma_bull_stack"], "or", ["trailing_pct", "donchian_exit"], "and");
  document.querySelectorAll('input[name="entryLogic"]').forEach(r => { r.checked = r.value === "or"; });
  document.querySelectorAll('input[name="exitLogic"]').forEach(r => { r.checked = r.value === "and"; });
  if ($("entryWindow")) $("entryWindow").value = 5;
  if ($("exitWindow")) $("exitWindow").value = 5;
  if ($("posEntryType")) $("posEntryType").value = "fixed";
  if ($("posReduceType")) $("posReduceType").value = "none";
  if ($("posMaxLev")) $("posMaxLev").value = 1;
  if ($("posMinLev")) $("posMinLev").value = 1;
  if ($("stopLossPct")) $("stopLossPct").value = 10;
}

function initFromUrlParams() {
  const qs = new URLSearchParams(window.location.search);
  const sym = qs.get("symbol");
  if (sym) $("symbol").value = sym;
  const st = qs.get("start");
  if (st) $("start").value = st;
  const ed = qs.get("end");
  if (ed) $("end").value = ed;
  if (qs.get("preset") === "scan5y") applyScan5yPreset();
}

function renderTrades(trades) {
  const tbody = document.querySelector("#trades tbody");
  if (!trades.length) {
    tbody.innerHTML = `<tr><td colspan="14" style="text-align:center;color:#8b949e">该区间内未触发任何交易</td></tr>`;
    return;
  }
  // 从近往远期排序：最新的交易显示在最上面（# 保持原始时间顺序编号）
  const ordered = trades.map((t, i) => [t, i]).reverse();
  tbody.innerHTML = ordered.map(([t, i]) => {
    const c = t.return_pct >= 0 ? "pos" : "neg";
    const pos = (t.position != null) ? t.position : 1;
    const posTxt = pos > 1 ? `<span class="lev">${pos}×</span>` : `${pos}`;
    const peTxt = (t.pe_pct != null) ? `<br><span class="pe">PE${t.pe_pct}%</span>` : "";
    const interest = (t.interest != null) ? t.interest : 0;
    const intTxt = (pos > 1 && interest > 0) ? `<span class="neg">-${interest}</span>` : `<span class="pe">无融资</span>`;
    const comm = (t.commission != null) ? t.commission : 0;
    const buyAmt = (t.buy_amount != null) ? fmtMoney(t.buy_amount) : "-";
    const sellAmt = (t.sell_amount != null) ? fmtMoney(t.sell_amount) : "-";
    const profit = (t.profit != null) ? t.profit : 0;
    const pc = profit >= 0 ? "pos" : "neg";
    const profitTxt = `${profit >= 0 ? "+" : ""}${fmtMoney(profit)}`;
    const rbs = t.rebalances || [];
    const toggle = rbs.length
      ? `<div class="delev-toggle" data-ti="${i}"><span class="caret">▶</span> ${rbs.length}次减仓</div>`
      : "";
    let row = `<tr>
      <td>${i + 1}</td>
      <td>${t.entry_date}</td>
      <td>${t.entry_price}</td>
      <td>${buyAmt}</td>
      <td>${posTxt}${peTxt}${toggle}</td>
      <td>${intTxt}</td>
      <td><span class="neg">-${comm}</span></td>
      <td>${t.exit_date}</td>
      <td>${t.exit_price}</td>
      <td>${sellAmt}</td>
      <td class="${pc}">${profitTxt}</td>
      <td class="${c}">${t.return_pct}%</td>
      <td>${t.holding_days}</td>
      <td>${t.reason}</td>
    </tr>`;
    // 卸杠杆减仓子行（默认折叠）
    rbs.forEach((rb, j) => {
      const tag = (rb.pe_pct != null) ? "PE减仓" : (rb.gain != null ? "盈利减仓" : "减仓");
      const detail = (rb.pe_pct != null) ? `PE分位${rb.pe_pct}%`
        : (rb.gain != null ? `涨幅+${rb.gain}%` : "");
      row += `<tr class="rebalance-row rb-${i}" style="display:none">
        <td></td>
        <td colspan="13">↳ <span class="delev">${tag} #${j + 1}</span> ${rb.date} @${rb.price}
           · 卖出 ${fmtMoney(rb.amount)}（${rb.shares}份）
           · 仓位 ${rb.from_pos}×→${rb.to_pos}×
           · ${detail} · 手续费 <span class="neg">-${rb.commission}</span></td>
      </tr>`;
    });
    return row;
  }).join("");

  // 卸杠杆子行折叠/展开
  tbody.querySelectorAll(".delev-toggle").forEach(el => {
    el.addEventListener("click", () => {
      const ti = el.dataset.ti;
      const rows = tbody.querySelectorAll(`.rb-${ti}`);
      const caret = el.querySelector(".caret");
      const open = caret.textContent === "▶";
      rows.forEach(r => { r.style.display = open ? "table-row" : "none"; });
      caret.textContent = open ? "▼" : "▶";
    });
  });
}

// ===== 请求缓存：参数未变则不重复请求后端，用本地数据重新渲染（带1s载入动效）=====
const _cache = { backtest: null, optimize: {} };  // optimize 按 symbol+start+end 缓存
const DATA_SOURCE_VERSION = "2026-06-28-or-entry-fix-v1";
const OPT_SCORE_VERSION = "2026-06-28-filter-return15-v1";
function _payloadKey(p) { return JSON.stringify({ v: DATA_SOURCE_VERSION, ...p }); }
function _sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// 判断是否被限流，弹窗提示
function _handleRateLimit(data) {
  if (data && data.rate_limited) {
    showModal("请求被限流", data.error || "请求过于频繁，请稍后再试。");
    return true;
  }
  return false;
}

// 通用弹窗
function showModal(title, body) {
  let m = document.getElementById("globalModal");
  if (!m) {
    m = document.createElement("div");
    m.id = "globalModal";
    m.className = "modal-mask";
    m.innerHTML = `<div class="modal-box">
        <div class="modal-title"></div>
        <div class="modal-body"></div>
        <div class="modal-foot"><button class="modal-ok">知道了</button></div>
      </div>`;
    document.body.appendChild(m);
    m.querySelector(".modal-ok").addEventListener("click", () => m.style.display = "none");
    m.addEventListener("click", (e) => { if (e.target === m) m.style.display = "none"; });
  }
  m.querySelector(".modal-title").textContent = title;
  m.querySelector(".modal-body").innerHTML = body;
  m.style.display = "flex";
}

// 载入遮罩（1秒动效，避免用户以为是假数据）
function _showLoading(text) {
  let m = document.getElementById("loadingMask");
  if (!m) {
    m = document.createElement("div");
    m.id = "loadingMask";
    m.className = "loading-mask";
    m.innerHTML = `<div class="loading-box"><div class="loading-spin"></div><div class="loading-text"></div></div>`;
    document.body.appendChild(m);
  }
  m.querySelector(".loading-text").textContent = text || "处理中…";
  m.style.display = "flex";
}
function _hideLoading() {
  const m = document.getElementById("loadingMask");
  if (m) m.style.display = "none";
}

async function runBacktest() {
  const btn = $("runBtn");
  const status = $("status");
  btn.disabled = true;

  const panels = document.querySelector(".panel");
  const allStrats = Array.from(panels.querySelectorAll(".strat"));
  const entrySet = new Set(["ma_golden", "donchian_breakout", "ma_bull_stack", "macd_golden", "volume_breakout"]);
  const entries = [], exits = [];
  allStrats.forEach(div => {
    const on = div.querySelector(".strat-on");
    if (!on || !on.checked) return;
    const spec = { type: div.dataset.type };
    div.querySelectorAll(".p").forEach(inp => {
      const k = inp.dataset.k;
      const raw = inp.value.trim();
      spec[k] = (k === "periods")
        ? raw.split(",").map(x => parseInt(x.trim())).filter(x => !isNaN(x))
        : parseFloat(raw);
    });
    (entrySet.has(div.dataset.type) ? entries : exits).push(spec);
  });

  const position = {
    entry: $("posEntryType").value,
    reduce: $("posReduceType").value,
    max_leverage: parseFloat($("posMaxLev").value),
    min_leverage: parseFloat($("posMinLev").value),
    reduce_start: parseFloat($("posReduceStart").value),
    reduce_step: parseFloat($("posReduceStep").value),
    reduce_pct: parseFloat($("posReducePct").value),
  };

  const payload = {
    symbol: $("symbol").value.trim(),
    start: $("start").value,
    end: $("end").value,
    adjust: $("adjust").value,
    initial_capital: parseFloat($("capital").value),
    commission: parseFloat($("commission").value),
    margin_rate: parseFloat($("marginRate").value),
    strategy: {
      entry_logic: document.querySelector('input[name="entryLogic"]:checked').value,
      exit_logic: document.querySelector('input[name="exitLogic"]:checked').value,
      entry_window: parseInt($("entryWindow").value) || 5,
      exit_window: parseInt($("exitWindow").value) || 5,
      entries,
      exits,
      stop_loss_pct: parseFloat($("stopLossPct").value) || 0,
      position,
    }
  };

  const pkey = _payloadKey(payload);

  // 参数未变 → 用缓存渲染（仍带1s载入动效，避免假数据观感）
  if (_cache.backtest && _cache.backtest.key === pkey) {
    status.className = "status";
    status.textContent = "参数未变，使用上次结果重新渲染…";
    _showLoading("参数未变，加载上次结果…");
    await _sleep(1000);
    _hideLoading();
    const data = _cache.backtest.data;
    _curCcy = ccyOf(data.meta.symbol);
    renderStats(data.stats);
    renderKChart(data);
    renderEquityChart(data);
    renderTrades(data.trades);
    status.textContent = `完成（缓存）：${data.meta.symbol}，共 ${data.meta.rows} 个交易日，${data.stats.num_trades} 笔交易。`;
    btn.disabled = false;
    return;
  }

  status.className = "status";
  status.textContent = "拉取数据并回测中…（首次拉取某只股票可能需几秒）";
  _showLoading("拉取数据并回测中…");

  try {
    const res = await fetch("/api/backtest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    await _sleep(1000);  // 至少1秒载入动效
    _hideLoading();
    if (_handleRateLimit(data)) {
      status.className = "status error";
      status.textContent = "已被限流";
      return;
    }
    if (!data.ok) {
      status.className = "status error";
      status.textContent = "错误：" + (data.error || "未知错误");
      return;
    }
    _cache.backtest = { key: pkey, data };
    _curCcy = ccyOf(data.meta.symbol);
    renderStats(data.stats);
    renderKChart(data);
    renderEquityChart(data);
    renderTrades(data.trades);
    const diag = data.diagnostics || {};
    if (data.stats.num_trades === 0 && diag.message) {
      status.className = "status error";
      status.textContent = "⚠ " + diag.message;
    } else {
      let peNote = "";
      if ($("posEntryType").value === "pe_percentile" || $("posReduceType").value === "pe_percentile") {
        if (data.meta.pe_available) {
          peNote = data.meta.pe_proxy ? ` · PE仓位已启用(用「${data.meta.pe_proxy}」PE代理)` : " · PE仓位已启用";
        } else {
          peNote = " · ⚠该标的无PE数据，已按满仓(1.0)";
        }
      }
      status.textContent = `完成：${data.meta.symbol}，共 ${data.meta.rows} 个交易日，${data.stats.num_trades} 笔交易${peNote}。`;
    }
  } catch (e) {
    _hideLoading();
    status.className = "status error";
    status.textContent = "请求失败：" + e.message;
  } finally {
    btn.disabled = false;
  }
}

// ===== 一键寻优 =====
function applyConfig(entryTypes, entryLogic, exitTypes, exitLogic) {
  document.querySelectorAll(".panel .strat").forEach(div => {
    const on = div.querySelector(".strat-on");
    const t = div.dataset.type;
    if (["ma_golden", "donchian_breakout", "ma_bull_stack", "macd_golden", "volume_breakout"].includes(t)) {
      on.checked = entryTypes.includes(t);
    } else {
      on.checked = exitTypes.includes(t);
    }
  });
  document.querySelector(`input[name="entryLogic"][value="${entryLogic}"]`).checked = true;
  document.querySelector(`input[name="exitLogic"][value="${exitLogic}"]`).checked = true;
}

function renderOpt(res) {
  const wrap = $("optWrap");
  wrap.style.display = "block";
  const bh = res.benchmark;
  const rowsHtml = res.top.map((r, i) => {
    const ec = r.excess >= 0 ? "pos" : "neg";
    const tc = r.total_return >= 0 ? "pos" : "neg";
    return `<tr data-i="${i}">
      <td>${i + 1}</td>
      <td><b>${r.score.toFixed(2)}</b></td>
      <td class="${tc}">${r.total_return}%</td>
      <td class="${ec}">${r.excess}%</td>
      <td>${r.annualized}%</td>
      <td>${r.sharpe}</td>
      <td>${r.calmar}</td>
      <td class="neg">${r.max_drawdown}%</td>
      <td>${r.trades}</td>
      <td>${r.win_rate}%</td>
      <td class="${(r.short_trade_rate || 0) > 0 ? "neg" : ""}">${r.short_trade_rate || 0}%</td>
      <td>${Math.round((r.avg_position_ratio || 0) * 100)}%</td>
      <td class="combo">${r.entry} <span class="arrow">→</span> ${r.exit}</td>
      <td><button class="apply-btn" data-i="${i}">载入</button></td>
    </tr>`;
  }).join("");
  wrap.innerHTML = `
    <div class="opt-head">
      <h2>寻优 Top10 <span class="sub">综合评分 = (0.35×卡玛分 + 0.30×夏普分 + 0.20×${res.return_basis === "annual" ? "年化" : "超额年化"}分 + 0.15×资金效率分) × 回撤惩罚 × 超短单惩罚（子项 clamp[-1,1]：卡玛/2、夏普/2、收益/15%、(年化/平均持仓)/50%；回撤惩罚=max(0.2, (1-最大回撤幅度)²)；超短单惩罚=max(0.5, 1-超短≤3日占比)）</span></h2>
      <div class="opt-meta">共 ${res.total_combos} 组合 → 去重后 ${res.unique_combos} 个 · 准入门槛(夏普≥${(res.filters || {}).sharpe_min ?? 0.5}·卡玛≥${(res.filters || {}).calmar_min ?? 0.3}·回撤≤${(res.filters || {}).mdd_max ?? 35}%·交易≥${res.min_trades}) → 合格 ${res.scored_pool} 个${(res.filters || {}).fallback ? "（无策略达标，已放宽为仅交易门槛）" : ""} · 收益基准：${res.return_basis === "annual" ? "年化收益率" : "超额年化"} · 同期买入持有 ${bh.buy_hold_return}%（年化${bh.buy_hold_annualized}%）</div>
    </div>
    <table class="opt-table">
      <thead><tr>
        <th>#</th><th>综合分</th><th>总回报</th><th>超额(vs大盘)</th><th>年化</th><th>夏普</th><th>卡玛</th><th>最大回撤</th><th>交易</th><th>胜率</th><th>超短≤3日</th><th>平均持仓</th><th>入场 → 出场</th><th></th>
      </tr></thead>
      <tbody>${rowsHtml}</tbody>
    </table>`;
  wrap.querySelectorAll(".apply-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const r = res.top[parseInt(btn.dataset.i)];
      applyConfig(r.entry_types, r.entry_logic, r.exit_types, r.exit_logic);
      runBacktest();
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
  });
}

async function runOptimize() {
  const btn = $("optBtn");
  const status = $("status");
  btn.disabled = true;
  $("runBtn").disabled = true;
  status.className = "status";
  status.textContent = "正在跑全部 3249 个组合…（约几秒）";

  const returnBasis = ($("optReturnBasis") && $("optReturnBasis").value) || "excess";
  const payload = {
    symbol: $("symbol").value.trim(),
    start: $("start").value,
    end: $("end").value,
    adjust: $("adjust").value,
    initial_capital: parseFloat($("capital").value),
    commission: parseFloat($("commission").value),
    top_n: 10,
    min_trades: 10,
    return_basis: returnBasis,
  };
  const okey = `${OPT_SCORE_VERSION}|${payload.symbol}|${payload.start}|${payload.end}|${payload.adjust}|${payload.commission}|${returnBasis}`;

  // 参数未变 → 用缓存渲染
  if (_cache.optimize[okey]) {
    status.textContent = "参数未变，使用上次寻优结果重新渲染…";
    _showLoading("参数未变，加载上次结果…");
    await _sleep(1000);
    _hideLoading();
    const data = _cache.optimize[okey];
    renderOpt(data);
    status.textContent = `寻优完成（缓存）：${data.meta.symbol} ${data.meta.start}~${data.meta.end}，已排出Top10。`;
    btn.disabled = false;
    $("runBtn").disabled = false;
    return;
  }

  _showLoading("正在跑全部 3249 个组合…");
  try {
    const res = await fetch("/api/optimize", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    await _sleep(1000);
    _hideLoading();
    if (_handleRateLimit(data)) {
      status.className = "status error";
      status.textContent = "已被限流";
      return;
    }
    if (!data.ok) {
      status.className = "status error";
      status.textContent = "寻优失败：" + (data.error || "未知错误");
      return;
    }
    _cache.optimize[okey] = data;
    renderOpt(data);
    status.textContent = `寻优完成：${data.meta.symbol} ${data.meta.start}~${data.meta.end}，已排出Top10（点"载入"看图）。`;
  } catch (e) {
    _hideLoading();
    status.className = "status error";
    status.textContent = "请求失败：" + e.message;
  } finally {
    btn.disabled = false;
    $("runBtn").disabled = false;
  }
}

// 默认日期：结束=当天，开始=十年前同一天
function localYMD(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}
(function initDates() {
  const today = new Date();
  const tenYrAgo = new Date(today.getFullYear() - 10, today.getMonth(), today.getDate());
  $("end").value = localYMD(today);
  $("start").value = localYMD(tenYrAgo);
})();

// ===== 牛/熊/牛熊 市场环境最佳策略 =====
const REGIMES = [
  { key: "bull", name: "牛市最佳", start: "2019-01-01", end: "2021-02-18", cls: "regime-bull" },
  { key: "bear", name: "熊市最佳", start: "2021-02-18", end: "2024-02-01", cls: "regime-bear" },
  { key: "cycle", name: "牛转熊最佳", start: "2019-01-01", end: "2024-02-01", cls: "regime-cycle" },
  { key: "bearbull", name: "熊转牛最佳", start: "2021-02-18", end: "2026-06-22", cls: "regime-bearbull" },
  {
    key: "decade", name: "十年最佳", cls: "regime-decade",
    start: localYMD(new Date(new Date().getFullYear() - 10, new Date().getMonth(), new Date().getDate())),
    end: localYMD(new Date()),
  },
];

function loadRegime(reg, top) {
  $("start").value = reg.start;
  $("end").value = reg.end;
  applyConfig(top.entry_types, top.entry_logic, top.exit_types, top.exit_logic);
  runBacktest();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

const REGIME_LABEL = "🎯 跑 牛市/熊市/牛熊/熊牛 最佳策略";
// 市场环境最佳策略固定以创业板指(sz399006)为基准寻优，不跟随主页输入框标的。
const REGIME_SYMBOL = "sz399006";

async function runRegimes() {
  const wrap = $("regimeWrap");
  const btn = $("regimeBtn") || $("regimeBtn2");
  const symbol = REGIME_SYMBOL;
  wrap.style.display = "";
  if (btn) { btn.disabled = true; btn.textContent = "正在跑四种市场环境…（约二十秒）"; }

  _showLoading("正在跑四种市场环境最佳策略…");
  const results = [];
  for (const reg of REGIMES) {
    const okey = `${OPT_SCORE_VERSION}|${symbol}|${reg.start}|${reg.end}|${$("adjust").value}|${$("commission").value}|excess`;
    let data;
    if (_cache.optimize[okey]) {
      data = _cache.optimize[okey];
    } else {
      try {
        const res = await fetch("/api/optimize", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            symbol, start: reg.start, end: reg.end,
            adjust: $("adjust").value,
            initial_capital: parseFloat($("capital").value),
            commission: parseFloat($("commission").value),
            top_n: 1, min_trades: 5,
          })
        });
        data = await res.json();
        if (data.ok) _cache.optimize[okey] = data;
      } catch (e) {
        data = { ok: false, error: e.message };
      }
    }
    if (_handleRateLimit(data)) { _hideLoading(); if (btn) { btn.disabled = false; btn.textContent = REGIME_LABEL; } return; }
    results.push({ reg, data });
  }
  await _sleep(1000);
  _hideLoading();

  const cards = results.map(({ reg, data }, idx) => {
    if (!data.ok || !data.top || !data.top.length) {
      return `<div class="regime-card ${reg.cls}"><div class="rg-name">${reg.name}</div>
        <div class="rg-err">无结果：${(data && data.error) || "无"}</div></div>`;
    }
    const t = data.top[0];
    const bh = data.benchmark;
    const ec = t.excess >= 0 ? "pos" : "neg";
    return `<div class="regime-card ${reg.cls}">
      <div class="rg-name">${reg.name} <span class="rg-range">${reg.start} ~ ${reg.end}</span></div>
      <div class="rg-combo">${t.entry} <span class="arrow">→</span> ${t.exit}</div>
      <div class="rg-metrics">
        <span>总回报 <b class="${t.total_return >= 0 ? "pos" : "neg"}">${t.total_return}%</b></span>
        <span>超额 <b class="${ec}">${t.excess}%</b></span>
        <span>年化 ${t.annualized}%</span>
        <span>夏普 ${t.sharpe}</span>
        <span>卡玛 ${t.calmar}</span>
        <span>回撤 <b class="neg">${t.max_drawdown}%</b></span>
        <span>交易 ${t.trades}</span>
        <span>胜率 ${t.win_rate}%</span>
        <span>超短≤3日 <b class="${(t.short_trade_rate || 0) > 0 ? "neg" : ""}">${t.short_trade_rate || 0}%</b></span>
      </div>
      <div class="rg-foot">同期买入持有 ${bh.buy_hold_return}% · <button class="rg-load" data-idx="${idx}">直接载入</button></div>
    </div>`;
  }).join("");

  wrap.innerHTML = `<div class="regime-head">
      <h2>市场环境最佳策略 <span class="sub">综合评分Top1（基金式打分）· 标的 ${symbol}</span></h2>
      <button id="regimeBtn2" class="regime-btn-sm">重新跑</button>
    </div>
    <div class="regime-cards">${cards}</div>`;

  if (btn) { btn.disabled = false; btn.textContent = REGIME_LABEL; }

  wrap.querySelectorAll(".rg-load").forEach(b => {
    b.addEventListener("click", () => {
      const idx = parseInt(b.dataset.idx);
      const { reg, data } = results[idx];
      if (data.ok && data.top && data.top.length) loadRegime(reg, data.top[0]);
    });
  });
  const b2 = $("regimeBtn2");
  if (b2) b2.addEventListener("click", runRegimes);
}

// 触发按钮在左侧面板(静态HTML)，结果渲染到右侧 regimeWrap
$("regimeBtn").addEventListener("click", runRegimes);

// 字段说明问号：点击切换对应 .hint 的显示
document.addEventListener("click", (e) => {
  const tip = e.target.closest(".q-tip");
  if (!tip) return;
  e.preventDefault();
  e.stopPropagation();
  const el = document.getElementById(tip.dataset.target);
  if (el) {
    el.hidden = !el.hidden;
    tip.classList.toggle("active", !el.hidden);
  }
});

$("runBtn").addEventListener("click", runBacktest);
$("optBtn").addEventListener("click", runOptimize);

// ===== 仓位管理 UI 联动：减仓策略决定显示哪些参数 =====
function updatePosUI() {
  const reduce = $("posReduceType").value;
  const rowStep = $("rowReduceStep");
  const lblStart = $("lblReduceStart");
  const lblStep = $("lblReduceStep");
  const lblPct = $("lblReducePct");
  if (reduce === "none") {
    rowStep.style.display = "none";
  } else {
    rowStep.style.display = "";
    if (reduce === "profit") {
      lblStart.firstChild.textContent = "上涨起步点(%)";
      lblStep.firstChild.textContent = "上涨步长(%)";
      lblPct.style.display = "";
    } else {  // pe_percentile
      lblStart.firstChild.textContent = "PE起步点(百分位)";
      lblStep.firstChild.textContent = "PE上升步长(百分位点)";
      lblPct.style.display = "none";
    }
  }
}
$("posReduceType").addEventListener("change", updatePosUI);
$("posEntryType").addEventListener("change", updatePosUI);
updatePosUI();

// 入场/出场逻辑切换：AND 时显示容忍窗口控件，OR 时隐藏
function updateEntryWindowUI() {
  const isAnd = document.querySelector('input[name="entryLogic"]:checked').value === "and";
  $("entryWindowWrap").hidden = !isAnd;
}
function updateExitWindowUI() {
  const isAnd = document.querySelector('input[name="exitLogic"]:checked').value === "and";
  $("exitWindowWrap").hidden = !isAnd;
}
document.querySelectorAll('input[name="entryLogic"]').forEach(r => r.addEventListener("change", updateEntryWindowUI));
document.querySelectorAll('input[name="exitLogic"]').forEach(r => r.addEventListener("change", updateExitWindowUI));
updateEntryWindowUI();
updateExitWindowUI();

// ===== 右上角反馈按钮 =====
function initFeedback() {
  const btn = document.createElement("button");
  btn.id = "feedbackBtn";
  btn.className = "feedback-btn";
  btn.textContent = "反馈意见";
  btn.addEventListener("click", openFeedbackModal);
  document.body.appendChild(btn);
}

function openFeedbackModal() {
  let m = document.getElementById("feedbackModal");
  if (!m) {
    m = document.createElement("div");
    m.id = "feedbackModal";
    m.className = "modal-mask";
    m.innerHTML = `<div class="modal-box feedback-box">
        <div class="modal-title">意见反馈</div>
        <div class="modal-body">
          <div style="margin-bottom:8px;color:#8b949e;font-size:13px">欢迎提出建议、报bug或功能需求，提交后会同步到 GitHub Issue。</div>
          <textarea id="fbText" class="fb-text" placeholder="请描述你的反馈…" rows="5"></textarea>
          <input id="fbContact" class="fb-contact" placeholder="联系方式（选填，如邮箱/微信）" />
        </div>
        <div class="modal-foot">
          <button class="fb-cancel">取消</button>
          <button class="fb-submit modal-ok">提交反馈</button>
        </div>
      </div>`;
    document.body.appendChild(m);
    m.querySelector(".fb-cancel").addEventListener("click", () => m.style.display = "none");
    m.addEventListener("click", (e) => { if (e.target === m) m.style.display = "none"; });
    m.querySelector(".fb-submit").addEventListener("click", submitFeedback);
  }
  m.style.display = "flex";
}

async function submitFeedback() {
  const text = $("fbText").value.trim();
  const contact = $("fbContact").value.trim();
  if (!text) { alert("请填写反馈内容"); return; }
  const m = $("feedbackModal");
  const submitBtn = m.querySelector(".fb-submit");
  submitBtn.disabled = true;
  submitBtn.textContent = "提交中…";
  _showLoading("正在提交反馈…");
  try {
    const res = await fetch("/api/feedback", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, contact })
    });
    const data = await res.json();
    await _sleep(1000);
    _hideLoading();
    if (_handleRateLimit(data)) { submitBtn.disabled = false; submitBtn.textContent = "提交反馈"; return; }
    if (!data.ok) {
      showModal("提交失败", data.error || "未知错误");
    } else {
      m.style.display = "none";
      const msg = data.issue_url
        ? `反馈已提交！GitHub Issue：<a href="${data.issue_url}" target="_blank" style="color:#58a6ff">${data.issue_url}</a>`
        : (data.note || "反馈已收到，感谢！");
      showModal("提交成功", msg);
      $("fbText").value = "";
      $("fbContact").value = "";
    }
  } catch (e) {
    _hideLoading();
    showModal("提交失败", e.message);
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = "提交反馈";
  }
}

initFeedback();
initFromUrlParams();
updatePosUI();
updateEntryWindowUI();
updateExitWindowUI();
// 首次自动跑一次默认示例；若 URL 带 symbol/start/end/preset，则自动跑对应回测。
runBacktest();
