#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TFZ Backtester — Trading From Zero
==================================
Replica la logica del scanner (main.py) sobre historico de 15m.
Seleccion intraday: movimiento >= 10% en 24h (el 7d ya no filtra).
Mide CADA setup contra TRES salidas y reporta WR + expectancia de cada una:
  1) SL 2% fijo + TP +6%              (lo actual)
  2) SL 2% fijo + TP en el 1er nivel
  3) SL debajo de la consolidacion + TP en el 1er nivel   (TFZ puro)
Ademas ejecuta un grid search sobre combinaciones de SL/TP.
Uso: python backtest.py
Requiere: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID. Opcional: COINGECKO_API_KEY.
"""
import os
import time
import csv
import logging
from datetime import datetime, timezone
import requests
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# ══════════════════════════════════════════════════════════
#  CONFIG  (mismos umbrales que main.py)
# ══════════════════════════════════════════════════════════
MIN_VOLUME_GLOBAL = 100_000_000  # $100M volumen global 24h (CoinGecko)
MIN_MOVE_PCT      = 10.0         # movimiento minimo 24h (intraday; el 7d ya no filtra)
SL_PCT            = 0.02         # 2%
TP_PCT            = 0.06         # 6%  (RR 1:3)
PIVOT_K          = 2
LEVEL_TOL_PCT    = 0.006
MIN_TOUCHES      = 2
MIN_LEVELS       = 2
GAP_TOP_COIN     = 0.020
GAP_ALTCOIN      = 0.030
MAX_DIST_TO_LEVEL = 0.15
TOP_COINS = {"BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX",
             "LINK", "TRX", "DOT", "MATIC", "LTC", "BCH", "TON"}
# Stablecoins: excluidas del universo
STABLES = {
    "USDT", "USDC", "DAI", "PYUSD", "RLUSD",
    "USD1", "USDG", "USDCV", "RUSD", "USDS",
    "FDUSD", "TUSD", "BUSD", "GUSD", "SUSD",
}
CONSOL_LOOKBACK  = 10
CONSOL_MAX_RANGE = 0.030
CONSOL_TO_LEVEL  = 0.030
WICK_LOOKBACK    = 30
MAX_MEAN_WICK    = 0.70
MAX_GAP_PCT      = 0.025
MAX_GAPS_ALLOWED = 3
LEVEL_WINDOW     = 200          # velas usadas para detectar niveles (= limit live)
CHANGE_LB        = 96           # 96 velas de 15m = 24h
OUTCOME_CANDLES  = 32           # 8h de ventana (igual que checker.py)
COOLDOWN_CANDLES = 8            # 2h entre setups del mismo symbol+side
BREAKEVEN_WR     = 25.0         # con RR 1:3 necesitas > 25% de aciertos
INTERVAL_MAP = {"15m": "Min15", "1h": "Min60", "4h": "Hour4", "1d": "Day1"}
BASE = "https://contract.mexc.com/api/v1/contract"
MAX_COINS = int(float(os.environ.get("MAX_COINS", 150)))   # aumentado de 80 a 150
# Grid de SL/TP a testear en paralelo
SL_GRID = [0.015, 0.020, 0.025, 0.030]   # 1.5%, 2%, 2.5%, 3%
TP_GRID = [0.040, 0.060, 0.080]           # 4%, 6%, 8%
FALLBACK_COINS = [
    "BTC_USDT", "ETH_USDT", "BNB_USDT", "SOL_USDT", "XRP_USDT",
    "DOGE_USDT", "ADA_USDT", "AVAX_USDT", "LINK_USDT", "DOT_USDT",
]
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "")
COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY", "")
# ══════════════════════════════════════════════════════════
#  API
# ══════════════════════════════════════════════════════════
def get_klines(symbol, interval, limit=2000):
    """Limite aumentado a 2000 (~21 dias en 15m) para mas muestra historica."""
    try:
        r = requests.get(
            f"{BASE}/kline/{symbol}",
            params={"interval": INTERVAL_MAP[interval], "limit": limit},
            timeout=15,
        )
        d = r.json().get("data", {})
        if not d or "close" not in d or not d["close"]:
            return None
        keys = ("open", "close", "high", "low", "vol", "amount", "time")
        return {k: [float(x) for x in d[k]] for k in keys if k in d}
    except Exception as e:  # noqa: BLE001
        log.error(f"{symbol} {interval}: {e}")
        return None
def build_universe():
    """
    Universo = monedas con VOLUMEN GLOBAL (CoinGecko) >= MIN_VOLUME_GLOBAL,
    como perpetuo USDT en MEXC. Excluye stablecoins. Cae a FALLBACK si falla.
    """
    try:
        key = f"&x_cg_demo_api_key={COINGECKO_API_KEY}" if COINGECKO_API_KEY else ""
        url = ("https://api.coingecko.com/api/v3/coins/markets"
               "?vs_currency=usd&order=volume_desc&per_page=250&page=1" + key)
        r = requests.get(url, timeout=20)
        data = r.json()
        coins, seen = [], set()
        if isinstance(data, list):
            for c in data:
                s = (c.get("symbol") or "").upper()
                if not s or s in seen:
                    continue
                seen.add(s)
                # Excluir stablecoins
                if s in STABLES:
                    continue
                if (c.get("total_volume") or 0) >= MIN_VOLUME_GLOBAL:
                    coins.append(f"{s}_USDT")
        if coins:
            log.info(f"Universo global: {len(coins)} monedas "
                     f"(vol >= ${MIN_VOLUME_GLOBAL/1e6:.0f}M, CoinGecko, sin stables)")
            return coins[:MAX_COINS]
    except Exception as e:  # noqa: BLE001
        log.error(f"build_universe global fallo: {e}")
    log.warning("Usando lista de respaldo (FALLBACK_COINS)")
    return FALLBACK_COINS
# ══════════════════════════════════════════════════════════
#  DETECCION DE NIVELES (identica a main.py)
# ══════════════════════════════════════════════════════════
def base_asset(symbol):
    return symbol.split("_")[0]
def _window(arrs, lo, hi):
    o, h, l, c = arrs["open"], arrs["high"], arrs["low"], arrs["close"]
    return [{"o": o[j], "h": h[j], "l": l[j], "c": c[j]} for j in range(lo, hi + 1)]
def pivots(candles, k, kind):
    res = []
    n = len(candles)
    for i in range(k, n - k):
        if kind == "high":
            v = candles[i]["h"]
            if all(candles[j]["h"] <= v for j in range(i - k, i + k + 1) if j != i):
                res.append(i)
        else:
            v = candles[i]["l"]
            if all(candles[j]["l"] >= v for j in range(i - k, i + k + 1) if j != i):
                res.append(i)
    return res
def cluster_levels(prices, tol):
    if not prices:
        return []
    prices = sorted(prices)
    clusters = [[prices[0]]]
    for p in prices[1:]:
        if abs(p - clusters[-1][-1]) / clusters[-1][-1] <= tol:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    return [(sum(c) / len(c), len(c)) for c in clusters]
def liquidity_levels(candles, side):
    kind = "high" if side == "LONG" else "low"
    idx = pivots(candles, PIVOT_K, kind)
    prices = [candles[i][kind[0]] for i in idx]
    levels = cluster_levels(prices, LEVEL_TOL_PCT)
    return [(p, n) for (p, n) in levels if n >= MIN_TOUCHES]
def is_untradeable(candles):
    recent = candles[-WICK_LOOKBACK:]
    wicks = []
    for c in recent:
        rng = c["h"] - c["l"]
        if rng <= 0:
            continue
        body = abs(c["c"] - c["o"])
        wicks.append((rng - body) / rng)
    if wicks and (sum(wicks) / len(wicks)) > MAX_MEAN_WICK:
        return True
    gaps = 0
    for i in range(1, len(recent)):
        prev = recent[i - 1]["c"]
        if prev and abs(recent[i]["o"] - prev) / prev > MAX_GAP_PCT:
            gaps += 1
    return gaps > MAX_GAPS_ALLOWED
def breakout_at_last(candles, side, nearest_level):
    """find_consolidation con el trigger en la ultima vela. Devuelve dict o None."""
    if len(candles) < CONSOL_LOOKBACK + 1:
        return None
    base = candles[-(CONSOL_LOOKBACK + 1):-1]
    trigger = candles[-1]
    highs = [c["h"] for c in base]
    lows = [c["l"] for c in base]
    hi, lo = max(highs), min(lows)
    if lo <= 0:
        return None
    rng_pct = (hi - lo) / lo
    if rng_pct > CONSOL_MAX_RANGE:
        return None
    trng = trigger["h"] - trigger["l"]
    tbody = abs(trigger["c"] - trigger["o"])
    if trng > 0 and tbody / trng < 0.4:
        return None
    if side == "LONG":
        if (nearest_level - hi) / nearest_level > CONSOL_TO_LEVEL:
            return None
        if not (trigger["c"] > hi):
            return None
    else:
        if (lo - nearest_level) / nearest_level > CONSOL_TO_LEVEL:
            return None
        if not (trigger["c"] < lo):
            return None
    return {"consol_high": hi, "consol_low": lo, "range_pct": round(rng_pct * 100, 2)}
# ══════════════════════════════════════════════════════════
#  SIMULACION
# ══════════════════════════════════════════════════════════
def simulate(direction, entry, tp, sl, future_highs, future_lows, future_closes=None):
    """
    Simula el resultado de un trade.
    Devuelve (outcome, max_float_pct, final_pct):
      - outcome: 'WIN', 'LOSS' o 'TIMEOUT'
      - max_float_pct: beneficio flotante maximo durante la ventana (%)
      - final_pct: P&L% al cerrar la ventana (solo util en TIMEOUT; None si WIN/LOSS)
    """
    max_float = 0.0
    for high, low in zip(future_highs, future_lows):
        if direction == "LONG":
            max_float = max(max_float, (high - entry) / entry * 100)
            if high >= tp:
                return "WIN", round(max_float, 2), None
            if low <= sl:
                return "LOSS", round(max_float, 2), None
        else:
            max_float = max(max_float, (entry - low) / entry * 100)
            if low <= tp:
                return "WIN", round(max_float, 2), None
            if high >= sl:
                return "LOSS", round(max_float, 2), None
    # TIMEOUT: calcula P&L al cierre de la ventana si tenemos closes
    final = None
    if future_closes:
        lc = future_closes[-1]
        final = round(((lc - entry) / entry if direction == "LONG"
                       else (entry - lc) / entry) * 100, 2)
    return "TIMEOUT", round(max_float, 2), final
# ══════════════════════════════════════════════════════════
#  BACKTEST POR MONEDA
# ══════════════════════════════════════════════════════════
def backtest_coin(symbol):
    results = []
    d15 = get_klines(symbol, "15m", limit=2000)
    time.sleep(0.2)
    min_needed = LEVEL_WINDOW + CHANGE_LB + OUTCOME_CANDLES + 5
    if not d15 or len(d15.get("close", [])) < min_needed:
        n_got = len(d15["close"]) if d15 else 0
        log.warning(f"{symbol}: datos insuficientes ({n_got} velas 15m)")
        return []
    closes = d15["close"]
    highs = d15["high"]
    lows = d15["low"]
    n = len(closes)
    max_gap = GAP_TOP_COIN if base_asset(symbol) in TOP_COINS else GAP_ALTCOIN
    last_signal = {}
    start = max(LEVEL_WINDOW, CHANGE_LB)
    end = n - OUTCOME_CANDLES - 1
    for idx in range(start, end):
        price = closes[idx]
        if price <= 0:
            continue
        # --- movimiento 24h (intraday) ------------------------------------- #
        ref24 = closes[idx - CHANGE_LB]
        if ref24 <= 0:
            continue
        ch24 = (price - ref24) / ref24 * 100.0
        if ch24 >= MIN_MOVE_PCT:
            side = "LONG"
        elif ch24 <= -MIN_MOVE_PCT:
            side = "SHORT"
        else:
            continue
        if side in last_signal and (idx - last_signal[side]) < COOLDOWN_CANDLES:
            continue
        w = _window(d15, idx - LEVEL_WINDOW + 1, idx)
        if is_untradeable(w):
            continue
        levels = liquidity_levels(w, side)
        if not levels:
            continue
        if side == "LONG":
            target = sorted([(p, c) for (p, c) in levels if p > price], key=lambda x: x[0])
        else:
            target = sorted([(p, c) for (p, c) in levels if p < price],
                            key=lambda x: x[0], reverse=True)
        if len(target) < MIN_LEVELS:
            continue
        l1, l2 = target[0][0], target[1][0]
        nearest = l1
        dist = abs(nearest - price) / price
        if dist > MAX_DIST_TO_LEVEL:
            continue
        gap = abs(l2 - l1) / l1
        if gap > max_gap:
            continue
        consol = breakout_at_last(w, side, nearest)
        if not consol:
            continue
        # --- datos futuros para simulacion --------------------------------- #
        fh = highs[idx + 1: idx + 1 + OUTCOME_CANDLES]
        fl = lows[idx + 1: idx + 1 + OUTCOME_CANDLES]
        fc = closes[idx + 1: idx + 1 + OUTCOME_CANDLES]
        if len(fh) < 4:
            continue
        if side == "LONG":
            sl_fix = price * (1 - SL_PCT)
            tp2 = price * (1 + TP_PCT)
            sl_tfz = consol["consol_low"]
        else:
            sl_fix = price * (1 + SL_PCT)
            tp2 = price * (1 - TP_PCT)
            sl_tfz = consol["consol_high"]
        tp1 = nearest
        sl_dist = abs(price - sl_tfz) / price * 100.0
        # --- 3 salidas principales ----------------------------------------- #
        outcome, max_float, final_pct = simulate(side, price, tp2, sl_fix, fh, fl, fc)
        outcome_tp1, _, _ = simulate(side, price, tp1, sl_fix, fh, fl, fc)
        outcome_tfz, _, _ = simulate(side, price, tp1, sl_tfz, fh, fl, fc)
        # --- grid search: todas las combinaciones SL x TP ------------------ #
        grid = {}
        for sl_g in SL_GRID:
            for tp_g in TP_GRID:
                sl_p = price * (1 + sl_g) if side == "SHORT" else price * (1 - sl_g)
                tp_p = price * (1 - tp_g) if side == "SHORT" else price * (1 + tp_g)
                g_out, _, _ = simulate(side, price, tp_p, sl_p, fh, fl, fc)
                grid[f"{sl_g*100:.1f}x{tp_g*100:.0f}"] = g_out
        last_signal[side] = idx
        ts = (datetime.utcfromtimestamp(d15["time"][idx]).strftime("%m-%d %H:%M")
              if "time" in d15 and idx < len(d15["time"]) else "?")
        row = {
            "symbol": symbol,
            "ts": ts,
            "direction": side,
            "dist_pct": round(dist * 100, 2),
            "sl_dist_pct": round(sl_dist, 2),
            "gap_pct": round(gap * 100, 2),
            "change_pct": round(ch24, 1),
            "consol_range_pct": consol["range_pct"],
            "outcome": outcome,
            "outcome_tp1": outcome_tp1,
            "outcome_tfz": outcome_tfz,
            "max_float_pct": max_float,
            "final_pct": final_pct,
        }
        row.update({f"grid_{k}": v for k, v in grid.items()})
        results.append(row)
    log.info(f"{symbol}: {len(results)} setups")
    return results
# ══════════════════════════════════════════════════════════
#  ANALISIS
# ══════════════════════════════════════════════════════════
def _wr(sub, key="outcome"):
    resolved = [r for r in sub if r[key] in ("WIN", "LOSS")]
    if not resolved:
        return (0, 0, 0.0)
    w = sum(1 for r in resolved if r[key] == "WIN")
    return (w, len(resolved), w / len(resolved) * 100)
def _expectancy(rows, key, win_gain_pct, loss_pct=None):
    """% medio por trade (sobre TODOS, incluyendo TIMEOUT=0)."""
    if not rows:
        return 0.0
    tot = 0.0
    for r in rows:
        o = r[key]
        if o == "WIN":
            tot += win_gain_pct(r) if callable(win_gain_pct) else win_gain_pct
        elif o == "LOSS":
            if loss_pct is None:
                tot += -SL_PCT * 100
            else:
                tot += -(loss_pct(r) if callable(loss_pct) else loss_pct)
    return tot / len(rows)
def _profit_factor(rows, key="outcome", win_pct=None, loss_pct=None):
    """
    Profit Factor = ganancia bruta total / perdida bruta total.
    Objetivo: PF > 1.5
    """
    gross_win = 0.0
    gross_loss = 0.0
    for r in rows:
        o = r.get(key)
        if o == "WIN":
            g = (win_pct(r) if callable(win_pct) else win_pct) if win_pct else TP_PCT * 100
            gross_win += g
        elif o == "LOSS":
            l = (loss_pct(r) if callable(loss_pct) else loss_pct) if loss_pct else SL_PCT * 100
            gross_loss += l
    return round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf")
def _equity_drawdown(rows, key="outcome"):
    """
    Equity curve empezando en $10,000 con el SL/TP del sistema principal.
    Devuelve (max_drawdown_pct, equity_final).
    """
    equity = 10_000.0
    peak = 10_000.0
    max_dd = 0.0
    for r in rows:
        o = r.get(key)
        if o == "WIN":
            equity *= (1 + TP_PCT)
        elif o == "LOSS":
            equity *= (1 - SL_PCT)
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak
        if dd > max_dd:
            max_dd = dd
    return round(max_dd * 100, 1), round(equity, 0)
def _grid_analysis(rows):
    """
    Para cada combinacion (SL, TP) del grid, calcula WR y expectancia.
    Devuelve dict {key: {wr, wins, resolved, exp}} y la mejor combinacion.
    """
    n_total = len(rows)
    if n_total == 0:
        return {}, None
    grid_keys = [f"{sl*100:.1f}x{tp*100:.0f}" for sl in SL_GRID for tp in TP_GRID]
    summary = {}
    for gk in grid_keys:
        col = f"grid_{gk}"
        sl_v = float(gk.split("x")[0]) / 100
        tp_v = float(gk.split("x")[1]) / 100
        wins_g = sum(1 for r in rows if r.get(col) == "WIN")
        losses_g = sum(1 for r in rows if r.get(col) == "LOSS")
        resolved_g = wins_g + losses_g
        wr_g = wins_g / resolved_g * 100 if resolved_g else 0
        # expectancia sobre todos los trades (TIMEOUT cuenta como 0)
        exp_g = ((wins_g * tp_v * 100) - (losses_g * sl_v * 100)) / n_total
        summary[gk] = {
            "wr": round(wr_g, 0),
            "wins": wins_g,
            "resolved": resolved_g,
            "exp": round(exp_g, 2),
        }
    best = max(summary.items(), key=lambda x: x[1]["exp"])
    return summary, best
def _timeout_analysis(rows):
    """Analiza los trades TIMEOUT para entender que paso."""
    timeout_rows = [r for r in rows if r.get("outcome") == "TIMEOUT"]
    if not timeout_rows:
        return None
    n = len(timeout_rows)
    avg_mf = sum(r.get("max_float_pct", 0) for r in timeout_rows) / n
    # Cuantos llegaron a mas del 50% del camino al TP (+3%)
    near_tp = sum(1 for r in timeout_rows
                  if r.get("max_float_pct", 0) >= TP_PCT * 100 / 2)
    # P&L promedio al cerrar la ventana (para los que tienen datos)
    final_rows = [r for r in timeout_rows if r.get("final_pct") is not None]
    avg_final = (sum(r["final_pct"] for r in final_rows) / len(final_rows)
                 if final_rows else None)
    return {
        "count": n,
        "avg_max_float": round(avg_mf, 2),
        "near_tp": near_tp,
        "avg_final": round(avg_final, 2) if avg_final is not None else None,
    }
def analyze(rows):
    total = len(rows)
    wins = sum(1 for r in rows if r["outcome"] == "WIN")
    losses = sum(1 for r in rows if r["outcome"] == "LOSS")
    timeouts = sum(1 for r in rows if r["outcome"] == "TIMEOUT")
    resolved = wins + losses
    wr = wins / resolved * 100 if resolved else 0.0
    # --- 3 salidas principales -------------------------------------------- #
    wr_tp2_all = _wr(rows, "outcome")
    wr_tp1_all = _wr(rows, "outcome_tp1")
    wr_tfz_all = _wr(rows, "outcome_tfz")
    exp_tp2 = _expectancy(rows, "outcome", TP_PCT * 100)
    exp_tp1 = _expectancy(rows, "outcome_tp1", lambda r: r["dist_pct"])
    exp_tfz = _expectancy(rows, "outcome_tfz",
                          lambda r: r["dist_pct"], lambda r: r["sl_dist_pct"])
    # --- Profit Factor ---------------------------------------------------- #
    pf_tp2 = _profit_factor(rows, "outcome")
    pf_tp1 = _profit_factor(rows, "outcome_tp1",
                             win_pct=lambda r: r["dist_pct"])
    pf_tfz = _profit_factor(rows, "outcome_tfz",
                             win_pct=lambda r: r["dist_pct"],
                             loss_pct=lambda r: r["sl_dist_pct"])
    # --- Equity curve + Drawdown ------------------------------------------ #
    max_dd, equity_final = _equity_drawdown(rows, "outcome")
    # --- LONG vs SHORT detallado ------------------------------------------ #
    by_dir_full = {}
    for d in ("LONG", "SHORT"):
        sub = [r for r in rows if r["direction"] == d]
        if sub:
            w, n_r, wr_d = _wr(sub)
            pf_d = _profit_factor(sub)
            exp_d = _expectancy(sub, "outcome", TP_PCT * 100)
            by_dir_full[d] = {
                "wins": w, "resolved": n_r, "total": len(sub),
                "wr": wr_d, "pf": pf_d, "exp": round(exp_d, 2),
            }
    # --- analisis legado por gap y distancia ------------------------------ #
    by_dir_tp1, by_dir_tfz = {}, {}
    for d in ("LONG", "SHORT"):
        sub = [r for r in rows if r["direction"] == d]
        if sub:
            by_dir_tp1[d] = _wr(sub, "outcome_tp1")
            by_dir_tfz[d] = _wr(sub, "outcome_tfz")
    by_dir = {}
    for d in ("LONG", "SHORT"):
        sub = [r for r in rows if r["direction"] == d]
        if sub:
            by_dir[d] = _wr(sub)
    by_gap = {}
    for lo, hi, lbl in [(0, 1, "0-1%"), (1, 2, "1-2%"), (2, 3, "2-3%")]:
        sub = [r for r in rows if lo <= r["gap_pct"] < hi]
        if sub:
            by_gap[lbl] = _wr(sub)
    by_dist = {}
    for lo, hi, lbl in [(0, 3, "0-3%"), (3, 7, "3-7%"), (7, 15.01, "7-15%")]:
        sub = [r for r in rows if lo <= r["dist_pct"] < hi]
        if sub:
            by_dist[lbl] = _wr(sub)
    # --- Grid y Timeout --------------------------------------------------- #
    grid_summary, best_grid = _grid_analysis(rows)
    timeout_stats = _timeout_analysis(rows)
    return {
        "total": total, "wins": wins, "losses": losses, "timeouts": timeouts,
        "resolved": resolved, "wr": wr,
        "by_dir": by_dir, "by_gap": by_gap, "by_dist": by_dist,
        "wr_tp2_all": wr_tp2_all, "wr_tp1_all": wr_tp1_all, "wr_tfz_all": wr_tfz_all,
        "exp_tp2": exp_tp2, "exp_tp1": exp_tp1, "exp_tfz": exp_tfz,
        "by_dir_tp1": by_dir_tp1, "by_dir_tfz": by_dir_tfz,
        "by_dir_full": by_dir_full,
        "pf_tp2": pf_tp2, "pf_tp1": pf_tp1, "pf_tfz": pf_tfz,
        "max_dd": max_dd, "equity_final": equity_final,
        "grid_summary": grid_summary, "best_grid": best_grid,
        "timeout_stats": timeout_stats,
    }
# ══════════════════════════════════════════════════════════
#  REPORTE TELEGRAM
# ══════════════════════════════════════════════════════════
def _fmt_section(title, d):
    if not d:
        return ""
    lines = [title]
    for lbl, (w, nn, wr) in sorted(d.items(), key=lambda x: -x[1][2]):
        lines.append(f"  {lbl}: {wr:.0f}% de aciertos ({w}/{nn} resueltas)")
    return "\n".join(lines)
def _fmt_pf(pf):
    if pf == float("inf"):
        return "inf (sin perdidas)"
    return f"{pf:.2f}"
def build_report(a, n_coins):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if a["total"] == 0:
        body = "Sin setups en el periodo analizado."
    else:
        wr = a["wr"]
        if a["resolved"] < 20:
            conclusion = (f"AVISO: solo {a['resolved']} operaciones resueltas — "
                          "muestra insuficiente para concluir.")
        elif wr >= 40:
            conclusion = f"FUERTE: {wr:.0f}% de aciertos (breakeven en 25%)."
        elif wr >= BREAKEVEN_WR:
            conclusion = f"RENTABLE: {wr:.0f}% de aciertos, por encima del 25% de breakeven."
        else:
            conclusion = f"NO RENTABLE: {wr:.0f}% de aciertos, por debajo del 25% de breakeven."
        w2, n2, p2 = a["wr_tp2_all"]
        w1, n1, p1 = a["wr_tp1_all"]
        wz, nz, pz = a["wr_tfz_all"]
        comparacion = "\n".join([
            "=== 3 salidas — comparacion ===",
            (f"1) SL2%+TP+6%:  WR {p2:.0f}% ({w2}/{n2})  exp {a['exp_tp2']:+.2f}%  PF {_fmt_pf(a['pf_tp2'])}"),
            (f"2) SL2%+TP niv: WR {p1:.0f}% ({w1}/{n1})  exp {a['exp_tp1']:+.2f}%  PF {_fmt_pf(a['pf_tp1'])}"),
            (f"3) TFZ puro:    WR {pz:.0f}% ({wz}/{nz})  exp {a['exp_tfz']:+.2f}%  PF {_fmt_pf(a['pf_tfz'])}"),
            "(PF objetivo: >1.5)",
        ])
        # Equity + Drawdown
        equity_line = (f"Equity ($10k inicial): ${a['equity_final']:,.0f}  "
                       f"|  Drawdown max: {a['max_dd']}%")
        # LONG vs SHORT detallado
        dir_lines = ["=== LONG vs SHORT ==="]
        for d, st in a["by_dir_full"].items():
            dir_lines.append(
                f"  {d}: WR {st['wr']:.0f}%  PF {_fmt_pf(st['pf'])}  "
                f"exp {st['exp']:+.2f}%/trade  ({st['wins']}/{st['resolved']} resueltas)"
            )
        dir_section = "\n".join(dir_lines)
        # Timeout
        to = a.get("timeout_stats")
        if to:
            to_lines = [f"=== TIMEOUT — {to['count']} de {a['total']} setups ==="]
            to_lines.append(f"  Float maximo medio: +{to['avg_max_float']}%  "
                            f"(cerca del TP: {to['near_tp']} trades)")
            if to["avg_final"] is not None:
                to_lines.append(f"  P&L al vencer la ventana: {to['avg_final']:+.2f}%")
            to_section = "\n".join(to_lines)
        else:
            to_section = ""
        # Grid
        gs = a.get("grid_summary", {})
        bg = a.get("best_grid")
        if gs and bg:
            best_key, best_val = bg
            sl_b = best_key.split("x")[0]
            tp_b = best_key.split("x")[1]
            grid_header = (f"=== Grid TP/SL  (mejor: SL{sl_b}%+TP{tp_b}%  "
                           f"WR {best_val['wr']:.0f}%  exp {best_val['exp']:+.2f}%) ===")
            grid_rows = ["       TP4%        TP6%        TP8%"]
            for sl_g in SL_GRID:
                row_parts = [f"SL{sl_g*100:.1f}%:"]
                for tp_g in TP_GRID:
                    gk = f"{sl_g*100:.1f}x{tp_g*100:.0f}"
                    v = gs.get(gk, {})
                    row_parts.append(f" {v.get('wr',0):.0f}%/{v.get('exp',0):+.2f}")
                grid_rows.append("  ".join(row_parts))
            grid_section = grid_header + "\n" + "\n".join(grid_rows)
        else:
            grid_section = ""
        body_parts = [
            (f"Aciertos (+6%): {wr:.0f}% — {a['wins']} ganadas / {a['losses']} perdidas / "
             f"{a['resolved']} resueltas"),
            f"Timeouts (8h sin tocar TP/SL): {a['timeouts']}  |  Total setups: {a['total']}",
            conclusion,
            "",
            comparacion,
            "",
            equity_line,
            "",
            dir_section,
            "",
            to_section,
            "",
            grid_section,
            "",
            _fmt_section("Por gap entre niveles:", a["by_gap"]),
            "",
            _fmt_section("Por distancia al nivel:", a["by_dist"]),
        ]
        body = "\n".join(x for x in body_parts if x is not None)
    parts_raw = [
        "📊 TFZ Backtest — Trading From Zero",
        f"Analizadas {n_coins} monedas en ~21 dias (velas de 15m)",
        "Stop Loss 2% · Take Profit 6% · RR 1:3 · breakeven WR > 25%",
        "",
        body,
        "",
        ts,
    ]
    return "\n".join(parts_raw)
def send_telegram(msg):
    """Envia el mensaje, dividiendolo si supera 4000 caracteres."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(msg)
        return
    # Dividir en partes de max 4000 chars por salto de linea
    chunks = []
    current = []
    current_len = 0
    for line in msg.splitlines():
        if current_len + len(line) + 1 > 4000 and current:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    for chunk in chunks:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": chunk,
                      "disable_web_page_preview": "true"},
                timeout=10,
            )
            if not r.ok:
                log.error(f"Telegram: {r.text}")
        except Exception as e:  # noqa: BLE001
            log.error(f"Telegram: {e}")
            print(chunk)
        time.sleep(0.5)
# ══════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════
def main():
    log.info("=== TFZ Backtester iniciando ===")
    log.info(f"SL={SL_PCT*100:.0f}%  TP={TP_PCT*100:.0f}%  RR=1:3  "
             f"move24h>={MIN_MOVE_PCT}%  vol_global>=${MIN_VOLUME_GLOBAL/1e6:.0f}M  "
             f"max_coins={MAX_COINS}")
    coins = build_universe()
    all_results = []
    for i, symbol in enumerate(coins):
        try:
            log.info(f"[{i+1}/{len(coins)}] {symbol}")
            all_results.extend(backtest_coin(symbol))
        except Exception as e:  # noqa: BLE001
            log.error(f"{symbol}: {e}")
    log.info(f"Total setups: {len(all_results)}")
    if all_results:
        csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "backtest_results.csv")
        # Campos base + grid
        base_fields = ["symbol", "ts", "direction", "dist_pct", "sl_dist_pct",
                       "gap_pct", "change_pct", "consol_range_pct",
                       "outcome", "outcome_tp1", "outcome_tfz",
                       "max_float_pct", "final_pct"]
        grid_fields = [f"grid_{sl*100:.1f}x{tp*100:.0f}"
                       for sl in SL_GRID for tp in TP_GRID]
        fields = base_fields + grid_fields
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(all_results)
        log.info(f"CSV guardado: {csv_path}")
    analysis = analyze(all_results)
    report = build_report(analysis, len(coins))
    log.info("Enviando reporte...")
    send_telegram(report)
    log.info("=== Backtest completado ===")
if __name__ == "__main__":
    main()
