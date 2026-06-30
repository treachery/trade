// pattern.js vFE030 - Canvas K-line chart + interactive similar list
window.app = window.app || {};

(function () {
  var g = function (id) { return document.getElementById(id); };
  var state = { data: null, visible: {}, fwd: {}, onlyMe: false };
  var SIM_COLORS = ["#f0883e","#a371f7","#3fb950","#d29922","#f85149","#58a6ff","#b62324","#2ea043","#9e6a03","#1f6feb"];
  var logLines = [];

  window._log = function (msg, cls) {
    logLines.push({ t: new Date().toLocaleTimeString(), m: msg, c: cls || "ok" });
    var lp = g("logPanel"), lc = g("logContent");
    if (lp) { lp.classList.add("show"); lc.innerHTML = logLines.map(function (x) { return '<div class="log-' + x.c + '">[' + x.t + '] ' + x.m + '</div>'; }).join(""); lp.scrollTop = lp.scrollHeight; }
    console.log(msg);
  };

  // ===== Analyze =====
  var _progressTimer = null;
  var _progressSteps = [
    { t: 0,   msg: "正在加载行情数据...",   detail: "从数据湖拉取标的及参考池K线" },
    { t: 3,   msg: "正在计算技术特征...",   detail: "提取19维特征向量 (t20/t40/t60/env)" },
    { t: 6,   msg: "正在逐级递进检索...",   detail: "t-20(Top100) -> t-40(Top50) -> t-60(Top10) -> 环境过滤" },
    { t: 9,   msg: "正在提取相似片段K线...", detail: "锚点前后各60日OHLC" },
    { t: 12,  msg: "正在评估候选策略...",   detail: "10个策略 x Bootstrap置信区间" },
    { t: 15,  msg: "仍在处理，请耐心等待...", detail: "数据量较大时需要更多时间" },
    { t: 25,  msg: "处理时间较长，正在进行本地形态匹配...", detail: "候选股票/片段较多时会更慢，但不会联网下载" },
    { t: 40,  msg: "仍在运行，未超时...",   detail: "正在本地计算相似度与策略评估" }
  ];
  function startProgress(btn) {
    var stepIdx = 0;
    if (_progressTimer) clearInterval(_progressTimer);
    _progressTimer = setInterval(function () {
      var elapsed = Math.round((Date.now() - btn._t0) / 1000);
      var cur = _progressSteps[stepIdx];
      if (stepIdx < _progressSteps.length - 1 && elapsed >= _progressSteps[stepIdx + 1].t) stepIdx++;
      cur = _progressSteps[stepIdx];
      btn.textContent = cur.msg;
      _log("[" + elapsed + "s] " + cur.msg + " " + (cur.detail || ""), "warn");
    }, 3000);
    _log("[0s] 开始分析...", "ok");
  }
  function stopProgress(btn) {
    if (_progressTimer) { clearInterval(_progressTimer); _progressTimer = null; }
    btn.textContent = "开始分析";
  }

  window.app.analyze = function () {
    var sym = (g("symbol").value || "").trim();
    if (!sym) { showErr("请输入标的代码"); return; }
    var rawRef = (g("refSymbols").value || "").trim();
    var refs = rawRef ? rawRef.split(",").map(function (s) { return s.trim(); }).filter(Boolean) : null;

    showErr(""); g("results").style.display = "none"; state.data = null; state.visible = {}; state.fwd = {}; state.onlyMe = false;
    var btn = g("analyzeBtn"); btn.disabled = true; btn._t0 = Date.now();
    startProgress(btn);

    var body = { symbol: sym, top_k: parseInt(g("topK").value) || 8, lookback_years: parseInt(g("lb").value) || 5,
                 as_of_date: g("asOfDate").value || "", reference_symbols: refs };

    var controller = new AbortController();
    var timeoutId = setTimeout(function () { controller.abort(); }, 120000);

    fetch("/api/pattern/analyze", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal: controller.signal })
      .then(function (r) { clearTimeout(timeoutId); if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (json) {
        btn.disabled = false; stopProgress(btn);
        if (!json.ok) { _log("分析失败: " + (json.error || "?"), "err"); showErr(json.error || "分析失败"); return; }
        var ms = Date.now() - btn._t0;
        var top = (json.retrieval && json.retrieval.final_top) || [];
        _log("完成! 耗时 " + (ms / 1000).toFixed(1) + "s | " + json.total_stocks + " 只股票, " + json.total_windows + " 个片段, Top" + top.length + " 相似", "ok");

        state.data = json;
        g("results").style.display = "block";
        renderAll();

        if (json.strategy_eval) { renderEval(json.strategy_eval); }
      })
      .catch(function (err) {
        clearTimeout(timeoutId);
        btn.disabled = false; stopProgress(btn);
        if (err.name === "AbortError") {
          _log("请求超时(120s)，可能数据量过大", "err");
          showErr("请求超时，请减少参考标的数量或缩短回溯年限");
        } else {
          _log("网络错误: " + (err.message || "无法连接"), "err");
          showErr("请求失败: " + (err.message || "请确认服务器运行"));
        }
      });
  };

  window.app.showOnlyMe = function () { state.onlyMe = !state.onlyMe; g("results").style.display && renderAll(); };

  // ===== Preload =====
  var _preloadTimer = null;

  // ===== Preload =====
  var _preloadTimer = null;
  var _preloadLogSeen = 0;
  var _preloadLastOk = 0;
  var _preloadLastTime = 0;
  var _stuckStock = "";

  function _stopPoll() {
    if (_preloadTimer) { clearInterval(_preloadTimer); _preloadTimer = null; }
  }

  function _startPoll(btn) {
    _stopPoll();
    _preloadTimer = setInterval(function () {
      fetch("/api/preload/status").then(function (r) { return r.json(); }).then(function (s) {
        if (s.running) {
          btn.textContent = "下载中 " + s.ok + "/" + s.total + " " + (s.speed || "") + " " + (s.total_data || "");
        } else {
          // 下载结束（完成或被停止）
          _stopPoll();
          btn.textContent = "预下载数据";
          var lastLog = (s.log && s.log.length > 0) ? s.log[s.log.length - 1] : "";
          if (lastLog.indexOf("[完成]") >= 0) {
            _log("预下载完成! 成功" + s.ok + " 失败" + s.fail + " 耗时" + s.elapsed + "s", "ok");
          } else if (lastLog.indexOf("[停止]") >= 0) {
            _log("下载已停止。成功" + s.ok + " 失败" + s.fail, "warn");
          }
          return;
        }
        // 打印新日志
        var logTotal = s.log_total || 0;
        if (logTotal > _preloadLogSeen) {
          var returnedCount = (s.log || []).length;
          var startIdx = Math.max(0, _preloadLogSeen - (logTotal - returnedCount));
          for (var i = startIdx; i < s.log.length; i++) {
            var cls = s.log[i].indexOf("失败") >= 0 || s.log[i].indexOf("超时") >= 0 ? "err" : "ok";
            _log(s.log[i], cls);
          }
          _preloadLogSeen = logTotal;
        }
        // 检测卡住
        if (s.ok > _preloadLastOk) {
          _preloadLastOk = s.ok; _preloadLastTime = Date.now(); _stuckStock = "";
        } else if (s.running && Date.now() - _preloadLastTime > 30000 && _stuckStock !== s.current) {
          _stuckStock = s.current;
          showErr("下载卡住！\n当前: " + s.current + "\n成功: " + s.ok + "/" + s.total + "\n\n后端会在20秒超时后自动跳过此标的。");
        }
      }).catch(function (e) {
        if (Date.now() - _preloadLastTime > 10000) {
          _stopPoll();
          btn.textContent = "预下载数据";
          showErr("服务器无响应！\n" + e.message + "\n\n服务器可能已崩溃，请刷新页面重试");
        }
      });
    }, 1000);
  }

  // 页面加载时检查是否有正在进行的下载
  (function _checkPreloadOnLoad() {
    fetch("/api/preload/status").then(function (r) { return r.json(); }).then(function (s) {
      if (s.running) {
        var btn = g("preloadBtn");
        _preloadLogSeen = s.log_total || 0;
        _preloadLastOk = s.ok || 0;
        _preloadLastTime = Date.now();
        _stuckStock = s.current || "";
        _log("检测到下载进行中，恢复进度显示", "warn");
        _startPoll(btn);
      }
    }).catch(function () {});
  })();

  var _preloadStarting = false;
  window.app.preload = function () {
    var btn = g("preloadBtn");
    // 状态1: 正在下载 → 点击=停止
    if (_preloadTimer) {
      _stopPoll();
      btn.textContent = "预下载数据";
      _log("正在停止下载...", "warn");
      fetch("/api/preload/stop", { method: "POST" }).catch(function () {});
      return;
    }
    // 状态2: 空闲 → 点击=开始下载
    if (_preloadStarting) return;
    _preloadStarting = true;
    _log("开始预下载10年K线+PE数据...", "ok");
    fetch("/api/preload/start", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ scope: "all" }) })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        _preloadStarting = false;
        if (!d.ok) {
          if (d.error && d.error.indexOf("正在进行中") >= 0) {
            _log("下载已在进行中，恢复进度显示", "warn");
          } else {
            _log("预下载启动失败: " + (d.error || "?"), "err");
            showErr("预下载启动失败: " + (d.error || "?"));
          }
          return;
        }
        _log("预下载已启动, 共 " + d.total + " 只标的 (再次点击按钮可停止)", "ok");
        _preloadLogSeen = 0;
        _preloadLastOk = 0;
        _preloadLastTime = Date.now();
        _stuckStock = "";
        _startPoll(btn);
      })
      .catch(function (e) {
        _preloadStarting = false;
        _log("预下载请求失败: " + e.message, "err");
        showErr("预下载请求失败: " + e.message);
      });
  };

  function showErr(m) { var eb = g("errorBox"); if (eb) { eb.textContent = m; m ? eb.classList.add("show") : eb.classList.remove("show"); } if (m) { var em = g("errModal"); var emm = g("errModalMsg"); if (em && emm) { emm.textContent = m; em.style.display = "flex"; } } }

  function pct(v) { if (v == null) return '<span style="color:#6e7681">' + String.fromCharCode(45, 45) + '</span>'; var c = v > 0 ? "pos" : "neg"; return '<span class="' + c + '">' + (v > 0 ? "+" : "") + v.toFixed(1) + "%</span>"; }

  // ===== Render All =====
  function renderAll() {
    var d = state.data; if (!d) return;
    renderTitle(d);
    renderPipeline(d);
    renderSimList(d);
    drawKline();
  }

  function renderTitle(d) {
    var s = d.signal_info || {};
    var tags = "";
    (s.buy_signals || []).forEach(function (x) { tags += '<span style="font-size:10px;padding:1px 6px;background:#3fb95022;color:#3fb950;border-radius:8px;margin:0 2px">' + x.label + '</span>'; });
    (s.sell_signals || []).forEach(function (x) { tags += '<span style="font-size:10px;padding:1px 6px;background:#f8514922;color:#f85149;border-radius:8px;margin:0 2px">' + x.label + '</span>'; });
    g("chartTitle").innerHTML = d.symbol + " | " + (d.as_of_date || "") + " | 收盘 " + (s.last_close || "--") + " | " + (s.market_regime || "") + " | " + tags;
  }

  function renderPipeline(d) {
    var ret = d.retrieval; if (!ret || !ret.stages) return;
    var h = "";
    var LABELS = { shape5: "5日形态", shape10: "10日形态", shape20: "20日形态", shape30: "30日形态", final_score: "综合评分" };
    ["shape5","shape10","shape20","shape30","final_score"].forEach(function (k) {
      var s = ret.stages[k]; if (!s) return;
      h += '<span>' + (LABELS[k] || k) + ": " + s.input + "->" + s.output + "</span>";
    });
    h += '<span>top: ' + ret.final_top.length + "</span>";
    g("pipeline").innerHTML = h;
  }

  function renderSimList(d) {
    var top = (d.retrieval || {}).final_top || [];
    var el = g("simList"); if (!el) return;
    if (!top.length) { el.innerHTML = '<div style="color:#6e7681;padding:10px">无相似片段</div>'; return; }
    var h = "";
    top.forEach(function (f, i) {
      if (state.visible[i] === undefined) state.visible[i] = true;
      var c = SIM_COLORS[i % SIM_COLORS.length];
      var r = f.fwd_returns || {};
      h += '<div class="sim-item" style="border-left:3px solid ' + c + '">';
      h += '<div class="sim-head"><span class="sim-code">#' + f.rank + " " + f.symbol + "</span>";
      h += '<span class="sim-sim" style="color:' + c + '">' + (f.similarity * 100).toFixed(1) + "%</span></div>";
      h += '<div class="sim-date">锚定日: ' + f.anchor_date + " | 入场价: " + (f.entry_price || "--") + "</div>";
      // 拟合度三项：形态相似 / 拟合度(RMSE) / 信号相似——直观显示"到底有多像"
      var fitTxt = "";
      if (f.shape_similarity !== undefined) fitTxt += "形态" + (f.shape_similarity * 100).toFixed(0) + "%";
      if (f.fit_similarity !== undefined) fitTxt += " · 拟合" + (f.fit_similarity * 100).toFixed(0) + "%";
      if (f.rmse_pct !== undefined) fitTxt += "(RMSE " + f.rmse_pct + "%)";
      if (f.signal_similarity !== undefined) fitTxt += " · 信号" + (f.signal_similarity * 100).toFixed(0) + "%";
      if (fitTxt) h += '<div class="sim-fit" style="color:#6e7681;font-size:11px">' + fitTxt + "</div>";
      h += '<div class="sim-ret">5d: ' + pct(r.r_5d) + " 10d: " + pct(r.r_10d) + " 20d: " + pct(r.r_20d) + "</div>";
      h += '<div class="sim-act">';
      h += '<label><input type="checkbox" class="sim-cb" data-i="' + i + '" ' + (state.visible[i] ? "checked" : "") + '> 叠加</label>';
      h += '<button class="btn-fwd sim-fb" data-i="' + i + '">' + (state.fwd[i] ? "隐藏前瞻" : "前瞻走势") + "</button>";
      h += '</div></div>';
    });
    el.innerHTML = h;

    el.querySelectorAll(".sim-cb").forEach(function (cb) {
      cb.addEventListener("change", function () { state.visible[parseInt(cb.dataset.i)] = cb.checked; drawKline(); });
    });
    el.querySelectorAll(".sim-fb").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var i = parseInt(btn.dataset.i); state.fwd[i] = !state.fwd[i];
        btn.textContent = state.fwd[i] ? "隐藏前瞻" : "前瞻走势";
        drawKline();
      });
    });
  }

  // ===== Canvas K-line =====
  function drawKline() {
    var d = state.data; if (!d) return;
    var canvas = g("klineCanvas"); if (!canvas) return;
    var ctx = canvas.getContext("2d");
    var H = canvas.height;

    var qk = d.query_kline; if (!qk || !qk.ohlc || !qk.ohlc.length) return;
    var top = (d.retrieval || {}).final_top || [];
    var N = qk.ohlc.length;

    // Compute total bars needed: query bars + max forward path
    var maxFwd = 0;
    top.forEach(function (f, i) {
      if (state.fwd[i] && f.fwd_path) maxFwd = Math.max(maxFwd, f.fwd_path.length);
    });
    var totalBars = N + maxFwd;

    // Resize canvas to container width
    var box = canvas.parentElement;
    var cw = (box && box.clientWidth > 100) ? box.clientWidth - 20 : 800;
    canvas.width = cw;
    var W = cw;

    ctx.fillStyle = "#0d1117"; ctx.fillRect(0, 0, W, H);

    // Query anchor close price (for price alignment with similar fragments)
    var qAnchorClose = qk.ohlc[N - 1][1];

    // Compute price range across all visible data (similar fragments are price-scaled)
    var allOHLC = qk.ohlc.slice();
    top.forEach(function (f, i) {
      if (!state.visible[i] || state.onlyMe) return;
      var kl = f.kline_ohlc || []; if (!kl.length) return;
      var off = f.anchor_offset || 0;
      var ac = kl[off]; if (!ac) return;
      var sAnchorClose = ac[1];
      var scale = sAnchorClose > 0 ? qAnchorClose / sAnchorClose : 1;
      kl.forEach(function (o) { allOHLC.push([o[0] * scale, o[1] * scale, o[2] * scale, o[3] * scale]); });
      if (state.fwd[i] && f.fwd_path) {
        f.fwd_path.forEach(function (r) { var p = qAnchorClose * (1 + r / 100); allOHLC.push([p, p, p, p]); });
      }
    });
    if (!allOHLC.length) return;
    var hi = -Infinity, lo = Infinity;
    allOHLC.forEach(function (o) { hi = Math.max(hi, o[3]); lo = Math.min(lo, o[2]); });
    var pad = (hi - lo) * 0.1 || 10; hi += pad; lo -= pad;
    if (hi <= lo) hi = lo + 1;
    var yr = H - 40, y0 = 10;
    function y(v) { return y0 + (1 - (v - lo) / (hi - lo)) * yr; }

    // x-axis: bar i at x = i * xStep, total totalBars bars
    var barPad = 1;
    var xStep = W / (totalBars + 1);
    var barW = Math.max(1, xStep - barPad);

    // Anchor position = last query bar = (N-1) * xStep
    var ax = (N - 1) * xStep;

    // Grid
    ctx.strokeStyle = "#21262d"; ctx.lineWidth = 0.5;
    for (var val = Math.ceil(lo); val <= Math.ceil(hi); val += Math.max(1, Math.ceil((hi - lo) / 8))) {
      var gy = y(val); ctx.beginPath(); ctx.moveTo(0, gy); ctx.lineTo(W, gy); ctx.stroke();
      ctx.fillStyle = "#6e7681"; ctx.font = "9px sans-serif"; ctx.fillText(val.toFixed(1), 2, gy - 2);
    }

    // Anchor line (vertical dashed at last query bar)
    ctx.strokeStyle = "#d29922"; ctx.lineWidth = 1.5; ctx.setLineDash([5, 4]);
    ctx.beginPath(); ctx.moveTo(ax + barW / 2, y0); ctx.lineTo(ax + barW / 2, y0 + yr); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "#d29922"; ctx.font = "bold 10px sans-serif";
    ctx.fillText("锚点日", ax + barW / 2 + 3, y0 + 12);

    // Query K-line (bars 0..N-1)
    for (var i = 0; i < N; i++) {
      var o = qk.ohlc[i]; var x = i * xStep;
      var isUp = o[1] >= o[0];
      ctx.strokeStyle = isUp ? "#26a69a" : "#ef5350";
      ctx.lineWidth = Math.max(0.5, barW * 0.3);
      ctx.beginPath(); ctx.moveTo(x + barW / 2, y(o[2])); ctx.lineTo(x + barW / 2, y(o[3])); ctx.stroke();
      var topY = y(isUp ? o[1] : o[0]), botY = y(isUp ? o[0] : o[1]);
      var bh = Math.max(1, Math.abs(botY - topY));
      ctx.fillStyle = isUp ? "#26a69a" : "#ef5350";
      ctx.fillRect(x, topY, barW, bh);
    }

    // Similar K-lines (price-aligned: anchor close prices match, anchor bar at ax)
    if (!state.onlyMe) {
      top.forEach(function (f, i) {
        if (!state.visible[i]) return;
        var c = SIM_COLORS[i % SIM_COLORS.length];
        var kl = f.kline_ohlc || []; if (!kl.length) return;
        var off = f.anchor_offset || 0;
        var ac = kl[off]; if (!ac) return;
        var sAnchorClose = ac[1];
        var scale = sAnchorClose > 0 ? qAnchorClose / sAnchorClose : 1;
        for (var j = 0; j < kl.length; j++) {
          var o2 = kl[j];
          var x2 = ax + (j - off) * xStep;
          if (x2 < -barW || x2 > W + barW) continue;
          var sO = o2[0] * scale, sC = o2[1] * scale, sL = o2[2] * scale, sH = o2[3] * scale;
          var isUp2 = sC >= sO;
          ctx.strokeStyle = c + "88"; ctx.lineWidth = 0.5;
          ctx.beginPath(); ctx.moveTo(x2 + barW / 2, y(sL)); ctx.lineTo(x2 + barW / 2, y(sH)); ctx.stroke();
          var ty = y(isUp2 ? sC : sO), by2 = y(isUp2 ? sO : sC);
          var bh2 = Math.max(1, Math.abs(by2 - ty));
          ctx.fillStyle = isUp2 ? c + "44" : c + "22";
          ctx.fillRect(x2, ty, barW, bh2);
        }

        // Forward path (dashed line, price-scaled, from anchor going right)
        if (!state.fwd[i]) return;
        var fw = f.fwd_path || []; if (!fw.length) return;
        ctx.strokeStyle = c; ctx.lineWidth = 2; ctx.setLineDash([4, 3]);
        ctx.beginPath();
        ctx.moveTo(ax + barW / 2, y(qAnchorClose));
        for (var k = 0; k < fw.length; k++) {
          var fp = qAnchorClose * (1 + fw[k] / 100);
          ctx.lineTo(ax + (k + 1) * xStep + barW / 2, y(fp));
        }
        ctx.stroke();
        ctx.setLineDash([]);
        // Label at end of forward line
        if (fw.length > 0) {
          var endP = qAnchorClose * (1 + fw[fw.length - 1] / 100);
          var endX = ax + fw.length * xStep + barW / 2;
          ctx.fillStyle = c; ctx.font = "bold 10px sans-serif";
          ctx.fillText((fw[fw.length - 1] > 0 ? "+" : "") + fw[fw.length - 1].toFixed(1) + "%", endX + 3, y(endP));
        }
      });
    }

    // Legend
    ctx.fillStyle = "#8b949e"; ctx.font = "10px sans-serif";
    ctx.fillText("当前标的", 8, H - 6);
    ctx.fillStyle = "#26a69a"; ctx.fillRect(70, H - 12, 10, 6);
    ctx.fillStyle = "#8b949e"; ctx.fillText("相似片段(半透明)", 85, H - 6);
    ctx.fillStyle = "#d29922"; ctx.fillText("| 锚点日", 195, H - 6);
    ctx.fillStyle = "#8b949e"; ctx.fillText("虚线=前瞻走势", 245, H - 6);
  }

  // ===== Eval =====
  var evalSort = { field: "robust_score", asc: false };
  function renderEval(ev) {
    var el = g("evalContent"); if (!el) return;
    if (!ev || !ev.ok) { el.innerHTML = '<div style="color:#6e7681;padding:10px">无评估结果</div>'; return; }
    var ss = ev.strategies || [], best = ev.best_strategy || {};
    state._evalData = ev;

    // Best card
    var h = '<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:8px">';
    h += '<div style="background:#0d1117;border:1px solid #3fb95055;border-radius:8px;padding:10px">';
    h += '<div style="font-size:11px;color:#8b949e">最佳策略</div>';
    h += '<div style="font-size:16px;font-weight:700;color:#3fb950;margin:4px 0">' + (best.strategy_name||"--") + '</div>';
    var cc = best.confidence === "HIGH" ? "#3fb950" : (best.confidence === "MED" ? "#d29922" : "#f85149");
    h += '<span style="font-size:11px;padding:2px 8px;background:' + cc + '22;color:' + cc + ';border-radius:4px">' + (best.confidence||"?") + '</span>';
    h += '</div>';
    h += '<div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:10px;font-size:11px">';
    h += '中位数收益: <b class="' + (best.median_return>0?'pos':'neg') + '">' + (best.median_return||0).toFixed(1) + '%</b> | ';
    h += '胜率: <b>' + (best.win_rate||0).toFixed(0) + '%</b><br>';
    // 相似度加权指标(K线越像的样本权重越高，更贴近"最像的样本说了算")
    if (best.weighted_mean_return !== undefined) {
      h += '<span title="按K线相似度加权的平均收益率(相似度高的样本权重更大)">⭐加权均值: <b class="' + (best.weighted_mean_return>0?'pos':'neg') + '">' + (best.weighted_mean_return||0).toFixed(1) + '%</b></span>';
      if (best.weighted_win_rate !== undefined) h += ' | <span title="按K线相似度加权的胜率">加权胜率: <b>' + (best.weighted_win_rate||0).toFixed(0) + '%</b></span>';
      h += '<br>';
    }
    h += '尾部风险(最差20%): <b class="neg">' + (best.worst_quantile_20||0).toFixed(1) + '%</b> | ';
    h += '最大回撤: <b class="neg">' + Math.abs(best.max_drawdown||0).toFixed(1) + '%</b><br>';
    h += 'Bootstrap 95%CI: [' + (best.bootstrap_lower||0).toFixed(1) + ', ' + (best.bootstrap_upper||0).toFixed(1) + ']<br>';
    h += '样本数: ' + best.sample_count + ' | 夏普: ' + (best.sharpe||0).toFixed(2) + ' | 卡玛: ' + (best.calmar||0).toFixed(2);
    h += '</div></div>';

    if (best.confidence === "LOW") h += '<div style="font-size:10px;color:#d29922;padding:4px 8px;background:#d2992211;border-radius:4px;margin-bottom:6px">警告: 样本数不足30，置信度低，仅供参考</div>';

    // Sortable table
    h += '<div style="font-size:10px;color:#6e7681;margin-bottom:4px">排序: ';
    ["robust_score","weighted_mean_return","median_return","win_rate","max_drawdown","sharpe","calmar"].forEach(function(f){
      h += '<a href="#" onclick="app.sortEval(\''+f+'\');return false" style="color:'+(evalSort.field===f?'#58a6ff':'#6e7681')+';margin:0 4px">'+f+'</a>';
    });
    h += '</div>';

    var sorted = ss.slice().sort(function(a,b){
      var va = a[evalSort.field] || 0, vb = b[evalSort.field] || 0;
      if (evalSort.field === "max_drawdown") { va = Math.abs(va); vb = Math.abs(vb); return evalSort.asc ? va - vb : vb - va; }
      return evalSort.asc ? va - vb : vb - va;
    });

    h += '<table><thead><tr>';
    [
      "#","策略","类别","样本","中位数",
      {label:"⭐加权均值", tip:"按K线相似度加权的平均收益率(相似度高的样本权重更大)"},
      {label:"加权胜率", tip:"按K线相似度加权的胜率"},
      "胜率","盈亏比","最大回撤","夏普","卡玛","Bootstrap 95%CI","评分","置信度","详情"
    ].forEach(function(x){
      if (typeof x === "string") h += '<th>'+x+'</th>';
      else h += '<th title="'+x.tip+'">'+x.label+'</th>';
    });
    h += '</tr></thead><tbody>';
    sorted.forEach(function(s,i){
      h += '<tr class="'+(i===0?"best":"")+'"><td>'+(i+1)+'</td><td>'+s.strategy_name+'</td>';
      h += '<td><span style="font-size:9px;padding:1px 4px;border-radius:3px;background:'+(s.category==='direct'?'#58a6ff33':(s.category==='risk'?'#a371f733':'#6e768133'))+';color:'+(s.category==='direct'?'#58a6ff':(s.category==='risk'?'#a371f7':'#8b949e'))+'">'+s.category+'</span></td>';
      h += '<td>'+s.sample_count+'</td>';
      h += '<td class="'+(s.median_return>0?'pos':'neg')+'">'+s.median_return+'%</td>';
      // 加权均值/加权胜率
      var wm = (s.weighted_mean_return!=null) ? s.weighted_mean_return : null;
      var ww = (s.weighted_win_rate!=null) ? s.weighted_win_rate : null;
      h += '<td class="'+(wm!=null && wm>0?'pos':'neg')+'" style="font-weight:600">'+(wm!=null?wm.toFixed(1)+'%':'--')+'</td>';
      h += '<td>'+(ww!=null?ww.toFixed(0)+'%':'--')+'</td>';
      h += '<td>'+s.win_rate+'%</td>';
      h += '<td>'+(s.profit_loss_ratio!=null?s.profit_loss_ratio.toFixed(2):'--')+'</td>';
      h += '<td class="neg">'+Math.abs(s.max_drawdown||0).toFixed(1)+'%</td>';
      h += '<td>'+s.sharpe.toFixed(2)+'</td><td>'+s.calmar.toFixed(2)+'</td>';
      h += '<td>['+(s.bootstrap_lower||0).toFixed(1)+','+(s.bootstrap_upper||0).toFixed(1)+']</td>';
      h += '<td style="font-weight:700">'+s.robust_score.toFixed(3)+'</td>';
      var bc = s.confidence==="HIGH"?"#3fb950":(s.confidence==="MED"?"#d29922":"#f85149");
      h += '<td><span style="color:'+bc+'">'+s.confidence+'</span></td>';
      h += '<td><a href="#" onclick="app.showReturns('+i+');return false" style="color:#58a6ff;font-size:10px">样本</a></td></tr>';
    });
    h += '</tbody></table>';

    // Returns detail (累计收益曲线 + 样本列表)
    h += '<div id="returnsDetail" style="display:none;margin-top:8px;padding:10px;background:#0d1117;border-radius:6px;border:1px solid #21262d">'
      +    '<div id="returnsDetailHead" style="font-size:11px;color:#c9d1d9;margin-bottom:6px"></div>'
      +    '<canvas id="equityCanvas" width="900" height="220" style="width:100%;background:#0a0d11;border-radius:4px;margin-bottom:8px"></canvas>'
      +    '<div id="returnsList" style="font-size:10px;color:#8b949e;max-height:120px;overflow:auto"></div>'
      +  '</div>';

    // Export
    h += '<div style="margin-top:8px"><button onclick="app.exportCSV()" style="padding:4px 10px;background:#21262d;color:#8b949e;border:1px solid #30363d;border-radius:4px;font-size:10px;cursor:pointer">导出CSV</button></div>';

    el.innerHTML = h;

    // Boxplot
    drawBoxplot(ev);
  }

  window.app.sortEval = function(field){evalSort.field=field;evalSort.asc=!evalSort.asc;if(state._evalData)renderEval(state._evalData);};
  window.app.showReturns = function(i){
    var d=state._evalData;if(!d)return;var s=d.strategies[i];if(!s||!s.returns)return;
    var el=g("returnsDetail");el.style.display="block";
    // 表头：策略名 + 关键指标
    var head = '<b style="color:#58a6ff">'+s.strategy_name+'</b>'
             + ' · 样本'+(s.sample_count||s.returns.length)
             + ' · 累计'+((s.equity_curve&&s.equity_curve.length)?s.equity_curve[s.equity_curve.length-1].toFixed(1):0)+'%'
             + ' · 最大回撤<span class="neg">'+Math.abs(s.max_drawdown||0).toFixed(1)+'%</span>'
             + ' · 最痛单笔<span class="neg">'+Math.abs(s.single_trade_mdd_min||0).toFixed(1)+'%</span>'
             + ' · 中位<span class="'+((s.median_return||0)>0?'pos':'neg')+'">'+(s.median_return||0).toFixed(1)+'%</span>';
    g("returnsDetailHead").innerHTML = head;
    // 画累计曲线
    drawEquityCurve(s);
    // 样本列表(按时间排序)
    var dates = s.sorted_dates || [], syms = s.sorted_symbols || [], rets = s.sorted_returns || s.returns;
    var listHtml = '<div style="margin-bottom:4px;color:#6e7681">按 anchor_date 升序的 '+rets.length+' 笔模拟交易：</div>';
    rets.forEach(function(r, k){
      var date = dates[k] || '', sym = syms[k] || '';
      listHtml += '<span style="display:inline-block;margin:2px 4px;padding:1px 6px;background:#161b22;border-radius:3px;font-family:Consolas,monospace">'
                + '<span style="color:#6e7681">'+(date.slice(0,10))+'</span> '
                + '<span style="color:#8b949e">'+sym+'</span> '
                + '<span class="'+(r>0?'pos':'neg')+'">'+(r>0?'+':'')+r.toFixed(1)+'%</span>'
                + '</span>';
    });
    g("returnsList").innerHTML = listHtml;
  };

  function drawEquityCurve(s){
    var canvas = g("equityCanvas"); if(!canvas) return;
    var ctx = canvas.getContext("2d");
    var dpr = window.devicePixelRatio||1;
    var W = canvas.clientWidth||900, H = 220;
    canvas.width = W*dpr; canvas.height = H*dpr;
    canvas.style.height = H+"px";
    ctx.scale(dpr, dpr);
    ctx.clearRect(0,0,W,H);

    var eq = s.equity_curve || [], dd = s.drawdown_curve || [];
    if (!eq.length) {
      ctx.fillStyle="#6e7681"; ctx.font="12px sans-serif"; ctx.fillText("无累计数据", 20, 30); return;
    }

    // 边距
    var PAD_L = 50, PAD_R = 20, PAD_T = 20, PAD_B = 30;
    var plotW = W - PAD_L - PAD_R, plotH = H - PAD_T - PAD_B;

    // Y 轴范围：合并 equity 和 drawdown(都为 %)
    var allY = eq.concat(dd);
    var ymin = Math.min.apply(null, allY), ymax = Math.max.apply(null, allY);
    if (ymin === ymax) { ymin -= 1; ymax += 1; }
    var pad = (ymax - ymin) * 0.08;
    ymin -= pad; ymax += pad;

    function x2px(i) { return PAD_L + (i / Math.max(eq.length-1, 1)) * plotW; }
    function y2px(v) { return PAD_T + (1 - (v - ymin) / (ymax - ymin)) * plotH; }

    // 网格 + Y 轴标签
    ctx.strokeStyle = "#21262d"; ctx.lineWidth = 1;
    ctx.fillStyle = "#6e7681"; ctx.font = "10px Consolas, monospace";
    ctx.textAlign = "right"; ctx.textBaseline = "middle";
    var nGrid = 5;
    for (var k = 0; k <= nGrid; k++) {
      var v = ymin + (ymax - ymin) * k / nGrid;
      var y = y2px(v);
      ctx.beginPath(); ctx.moveTo(PAD_L, y); ctx.lineTo(W-PAD_R, y); ctx.stroke();
      ctx.fillText(v.toFixed(0)+"%", PAD_L-4, y);
    }
    // 零轴(粗一点)
    if (ymin < 0 && ymax > 0) {
      var y0 = y2px(0);
      ctx.strokeStyle = "#30363d"; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(PAD_L, y0); ctx.lineTo(W-PAD_R, y0); ctx.stroke();
    }

    // 回撤区域(填充)
    ctx.fillStyle = "rgba(248,81,73,0.15)";
    ctx.beginPath();
    ctx.moveTo(x2px(0), y2px(0));
    for (var i = 0; i < dd.length; i++) ctx.lineTo(x2px(i), y2px(dd[i]));
    ctx.lineTo(x2px(dd.length-1), y2px(0));
    ctx.closePath(); ctx.fill();

    // 回撤曲线(红)
    ctx.strokeStyle = "#f85149"; ctx.lineWidth = 1.2;
    ctx.beginPath();
    for (var i = 0; i < dd.length; i++) {
      var px = x2px(i), py = y2px(dd[i]);
      if (i===0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    }
    ctx.stroke();

    // 累计曲线(绿)
    ctx.strokeStyle = "#3fb950"; ctx.lineWidth = 1.8;
    ctx.beginPath();
    for (var i = 0; i < eq.length; i++) {
      var px = x2px(i), py = y2px(eq[i]);
      if (i===0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    }
    ctx.stroke();

    // 终点圆点
    var lastV = eq[eq.length-1];
    ctx.fillStyle = lastV > 0 ? "#3fb950" : "#f85149";
    ctx.beginPath(); ctx.arc(x2px(eq.length-1), y2px(lastV), 3.5, 0, Math.PI*2); ctx.fill();

    // 图例 + 终值标签
    ctx.fillStyle = "#3fb950"; ctx.font = "11px sans-serif"; ctx.textAlign = "left"; ctx.textBaseline = "top";
    ctx.fillText("● 累计收益曲线", PAD_L+4, PAD_T+2);
    ctx.fillStyle = "#f85149"; ctx.fillText("● 回撤曲线", PAD_L+120, PAD_T+2);
    ctx.fillStyle = "#c9d1d9"; ctx.textAlign = "right";
    ctx.fillText("终值: " + lastV.toFixed(1) + "%", W-PAD_R-4, PAD_T+2);

    // X 轴标签(首/中/末)
    ctx.fillStyle = "#6e7681"; ctx.font = "10px Consolas, monospace";
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    var dates = s.sorted_dates || [];
    if (dates.length) {
      var idxs = [0, Math.floor(dates.length/2), dates.length-1];
      idxs.forEach(function(idx, k){
        var d = dates[idx]; if (!d) return;
        ctx.fillText(d.slice(0,10), x2px(idx+1), H-PAD_B+4);
      });
    }
    ctx.textAlign = "center"; ctx.fillStyle = "#8b949e";
    ctx.fillText("第 N 笔交易(按 anchor_date 升序)", W/2, H-12);
  }
  window.app.exportCSV = function(){
    var d=state._evalData;if(!d)return;
    var rows=[['排名','策略','样本','中位数收益','加权均值','加权胜率','胜率','盈亏比','最大回撤','夏普','卡玛','评分','置信度']];
    d.strategies.forEach(function(s,i){
      rows.push([i+1,s.strategy_name,s.sample_count,s.median_return,
                 s.weighted_mean_return,s.weighted_win_rate,
                 s.win_rate,s.profit_loss_ratio,s.max_drawdown,s.sharpe,s.calmar,s.robust_score,s.confidence]);
    });
    var csv=rows.map(function(r){return r.join(',')}).join('\n');
    var blob=new Blob(['\uFEFF'+csv],{type:'text/csv;charset=utf-8'});
    var a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='strategy_eval.csv';a.click();
  };

  function drawBoxplot(ev){
    var canvas=g("evalBoxplot");if(!canvas)return;
    var ctx=canvas.getContext("2d");
    var W=canvas.width,H=canvas.height;
    ctx.fillStyle="#0d1117";ctx.fillRect(0,0,W,H);
    var ss=(ev.strategies||[]).slice(0,8);
    if(!ss.length)return;
    var allRets=[];ss.forEach(function(s){allRets=allRets.concat(s.returns||[])});
    if(!allRets.length)return;
    var lo=Math.min.apply(null,allRets),hi=Math.max.apply(null,allRets);
    var pad=(hi-lo)*0.1||5;lo-=pad;hi+=pad;if(hi<=lo)hi=lo+1;
    var n=ss.length,margin=50,barH=Math.min(20,(H-60)/n-4);
    function x(v){return margin+(v-lo)/(hi-lo)*(W-margin-20);}
    ctx.strokeStyle="#21262d";ctx.lineWidth=0.5;
    for(var v=Math.round(lo);v<=Math.ceil(hi);v+=Math.max(1,Math.round((hi-lo)/10))){var gx=x(v);ctx.beginPath();ctx.moveTo(gx,10);ctx.lineTo(gx,H-10);ctx.stroke();}
    ctx.fillStyle="#8b949e";ctx.font="9px sans-serif";
    ss.forEach(function(s,i){
      var rets=(s.returns||[]).slice().sort(function(a,b){return a-b;});
      var y=20+i*(barH+10),min=rets[0],q1=rets[Math.floor(rets.length*.25)],med=rets[Math.floor(rets.length*.5)],q3=rets[Math.floor(rets.length*.75)],max=rets[rets.length-1];
      ctx.strokeStyle="#58a6ff";ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(x(min),y+barH/2);ctx.lineTo(x(max),y+barH/2);ctx.stroke();
      ctx.fillStyle="#1f6feb44";ctx.fillRect(x(q1),y,x(q3)-x(q1),barH);
      ctx.strokeStyle="#58a6ff";ctx.lineWidth=2;ctx.strokeRect(x(q1),y,x(q3)-x(q1),barH);
      ctx.strokeStyle="#fff";ctx.lineWidth=2;ctx.beginPath();ctx.moveTo(x(med),y);ctx.lineTo(x(med),y+barH);ctx.stroke();
      ctx.fillStyle="#8b949e";ctx.textAlign="right";ctx.fillText(s.strategy_name.substring(0,12),margin-4,y+barH/2+3);
    });
  }


  _log("page ready");
})();
