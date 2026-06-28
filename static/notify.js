// trade-notify 前端逻辑：选标的 → 扫描(异步轮询) → 展示信号/估值/建议仓位；订阅保存与webhook。
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const CAT_NAME = {
    broad: "宽基", a500: "A500成分股",
    hk_index: "港股指数", hk: "港股大市值",
    us_index: "美股指数", sp500: "标普500成分股",
    custom: "自定义", holding: "持仓",
  };

  // 按钮点击瞬时高亮反馈
  function flashBtn(btn) {
    if (!btn) return;
    btn.classList.add("flash");
    setTimeout(() => btn.classList.remove("flash"), 220);
  }
  // 订阅区即时提示(紧挨按钮，绿/红)
  let _toastTimer = null;
  function subToast(msg, ok) {
    const el = $("subToast");
    if (!el) return;
    el.textContent = msg;
    el.className = "inline-toast show " + (ok === false ? "err" : "ok");
    el.style.display = "block";
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(() => { el.classList.remove("show"); }, 2600);
  }

  let SYMBOLS = { broad: [], a500: [] };
  let lastResults = [];
  let curFilter = "all";
  let polling = null;
  let currentScanTaskId = null;
  let scanRunning = false;
  const SCAN_BTN_IDLE_TEXT = "▶ 立即扫描";
  const SCAN_BTN_CANCEL_TEXT = "取消扫描";

  // ---------- 渲染标的复选 ----------
  function selectedValues(elId) {
    const box = $(elId);
    const inputs = Array.from(box.querySelectorAll("input[type=checkbox]"));
    if (!inputs.length) return null;
    return new Set(inputs.filter((b) => b.checked).map((b) => b.value));
  }

  function renderCheckList(elId, items, checkedState) {
    const box = $(elId);
    const checkedSet = checkedState instanceof Set ? checkedState : null;
    box.innerHTML = "";
    items.forEach((it) => {
      const lab = document.createElement("label");
      lab.className = "chk";
      const checked = checkedSet ? checkedSet.has(it.symbol) : !!checkedState;
      lab.innerHTML = `<input type="checkbox" value="${it.symbol}" data-name="${it.name}" ${checked ? "checked" : ""}>
        <span>${it.name}</span><span class="code">${it.symbol}</span>`;
      box.appendChild(lab);
    });
  }

  function toggleAll(elId) {
    const boxes = $(elId).querySelectorAll("input[type=checkbox]");
    const anyUnchecked = Array.from(boxes).some((b) => !b.checked);
    boxes.forEach((b) => (b.checked = anyUnchecked));
  }

  // ---------- 加载可选标的 ----------
  // 指数(轻量)默认加载；成分股(A500/标普500/港股大市值)按开关 query 控制是否拉取。
  async function loadSymbols() {
    try {
      const broadChecked = selectedValues("broadList");
      const hkIndexChecked = selectedValues("hkIndexList");
      const usIndexChecked = selectedValues("usIndexList");
      const incA500 = $("incA500").checked ? "1" : "0";
      const incSp500 = $("incSp500").checked ? "1" : "0";
      const incHk = $("incHk").checked ? "1" : "0";
      const url = `/api/notify/symbols?include_a500=${incA500}&include_sp500=${incSp500}&include_hk=${incHk}`;
      const r = await fetch(url).then((x) => x.json());
      if (!r.ok) throw new Error(r.error || "加载标的失败");
      SYMBOLS = {
        broad: r.broad || [], a500: r.a500 || [],
        hk_index: r.hk_index || [], us_index: r.us_index || [],
        sp500: r.sp500 || [], hk: r.hk || [],
      };
      renderCheckList("broadList", SYMBOLS.broad, broadChecked || true);
      renderCheckList("hkIndexList", SYMBOLS.hk_index, hkIndexChecked || true);
      renderCheckList("usIndexList", SYMBOLS.us_index, usIndexChecked || true);
      $("a500Count").textContent = `（${SYMBOLS.a500.length} 只）`;
      $("sp500Count").textContent = SYMBOLS.sp500.length ? `（${SYMBOLS.sp500.length} 只）` : "";
      $("hkCount").textContent = SYMBOLS.hk.length ? `（${SYMBOLS.hk.length} 只）` : "";
    } catch (e) {
      $("scanStatus").textContent = "标的加载失败：" + e.message;
    }
  }

  // 勾选 A500/标普500/港股大市值开关 -> 联网拉取名单(保留指数勾选状态)
  async function onA500Toggle() {
    $("a500Warn").style.display = $("incA500").checked ? "block" : "none";
    if (!$("incA500").checked) {
      $("a500Count").textContent = "";
      SYMBOLS.a500 = [];
      return;
    }
    subToast("正在加载中证A500成分股名单…", true);
    await loadSymbols();
    const n = (SYMBOLS.a500 || []).length;
    subToast(n ? `中证A500成分股名单已加载(${n}只)` : "中证A500名单加载完成", true);
  }

  async function onOverseasConsToggle() {
    const anyOn = $("incSp500").checked || $("incHk").checked;
    $("overseasWarn").style.display = anyOn ? "block" : "none";
    subToast("正在加载海外成分股名单…", true);
    await loadSymbols();
    const n = (SYMBOLS.sp500 || []).length + (SYMBOLS.hk || []).length;
    subToast(n ? `海外成分股名单已加载(${n}只)` : "名单加载完成", true);
  }

  // ---------- 收集持仓（[[code, name], ...]）----------
  function collectHoldings() {
    const raw = $("holdings").value.trim();
    if (!raw) return [];
    return raw.split(/[,，\s]+/).filter(Boolean).map((c) => [c.trim(), c.trim()]);
  }

  async function ensureDynamicSymbolsLoaded() {
    const needA500 = $("incA500").checked && !(SYMBOLS.a500 || []).length;
    const needSp500 = $("incSp500").checked && !(SYMBOLS.sp500 || []).length;
    const needHk = $("incHk").checked && !(SYMBOLS.hk || []).length;
    if (needA500 || needSp500 || needHk) {
      $("scanStatus").className = "status";
      $("scanStatus").textContent = "正在加载成分股名单…";
      await loadSymbols();
      if (needA500 && !(SYMBOLS.a500 || []).length) throw new Error("中证A500成分股列表为空");
      if (needSp500 && !(SYMBOLS.sp500 || []).length) throw new Error("标普500成分股列表为空");
      if (needHk && !(SYMBOLS.hk || []).length) throw new Error("港股大市值列表为空");
    }
  }

  // ---------- 收集当前选择的标的 ----------
  function collectSymbols() {
    const out = [];
    $("broadList").querySelectorAll("input:checked").forEach((b) =>
      out.push([b.value, b.dataset.name, "broad"]));
    $("hkIndexList").querySelectorAll("input:checked").forEach((b) =>
      out.push([b.value, b.dataset.name, "hk_index"]));
    $("usIndexList").querySelectorAll("input:checked").forEach((b) =>
      out.push([b.value, b.dataset.name, "us_index"]));
    if ($("incA500").checked) {
      SYMBOLS.a500.forEach((it) => out.push([it.symbol, it.name, "a500"]));
    }
    if ($("incSp500").checked) {
      (SYMBOLS.sp500 || []).forEach((it) => out.push([it.symbol, it.name, "sp500"]));
    }
    if ($("incHk").checked) {
      (SYMBOLS.hk || []).forEach((it) => out.push([it.symbol, it.name, "hk"]));
    }
    const custom = $("customSyms").value.trim();
    if (custom) {
      custom.split(/[,，\s]+/).filter(Boolean).forEach((c) =>
        out.push([c.trim(), c.trim(), "custom"]));
    }
    return out;
  }

  // ---------- 扫描 ----------
  function resetScanButton() {
    scanRunning = false;
    currentScanTaskId = null;
    $("scanBtn").disabled = false;
    $("scanBtn").textContent = SCAN_BTN_IDLE_TEXT;
  }

  async function cancelScan() {
    if (!currentScanTaskId) {
      resetScanButton();
      return;
    }
    $("scanBtn").disabled = true;
    try {
      const r = await fetch("/api/notify/scan_cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task_id: currentScanTaskId }),
      }).then((x) => x.json());
      if (!r.ok) throw new Error(r.error || "取消失败");
      if (polling) clearInterval(polling);
      polling = null;
      $("scanStatus").className = "status";
      $("scanStatus").textContent = "扫描已取消。";
    } catch (e) {
      $("scanStatus").className = "status error";
      $("scanStatus").textContent = "取消失败：" + e.message;
    } finally {
      resetScanButton();
    }
  }

  async function handleScanClick() {
    if (scanRunning) {
      await cancelScan();
    } else {
      await startScan();
    }
  }

  async function startScan() {
    $("scanBtn").disabled = true;
    try {
      await ensureDynamicSymbolsLoaded();
    } catch (e) {
      $("scanStatus").className = "status error";
      $("scanStatus").textContent = "成分股名单加载失败：" + e.message;
      $("scanBtn").disabled = false;
      return;
    }
    const symbols = collectSymbols();
    if (!symbols.length) {
      $("scanStatus").textContent = "请至少选择一个标的";
      $("scanBtn").disabled = false;
      return;
    }
    $("progressWrap").style.display = "block";
    $("barFill").style.width = "0%";
    $("scanStatus").className = "status";
    $("scanStatus").textContent = `准备扫描 ${symbols.length} 个标的…`;

    try {
      const r = await fetch("/api/notify/scan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          symbols,
          lookback_days: parseInt($("lookback").value) || 10,
          webhook: $("webhook").value.trim(),
          holdings: collectHoldings(),
        }),
      }).then((x) => x.json());
      if (!r.ok) throw new Error(r.error || "启动扫描失败");
      currentScanTaskId = r.task_id;
      scanRunning = true;
      $("scanBtn").disabled = false;
      $("scanBtn").textContent = SCAN_BTN_CANCEL_TEXT;
      pollStatus(r.task_id, r.total);
    } catch (e) {
      $("scanStatus").className = "status error";
      $("scanStatus").textContent = "扫描失败：" + e.message;
      resetScanButton();
    }
  }

  function pollStatus(taskId, total) {
    if (polling) clearInterval(polling);
    polling = setInterval(async () => {
      try {
        const r = await fetch("/api/notify/scan_status?task_id=" + taskId).then((x) => x.json());
        if (!r.ok) throw new Error(r.error || "查询失败");
        const pct = total ? Math.round((r.done / total) * 100) : 0;
        $("barFill").style.width = pct + "%";
        if (r.status === "backtesting") {
          $("scanStatus").textContent = `扫描完成，正在回测推荐值Top10… · 已发现 ${r.results.length} 个有信号`;
        } else {
          $("scanStatus").textContent =
            `扫描中 ${r.done}/${total}（${pct}%）${r.current ? " · " + r.current : ""} · 已发现 ${r.results.length} 个有信号`;
        }
        lastResults = r.results || [];
        renderResults();
        if (r.finished) {
          clearInterval(polling);
          polling = null;
          if (r.cancelled || r.status === "cancelled") {
            $("scanStatus").textContent = `扫描已取消：已完成 ${r.done}/${total}，已发现 ${lastResults.length} 个有信号。`;
          } else {
            $("scanStatus").textContent = `扫描完成：${total} 个标的，${lastResults.length} 个出现信号。`;
          }
          resetScanButton();
        }
      } catch (e) {
        clearInterval(polling);
        polling = null;
        resetScanButton();
        $("scanStatus").className = "status error";
        $("scanStatus").textContent = "查询失败：" + e.message;
      }
    }, 1200);
  }

  // ---------- 渲染结果 ----------
  function cardClass(r) {
    const e = r.entries && r.entries.length, x = r.exits && r.exits.length;
    if (e && x) return "has-both";
    if (x) return "has-exit";
    return "has-entry";
  }

  function peHtml(pe) {
    if (!pe) return `<span>估值：<b>无PE源</b></span>`;
    if (pe.percentile == null)
      return `<span>估值：<b>PE ${pe.pe}</b> · 当前TTM（近5年分位数据不足）</span>`;
    const cls = pe.percentile < 40 ? "pe-low" : pe.percentile > 60 ? "pe-high" : "";
    return `<span>估值：<b class="${cls}">PE ${pe.pe}</b> · 近5年 <b class="${cls}">${pe.percentile}%</b> 分位（${pe.level}）</span>`;
  }

  function scoreHtml(s, r) {
    if (!s || s.score == null) return "";
    const v = Number(s.score || 0);
    const cls = v > 0 ? "score-high" : v < 0 ? "score-low" : "score-mid";
    const trade = Number(s.trade || 0);
    const valuation = Number(s.valuation || 0);
    const parts = [
      `交易分数 ${trade > 0 ? "+" : ""}${trade.toFixed(1)}`,
      `估值分数 ${valuation > 0 ? "+" : ""}${valuation.toFixed(1)}`,
    ];
    if (s.conflict) parts.push("买卖信号冲突，交易分归零");
    const detail = `<span class="score-detail">${parts.join(" · ")}</span>`;
    let bt = "";
    const best = r && r.bt_best;
    if (best && best.ok && best.total_return != null) {
      const ret = Number(best.total_return);
      const rc = ret >= 0 ? "pos" : "neg";
      const srcName = best.source === "optimized" ? "一键寻优" : "形态推荐";
      const qs = new URLSearchParams({
        symbol: r.symbol, start: best.start, end: best.end, preset: "scan5y", autorun: "1",
      });

      // 单组回测明细渲染
      const grp = (b, title, isBest) => {
        if (!b) return "";
        if (!b.ok) return `<div class="bt5y-grp"><b>${title}</b><span class="neg">回测失败：${b.error || "—"}</span></div>`;
        const gr = Number(b.total_return), ge = Number(b.excess_return || 0), gd = Number(b.max_drawdown || 0);
        const grc = gr >= 0 ? "pos" : "neg", gec = ge >= 0 ? "pos" : "neg";
        return `<div class="bt5y-grp ${isBest ? "bt5y-grp-best" : ""}">
          <b>${isBest ? "⭐ " : ""}${title}${b.pattern ? `（形态：${b.pattern}）` : ""}</b>
          <span class="bt5y-strat">${b.strategy || ""}</span>
          <span>策略收益：<em class="${grc}">${gr >= 0 ? "+" : ""}${gr.toFixed(2)}%</em> · 相对：<em class="${gec}">${ge >= 0 ? "+" : ""}${ge.toFixed(2)}%</em></span>
          <span>年化 ${b.annualized}% · 最大回撤 <em class="neg">${gd.toFixed(2)}%</em></span>
          <span>交易 ${b.num_trades} · 胜率 ${b.win_rate}%</span>
        </div>`;
      };

      bt = `<span class="bt5y-wrap">
        <span class="bt5y-summary ${rc}">Top${best.rank} 推荐[${srcName}] ${ret >= 0 ? "+" : ""}${ret.toFixed(1)}%</span>
        <span class="bt5y-pop">
          <b>Top${best.rank}｜近5年两组策略回测对比</b>
          ${grp(r.bt_pattern, "形态推荐策略", r.bt_pattern && r.bt_pattern.recommended)}
          ${grp(r.bt_optimized, "一键寻优最优策略", r.bt_optimized && r.bt_optimized.recommended)}
          <a class="bt5y-load" href="/?${qs.toString()}">载入主页查看回测</a>
        </span>
      </span>`;
    }
    return `<div class="sc-score">
        <span class="score-badge ${cls}">推荐值 ${v.toFixed(1)}</span>
        <span class="score-level">${s.level || ""}</span>${bt}${detail}
      </div>`;
  }

  function chipsHtml(arr, kind) {
    if (!arr || !arr.length) return "";
    const cls = kind === "buy" ? "buy" : "sell";
    const items = arr.map((s) => {
      const ago = s.days_ago === 0 ? "今日" : s.days_ago + "日前";
      return `<span class="chip ${cls}">${s.label}<span class="ago">${ago}</span></span>`;
    }).join("");
    const label = kind === "buy" ? "入场信号" : "清仓信号";
    return `<div class="sig-block"><div class="sig-label">${label}</div><div class="chips">${items}</div></div>`;
  }

  function renderResults() {
    const box = $("cards");
    let list = lastResults.slice();
    if (curFilter === "entry") list = list.filter((r) => r.entries && r.entries.length);
    else if (curFilter === "exit") list = list.filter((r) => r.exits && r.exits.length);

    // 排序：按「推荐值」从高到低；同分再按信号触发越近越前
    list.sort((a, b) => {
      const sa = (a.score && a.score.score != null) ? a.score.score : -1e9;
      const sb = (b.score && b.score.score != null) ? b.score.score : -1e9;
      if (sa !== sb) return sb - sa;
      const am = Math.min.apply(null, (a.entries || []).concat(a.exits || []).map((s) => s.days_ago).concat([99]));
      const bm = Math.min.apply(null, (b.entries || []).concat(b.exits || []).map((s) => s.days_ago).concat([99]));
      return am - bm;
    });

    $("resultMeta").textContent = lastResults.length
      ? `共 ${lastResults.length} 个有信号标的，当前显示 ${list.length} 个`
      : "";

    if (!list.length) {
      box.innerHTML = "";
      $("emptyTip").style.display = "block";
      $("emptyTip").textContent = lastResults.length
        ? "当前筛选无匹配标的。" : "暂无结果，点左侧「立即扫描」开始。";
      return;
    }
    $("emptyTip").style.display = "none";
    box.innerHTML = list.map((r) => {
      const cat = CAT_NAME[r.category] || "标的";
      return `<div class="scard ${cardClass(r)}">
        <div class="sc-head">
          <div class="sc-name">${r.name || r.symbol}<span class="sc-code">${r.symbol}</span></div>
          <span class="sc-cat ${r.category || ""}">${cat}</span>
        </div>
        ${scoreHtml(r.score, r)}
        <div class="sc-meta">
          <span>最新价 <b>${r.last_close}</b>（${r.last_date}）</span>
          ${peHtml(r.pe)}
        </div>
        <div class="sc-meta"><span>建议仓位：<b>${r.suggest ? r.suggest.text : "—"}</b></span></div>
        ${chipsHtml(r.entries, "buy")}
        ${chipsHtml(r.exits, "sell")}
      </div>`;
    }).join("");
  }

  // ---------- 订阅列表(多组) ----------
  let SUBS = [];          // 全部订阅
  let curName = "";       // 当前选中订阅名

  // 把一组订阅的设置回填到表单
  function fillForm(s) {
    s = s || {};
    $("subName").value = s.name || "";
    $("lookback").value = s.lookback_days || 10;
    $("webhook").value = s.webhook || "";
    $("email").value = s.email || "";
    $("incA500").checked = s.include_a500 !== false;
    $("incSp500").checked = !!s.include_sp500;
    $("incHk").checked = !!s.include_hk;
    $("a500Warn").style.display = $("incA500").checked ? "block" : "none";
    $("overseasWarn").style.display = ($("incSp500").checked || $("incHk").checked) ? "block" : "none";
    $("autoEnabled").checked = !!s.auto_enabled;
    $("holdings").value = Array.isArray(s.holdings)
      ? s.holdings.map((it) => (Array.isArray(it) ? it[0] : it)).join(",") : "";
    $("customSyms").value = "";
    // 指数勾选(broad/港/美)：有保存的 symbols 则按其恢复，否则全选
    const lists = ["broadList", "hkIndexList", "usIndexList"];
    if (Array.isArray(s.symbols) && s.symbols.length) {
      const set = new Set(s.symbols.map((it) => (Array.isArray(it) ? it[0] : it)));
      lists.forEach((id) =>
        $(id).querySelectorAll("input").forEach((b) => (b.checked = set.has(b.value))));
      const customCodes = s.symbols
        .filter((it) => (Array.isArray(it) ? it[2] : "") === "custom").map((it) => it[0]);
      if (customCodes.length) $("customSyms").value = customCodes.join(",");
    } else {
      lists.forEach((id) =>
        $(id).querySelectorAll("input").forEach((b) => (b.checked = true)));
    }
  }

  function renderSubSelect() {
    const sel = $("subSelect");
    sel.innerHTML = SUBS.map((s) => {
      const flag = s.auto_enabled ? " ⏰" : "";
      return `<option value="${s.name}">${s.name}${flag}</option>`;
    }).join("");
    sel.value = curName;
  }

  async function loadSubscriptions() {
    try {
      const r = await fetch("/api/notify/subscriptions").then((x) => x.json());
      if (!r.ok || !Array.isArray(r.subscriptions) || !r.subscriptions.length) return;
      SUBS = r.subscriptions;
      if (!SUBS.some((s) => s.name === curName)) curName = SUBS[0].name;
      renderSubSelect();
      fillForm(SUBS.find((s) => s.name === curName));
    } catch (e) { /* ignore */ }
  }

  function onSelectSub() {
    curName = $("subSelect").value;
    const s = SUBS.find((x) => x.name === curName);
    if (s) { fillForm(s); subToast(`已切换到「${curName}」`, true); }
    // 切换订阅时复位删除确认态
    _delArmed = false;
    const db = $("delSub"); if (db) db.textContent = "🗑 删除当前";
  }

  function newSubscription() {
    flashBtn($("newSub"));
    let base = "新订阅", name = base, i = 1;
    while (SUBS.some((s) => s.name === name)) name = base + ++i;
    const s = { name, symbols: null, include_a500: true, include_sp500: false, include_hk: false,
                lookback_days: 10, webhook: "", email: "", auto_enabled: false, holdings: [] };
    SUBS.push(s);
    curName = name;
    renderSubSelect();
    fillForm(s);
    subToast("已新建，编辑后点保存生效", true);
    $("subName").focus();
    $("subName").select();
  }

  // 删除：两步确认(不依赖被拦截的 confirm)。第一次点变"再点一次确认"，3秒内未再点则还原
  let _delArmed = false, _delTimer = null;
  async function delSubscription() {
    const btn = $("delSub");
    flashBtn(btn);
    if (!curName) { subToast("没有可删除的订阅", false); return; }
    if (!_delArmed) {
      _delArmed = true;
      btn.textContent = "⚠ 再点一次确认删除";
      subToast(`将删除「${curName}」，再点一次确认`, false);
      clearTimeout(_delTimer);
      _delTimer = setTimeout(() => {
        _delArmed = false; btn.textContent = "🗑 删除当前";
      }, 3000);
      return;
    }
    _delArmed = false; clearTimeout(_delTimer); btn.textContent = "🗑 删除当前";
    btn.disabled = true;
    try {
      const r = await fetch("/api/notify/subscription?name=" + encodeURIComponent(curName),
        { method: "DELETE" }).then((x) => x.json());
      if (!r.ok) { subToast("删除失败：" + r.error, false); return; }
      const deleted = curName;
      SUBS = r.subscriptions || [];
      curName = SUBS.length ? SUBS[0].name : "";
      renderSubSelect();
      fillForm(SUBS.find((s) => s.name === curName));
      subToast(`✅ 已删除「${deleted}」`, true);
    } catch (e) {
      subToast("删除失败：" + e.message, false);
    } finally {
      btn.disabled = false;
    }
  }

  async function saveSubscription() {
    flashBtn($("saveSub"));
    // 成分股(a500/sp500/hk)由各自开关控制，不逐个存
    const consCats = new Set(["a500", "sp500", "hk"]);
    const symbols = collectSymbols().filter((s) => !consCats.has(s[2]));
    const newName = $("subName").value.trim();
    if (!newName) { subToast("请先填写订阅名称", false); $("subName").focus(); return; }
    const body = {
      old_name: curName,                // 用于定位/改名
      name: newName,
      symbols: symbols.length ? symbols : null,
      include_a500: $("incA500").checked,
      include_sp500: $("incSp500").checked,
      include_hk: $("incHk").checked,
      lookback_days: parseInt($("lookback").value) || 10,
      webhook: $("webhook").value.trim(),
      email: $("email").value.trim(),
      auto_enabled: $("autoEnabled").checked,
      holdings: collectHoldings(),
    };
    try {
      const r = await fetch("/api/notify/subscription", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }).then((x) => x.json());
      if (r.ok) {
        SUBS = r.subscriptions || SUBS;
        curName = newName;
        renderSubSelect();
        subToast(`✅ 订阅「${newName}」已保存`, true);
      } else {
        subToast("保存失败：" + r.error, false);
      }
    } catch (e) {
      subToast("保存失败：" + e.message, false);
    }
  }

  async function testWebhook() {
    flashBtn($("testWebhook"));
    const url = $("webhook").value.trim();
    const mail = $("email").value.trim();
    if (!url && !mail) { subToast("请先填写 Webhook 或 邮箱", false); return; }
    subToast("正在测试推送…", true);
    try {
      const r = await fetch("/api/notify/test_webhook", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ webhook: url, email: mail, holdings: collectHoldings() }),
      }).then((x) => x.json());
      subToast(r.ok ? "✅ 推送成功：" + r.msg : ("推送失败：" + r.msg), r.ok);
    } catch (e) {
      subToast("推送失败：" + e.message, false);
    }
  }

  // ---------- 加载最近一次结果 ----------
  async function loadLatest() {
    try {
      const r = await fetch("/api/notify/latest").then((x) => x.json());
      if (r.ok && r.latest && Array.isArray(r.latest.results)) {
        lastResults = r.latest.results;
        renderResults();
        if (lastResults.length || r.latest.time) {
          $("introNote").innerHTML =
            `最近一次扫描：<b>${r.latest.time || ""}</b>（${r.latest.source === "auto" ? "自动" : "手动"}，` +
            `共 ${r.latest.total} 个标的，${lastResults.length} 个有信号，近 ${r.latest.lookback_days} 交易日）。可点「立即扫描」刷新。`;
        }
      }
    } catch (e) { /* ignore */ }
  }

  // ---------- 事件绑定 ----------
  function bind() {
    $("scanBtn").addEventListener("click", handleScanClick);
    $("saveSub").addEventListener("click", saveSubscription);
    $("testWebhook").addEventListener("click", testWebhook);
    $("subSelect").addEventListener("change", onSelectSub);
    $("newSub").addEventListener("click", newSubscription);
    $("delSub").addEventListener("click", delSubscription);
    $("broadAll").addEventListener("click", () => toggleAll("broadList"));
    $("hkAll").addEventListener("click", () => toggleAll("hkIndexList"));
    $("usAll").addEventListener("click", () => toggleAll("usIndexList"));
    $("incA500").addEventListener("change", onA500Toggle);
    // 勾选A500/标普500/港股大市值成分股开关时，需联网按需拉取名单
    $("incSp500").addEventListener("change", onOverseasConsToggle);
    $("incHk").addEventListener("change", onOverseasConsToggle);
    document.querySelectorAll(".filters button").forEach((btn) =>
      btn.addEventListener("click", () => {
        document.querySelectorAll(".filters button").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        curFilter = btn.dataset.filter;
        renderResults();
      }));
  }

  // ---------- 初始化 ----------
  async function init() {
    bind();
    await loadSymbols();
    await loadSubscriptions();
    await loadLatest();
  }
  document.addEventListener("DOMContentLoaded", init);
})();
