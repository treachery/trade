"""股票策略回测平台 - Flask 入口。

运行：
  python app.py
然后浏览器打开 http://127.0.0.1:5000
"""
import traceback

from flask import Flask, render_template, request, jsonify

from backtest import load_kline, load_pe, run_backtest, run_optimization, StrategyConfig, INDEX_PE_PROXY

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/help")
def help_page():
    return render_template("help.html")


@app.route("/api/backtest", methods=["POST"])
def api_backtest():
    try:
        body = request.get_json(force=True) or {}
        symbol = str(body.get("symbol", "000001")).strip()
        start = str(body.get("start", "2015-03-01")).strip()
        end = str(body.get("end", "2018-03-01")).strip()
        adjust = str(body.get("adjust", "qfq")).strip()
        if adjust not in ("qfq", "hfq", ""):
            adjust = "qfq"
        initial_capital = float(body.get("initial_capital", 100000))
        commission = float(body.get("commission", 0.0005))
        margin_rate = float(body.get("margin_rate", 0.0699))

        config = StrategyConfig.from_dict(body.get("strategy", {}))

        df = load_kline(symbol, start, end, adjust=adjust)
        if df is None or df.empty:
            return jsonify({"ok": False, "error": f"未获取到 {symbol} 在 {start}~{end} 的数据，请检查代码或区间。"})

        # PE 仓位管理所需的市盈率序列(仅支持的指数有)
        pe_series = None
        pe_available = False
        pe_proxy = INDEX_PE_PROXY.get(str(symbol).strip().lower())
        if config.position.get("type") == "pe_percentile":
            pe_df = load_pe(symbol, start, end)
            if pe_df is not None and not pe_df.empty:
                pe_series = dict(zip(pe_df["date"], pe_df["pe"]))
                pe_available = True

        result = run_backtest(df, config, initial_capital=initial_capital,
                              commission=commission, pe_series=pe_series,
                              margin_rate=margin_rate)
        result["meta"] = {
            "symbol": symbol,
            "start": start,
            "end": end,
            "adjust": adjust,
            "rows": len(df),
            "strategy": config.to_dict(),
            "pe_available": pe_available,
            "pe_proxy": pe_proxy if pe_available else None,
        }
        return jsonify(result)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"})


@app.route("/api/optimize", methods=["POST"])
def api_optimize():
    try:
        body = request.get_json(force=True) or {}
        symbol = str(body.get("symbol", "399001")).strip()
        start = str(body.get("start", "2015-01-01")).strip()
        end = str(body.get("end", "2025-10-31")).strip()
        adjust = str(body.get("adjust", "qfq")).strip()
        if adjust not in ("qfq", "hfq", ""):
            adjust = "qfq"
        initial_capital = float(body.get("initial_capital", 100000))
        commission = float(body.get("commission", 0.0005))
        top_n = int(body.get("top_n", 10))
        min_trades = int(body.get("min_trades", 10))

        df = load_kline(symbol, start, end, adjust=adjust)
        if df is None or df.empty:
            return jsonify({"ok": False, "error": f"未获取到 {symbol} 在 {start}~{end} 的数据。"})

        result = run_optimization(df, initial_capital=initial_capital, commission=commission,
                                  top_n=top_n, min_trades=min_trades)
        result["meta"] = {"symbol": symbol, "start": start, "end": end, "rows": len(df)}
        return jsonify(result)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    import os
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("DEBUG", "1") == "1"
    app.run(host=host, port=port, debug=debug, threaded=True)
