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

MAX_COINS = int(float(os.environ.get("MAX_COINS", 80)))

FALLBACK_COINS = [
    "BTC_USDT", "ETH_USDT", "BNB_USDT", "SOL_USDT", "XRP_USDT",
    "DOGE_USDT", "ADA_USDT", "AVAX_USDT", "LINK_USDT", "DOT_USDT",
]

TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "")
COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY", "")  # Demo (opcional)


# ══════════════════════════════════════════════════════════
#  API
# ══════════════════════════════════════════════════════════
def get_klines(symbol, interval, limit=1500):
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
    como perpetuo USDT en MEXC. Mismo criterio que el scanner. CoinGecko da el
    volumen de HOY. Cae a FALLBACK_COINS si CoinGecko falla.
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
                if (c.get("total_volume") or 0) >= MIN_VOLUME_GLOBAL:
                    coins.append(f"{s}_USDT")
        if coins:
            log.info(f"Universo global: {len(coins)} monedas "
                     f"(vol >= ${MIN_VOLUME_GLOBAL/1e6:.0f}M, CoinGecko)")
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
#  RESULTADO DEL TRADE
# ══════════════════════════════════════════════════════════
def simulate(direction, entry, tp, sl, future_highs, future_lows):
    """WIN si toca tp, LOSS si toca sl (tp primero si ambos), TIMEOUT si no."""
    for high, low in zip(future_highs, future_lows):
        if direction == "LONG":
            if high >= tp:
                return "WIN"
            if low <= sl:
                return "LOSS"
        else:
            if low <= tp:
                return "WIN"
            if high >= sl:
                return "LOSS"
    return "TIMEOUT"


# ══════════════════════════════════════════════════════════
#  BACKTEST POR MONEDA
# ══════════════════════════════════════════════════════════
def backtest_coin(symbol):
    results = []
    d15 = get_klines(symbol, "15m", limit=1500)
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

        # --- movimiento 24h (intraday): SOLO el 24h decide la direccion ---- #
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

        # --- resultado: TRES salidas ------------------------------------- #
        fh = highs[idx + 1: idx + 1 + OUTCOME_CANDLES]
        fl = lows[idx + 1: idx + 1 + OUTCOME_CANDLES]
        if len(fh) < 4:
            continue
        if side == "LONG":
            sl_fix = price * (1 - SL_PCT)         # SL fijo 2%
            tp2 = price * (1 + TP_PCT)            # +6%
            sl_tfz = consol["consol_low"]         # SL TFZ: debajo de la consolidacion
        else:
            sl_fix = price * (1 + SL_PCT)
            tp2 = price * (1 - TP_PCT)
            sl_tfz = consol["consol_high"]
        tp1 = nearest                             # primer nivel (TP TFZ real)
        sl_dist = abs(price - sl_tfz) / price * 100.0

        outcome = simulate(side, price, tp2, sl_fix, fh, fl)         # SL2% + TP+6%
        outcome_tp1 = simulate(side, price, tp1, sl_fix, fh, fl)     # SL2% + TP nivel
        outcome_tfz = simulate(side, price, tp1, sl_tfz, fh, fl)     # SLconsol + TP nivel
        last_signal[side] = idx

        ts = (datetime.utcfromtimestamp(d15["time"][idx]).strftime("%m-%d %H:%M")
              if "time" in d15 and idx < len(d15["time"]) else "?")
        results.append({
            "symbol": symbol,
            "ts": ts,
            "direction": side,
            "dist_pct": round(dist * 100, 2),   # ganancia si TP1 WIN
            "sl_dist_pct": round(sl_dist, 2),   # perdida si TFZ LOSS
            "gap_pct": round(gap * 100, 2),
            "change_pct": round(ch24, 1),
            "consol_range_pct": consol["range_pct"],
            "outcome": outcome,
            "outcome_tp1": outcome_tp1,
            "outcome_tfz": outcome_tfz,
        })

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
    """% medio por trade. WIN=+ganancia, LOSS=-perdida, TIMEOUT=0. Sobre TODOS.
    win_gain_pct / loss_pct: numero o funcion(r). loss_pct None -> SL fijo."""
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


def analyze(rows):
    total = len(rows)
    wins = sum(1 for r in rows if r["outcome"] == "WIN")
    losses = sum(1 for r in rows if r["outcome"] == "LOSS")
    timeouts = sum(1 for r in rows if r["outcome"] == "TIMEOUT")
    resolved = wins + losses
    wr = wins / resolved * 100 if resolved else 0.0

    # --- Comparacion de 3 salidas ------------------------------------------ #
    wr_tp2_all = _wr(rows, "outcome")
    wr_tp1_all = _wr(rows, "outcome_tp1")
    wr_tfz_all = _wr(rows, "outcome_tfz")
    exp_tp2 = _expectancy(rows, "outcome", TP_PCT * 100)                       # +6% / -2%
    exp_tp1 = _expectancy(rows, "outcome_tp1", lambda r: r["dist_pct"])        # +dist / -2%
    exp_tfz = _expectancy(rows, "outcome_tfz",
                          lambda r: r["dist_pct"], lambda r: r["sl_dist_pct"])  # +dist / -SLconsol
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

    return {
        "total": total, "wins": wins, "losses": losses, "timeouts": timeouts,
        "resolved": resolved, "wr": wr,
        "by_dir": by_dir, "by_gap": by_gap, "by_dist": by_dist,
        "wr_tp2_all": wr_tp2_all, "wr_tp1_all": wr_tp1_all, "wr_tfz_all": wr_tfz_all,
        "exp_tp2": exp_tp2, "exp_tp1": exp_tp1, "exp_tfz": exp_tfz,
        "by_dir_tp1": by_dir_tp1, "by_dir_tfz": by_dir_tfz,
    }


# ══════════════════════════════════════════════════════════
#  REPORTE TELEGRAM
# ══════════════════════════════════════════════════════════
def _fmt_section(title, d):
    if not d:
        return ""
    lines = [title]
    for lbl, (w, nn, wr) in sorted(d.items(), key=lambda x: -x[1][2]):
        lines.append(f"  {lbl}: {wr:.0f}% de aciertos ({w} ganadas / {nn} resueltas)")
    return "\n".join(lines)


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
            "=== Comparacion de 3 salidas (lo importante) ===",
            (f"1) SL 2% + TP +6% (actual): WR {p2:.0f}% ({w2}/{n2})  ·  "
             f"exp {a['exp_tp2']:+.2f}%/trade"),
            (f"2) SL 2% + TP 1er nivel: WR {p1:.0f}% ({w1}/{n1})  ·  "
             f"exp {a['exp_tp1']:+.2f}%/trade"),
            (f"3) SL bajo consolidacion + TP 1er nivel (TFZ puro): WR {pz:.0f}% "
             f"({wz}/{nz})  ·  exp {a['exp_tfz']:+.2f}%/trade"),
            ("Expectancia = % medio por trade. La (3) es la salida real de TFZ; "
             "si su expectancia es positiva, el sistema gana."),
        ])

        body = "\n".join(x for x in [
            (f"Aciertos (+6%): {wr:.0f}% — {a['wins']} ganadas / {a['losses']} perdidas / "
             f"{a['resolved']} resueltas"),
            f"Timeouts (no tocaron TP ni SL en 8h): {a['timeouts']}  ·  Total setups: {a['total']}",
            conclusion,
            "",
            comparacion,
            "",
            _fmt_section("Por direccion (salida 1er nivel):", a["by_dir_tp1"]),
            _fmt_section("Por direccion (salida TFZ pura):", a["by_dir_tfz"]),
            "",
            _fmt_section("Por cercania entre niveles (gap):", a["by_gap"]),
            "",
            _fmt_section("Por distancia del precio al nivel:", a["by_dist"]),
        ] if x is not None)

    parts = [
        "📊 TFZ Backtest — Trading From Zero",
        f"Analizadas {n_coins} monedas en ~15 dias (velas de 15m)",
        "Stop Loss 2% · Take Profit 6% · RR 1:3",
        "Para tener edge necesitas acertar mas del 25% de las veces",
        "",
        body,
        "",
        ts,
    ]
    return "\n".join(parts)


def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(msg)
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg,
                  "disable_web_page_preview": "true"},
            timeout=10,
        )
        if not r.ok:
            log.error(f"Telegram: {r.text}")
    except Exception as e:  # noqa: BLE001
        log.error(f"Telegram: {e}")
        print(msg)


# ══════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════
def main():
    log.info("=== TFZ Backtester iniciando ===")
    log.info(f"SL={SL_PCT*100:.0f}%  TP={TP_PCT*100:.0f}%  RR=1:3  "
             f"move24h>={MIN_MOVE_PCT}%  vol_global>=${MIN_VOLUME_GLOBAL/1e6:.0f}M")

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
        fields = ["symbol", "ts", "direction", "dist_pct", "sl_dist_pct", "gap_pct",
                  "change_pct", "consol_range_pct", "outcome", "outcome_tp1", "outcome_tfz"]
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
