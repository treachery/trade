# BE-022 信号扫描 API — 实现文档

## 1. 模块位置

`pattern_matching/signals.py` + `app.py` 路由

## 2. 数据结构

### 2.1 API 入参

```json
POST /api/pattern/signals
{
  "symbol": "sh000300",
  "as_of_date": "2026-06-29",
  "adjust": "qfq",
  "lookback_years": 5
}
```

### 2.2 API 出参

```json
{
  "ok": true,
  "symbol": "sh000300",
  "as_of_date": "2026-06-29",
  "last_close": 3866.21,
  "market_regime": "RANGE",
  "volatility_regime": "LOW",
  "buy_signals": [...],
  "sell_signals": [],
  "has_signal": true,
  "has_buy_signal": true,
  "features_snapshot": {...}
}
```

## 3. 接口

```http
POST /api/pattern/signals
```

## 4. 验收结果

```
buy=1 sell=0 regime=RANGE
```
