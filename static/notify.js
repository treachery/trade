// trade-notify 前端逻辑：选标的 → 扫描(异步轮询) → 展示信号/估值/建议仓位；订阅保存与webhook。
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const CAT_NAME = { broad: "宽基", a500: "A500成分股", custom: "自定义", holding: "持仓" };

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

  // ---------- 渲染标的复选 ----------
  function renderCheckList(elId, items, checkedAll) {
    const box = $(elId);
    box.innerHTML = "";
    items.forEach((it) => {
      const lab = document.createElement("label");
      lab.className = "chk";
      lab.innerHTML = `<input type="checkbox" value="${it.symbol}" data-name="${it.name}" ${checkedAll ? "checked" : ""}>
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
  async function loadSymbols() {
    try {
      const r = await fetch("/api/notify/symbols?include_a500=1").then((x) => x.json());
      if (!r.ok) throw new Error(r.error || "加载标的失败");
      SYMBOLS = { broad: r.broad || [], a500: r.a500 || [] };
      renderCheckList("broadList", SYMBOLS.broad, true);
      $("a500Count").textContent = `（${SYMBOLS.a500.length} 只）`;
    } catch (e) {
      $("scanStatus").textContent = "标的加载失败：" + e.message;
    }
  }

  // ---------- 收集持仓（[[code, name], ...]）----------
  function collectHoldings() {
    const raw = $("holdings").value.trim();
    if (!raw) return [];
    return raw.split(/[,，\s]+/).filter(Boolean).map((c) => [c.trim(), c.trim()]);
  }

  // ---------- 收集当前选择的标的 ----------
  function collectSymbols() {
    const out = [];
    $("broadList").querySelectorAll("input:checked").forEach((b) =>
      out.push([b.value, b.dataset.name, "broad"]));
    if ($("incA500").checked) {
      SYMBOLS.a500.forEach((it) => out.push([it.symbol, it.name, "a500"]));
    }
    const custom = $("customSyms").value.trim();
    if (custom) {
      custom.split(/[,，\s]+/).filter(Boolean).forEach((c) =>
        out.push([c.trim(), c.trim(), "custom"]));
    }
    return out;
  }

  // ---------- 扫描 ----------
  async function startScan() {
    const symbols = collectSymbols();
    if (!symbols.length) {
      $("scanStatus").textContent = "请至少选择一个标的";
      return;
    }
    $("scanBtn").disabled = true;
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
      pollStatus(r.task_id, r.total);
    } catch (e) {
      $("scanStatus").className = "status error";
      $("scanStatus").textContent = "扫描失败：" + e.message;
      $("scanBtn").disabled = false;
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
        $("scanStatus").textContent =
          `扫描中 ${r.done}/${total}（${pct}%）${r.current ? " · " + r.current : ""} · 已发现 ${r.results.length} 个有信号`;
        lastResults = r.results || [];
        renderResults();
        if (r.finished) {
          clearInterval(polling);
          polling = null;
          $("scanBtn").disabled = false;
          $("scanStatus").textContent = `扫描完成：${total} 个标的，${lastResults.length} 个出现信号。`;
        }
      } catch (e) {
        clearInterval(polling);
        polling = null;
        $("scanBtn").disabled = false;
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

  function scoreHtml(s) {
    if (!s || s.score == null) return "";
    const v = s.score;
    const cls = v >= 60 ? "score-high" : v >= 45 ? "score-mid" : "score-low";
    const parts = [];
    if (s.buy) parts.push(`买入 +${s.buy}`);
    if (s.sell) parts.push(`卖出 -${s.sell}`);
    if (s.valuation) parts.push(`估值 ${s.valuation > 0 ? "+" : ""}${s.valuation}`);
    const detail = parts.length ? `<span class="score-detail">${parts.join(" · ")}</span>` : "";
    return `<div class="sc-score">
        <span class="score-badge ${cls}">推荐买入 ${v}</span>
        <span class="score-level">${s.level || ""}</span>${detail}
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

    // 排序：按「推荐买入分」从高到低；同分再按信号触发越近越前
    list.sort((a, b) => {
      const sa = (a.score && a.score.score != null) ? a.score.score : -1;
      const sb = (b.score && b.score.score != null) ? b.score.score : -1;
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
        ${scoreHtml(r.score)}
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
    $("a500Warn").style.display = $("incA500").checked ? "block" : "none";
    $("autoEnabled").checked = !!s.auto_enabled;
    $("holdings").value = Array.isArray(s.holdings)
      ? s.holdings.map((it) => (Array.isArray(it) ? it[0] : it)).join(",") : "";
    $("customSyms").value = "";
    // 标的勾选：有保存的 symbols 则按其恢复，否则全选宽基
    if (Array.isArray(s.symbols) && s.symbols.length) {
      const set = new Set(s.symbols.map((it) => (Array.isArray(it) ? it[0] : it)));
      $("broadList").querySelectorAll("input").forEach((b) => (b.checked = set.has(b.value)));
      const customCodes = s.symbols
        .filter((it) => (Array.isArray(it) ? it[2] : "") === "custom").map((it) => it[0]);
      if (customCodes.length) $("customSyms").value = customCodes.join(",");
    } else {
      $("broadList").querySelectorAll("input").forEach((b) => (b.checked = true));
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
    const s = { name, symbols: null, include_a500: true, lookback_days: 10,
                webhook: "", email: "", auto_enabled: false, holdings: [] };
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
    const symbols = collectSymbols().filter((s) => s[2] !== "a500"); // a500 由开关控制
    const newName = $("subName").value.trim();
    if (!newName) { subToast("请先填写订阅名称", false); $("subName").focus(); return; }
    const body = {
      old_name: curName,                // 用于定位/改名
      name: newName,
      symbols: symbols.length ? symbols : null,
      include_a500: $("incA500").checked,
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
    $("scanBtn").addEventListener("click", startScan);
    $("saveSub").addEventListener("click", saveSubscription);
    $("testWebhook").addEventListener("click", testWebhook);
    $("subSelect").addEventListener("change", onSelectSub);
    $("newSub").addEventListener("click", newSubscription);
    $("delSub").addEventListener("click", delSubscription);
    $("broadAll").addEventListener("click", () => toggleAll("broadList"));
    $("incA500").addEventListener("change", () => {
      $("a500Warn").style.display = $("incA500").checked ? "block" : "none";
    });
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
