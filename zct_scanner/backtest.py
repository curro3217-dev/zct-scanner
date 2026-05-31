#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TFZ Backtester — Trading From Zero (sustituye al backtester ZCT v8)
===================================================================
Replica la logica de entrada del scanner (main.py) pero recorriendo
historico, para estimar el win-rate de la estrategia.

Logica testeada (identica a main.py):
  - Seleccion: movimiento >= 10% en 24h Y volumen >= $20M (reconstruidos
    del propio historico de 15m). LONG si sube, SHORT si baja.
  - 2+ niveles de liquidez claros y cercanos (gap <= 2%/3%) en la direccion.
  - Nivel mas cercano a < 15% del precio.
  - Consolidacion (base estrecha) pegada al nivel + BREAKOUT de la vela.
  - Descarte de graficos no-tradeables.
  - SL 2% | TP 6% (RR 1:3). Breakeven con > 25% de aciertos.
  - Ventana de resultado: 8h (32 velas de 15m), igual que checker.py.

DIVERGENCIA vs produccion: el backtest corre sobre 15m (no 5m) para tener
~15 dias de historico con el limite de klines de MEXC. La estructura de la
estrategia es la misma; solo cambia el timeframe de las velas.

Uso: python backtest.py
Requiere: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID (env vars)
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
MIN_VOLUME_USD   = 20_000_000   # volumen 24h reconstruido (suma de 96 velas)
MIN_MOVE_PCT     = 10.0         # movimiento minimo 24h
SL_PCT           = 0.02         # 2%
TP_PCT           = 0.06         # 6%  (RR 1:3)

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
COOLDOWN_CANDLES = 8            # 2h entre setups del mismo symbol+side (= COOLDOWN_MIN/15)

BREAKEVEN_WR     = 25.0         # con RR 1:3 necesitas > 25% de aciertos

INTERVAL_MAP = {"15m": "Min15", "1h": "Min60", "4h": "Hour4", "1d": "Day1"}

# Universo de monedas para el backtest (muestra; el scanner en vivo escanea
# todos los perpetuos, aqui usamos una lista fija liquida y volatil).
COINS = [
    "BTC_USDT", "ETH_USDT", "BNB_USDT", "SOL_USDT", "XRP_USDT",
    "DOGE_USDT", "ADA_USDT", "AVAX_USDT", "LINK_USDT", "DOT_USDT",
    "LTC_USDT", "UNI_USDT", "ATOM_USDT", "NEAR_USDT", "APT_USDT",
    "ARB_USDT", "OP_USDT", "INJ_USDT", "SUI_USDT", "TRX_USDT",
    "TON_USDT", "WIF_USDT", "JUP_USDT", "SEI_USDT", "PEPE_USDT",
    "BONK_USDT", "TIA_USDT", "PENDLE_USDT", "FTM_USDT", "MATIC_USDT",
]

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


# ══════════════════════════════════════════════════════════
#  API MEXC
# ══════════════════════════════════════════════════════════
def get_klines(symbol, interval, limit=1500):
    try:
        r = requests.get(
            f"https://contract.mexc.com/api/v1/contract/kline/{symbol}",
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


# ══════════════════════════════════════════════════════════
#  DETECCION DE NIVELES (identica a main.py, sobre dicts de velas)
# ══════════════════════════════════════════════════════════
def base_asset(symbol):
    return symbol.split("_")[0]


def _window(arrs, lo, hi):
    """Construye lista de velas dict desde arrays paralelos, indices [lo, hi]."""
    o, h, l, c = arrs["open"], arrs["high"], arrs["low"], arrs["close"]
    out = []
    for j in range(lo, hi + 1):
        out.append({"o": o[j], "h": h[j], "l": l[j], "c": c[j]})
    return out


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
    """
    Igual que find_consolidation de main.py pero el trigger es SIEMPRE la
    ultima vela de 'candles' (en el backtest evaluamos cada vela como trigger,
    no necesitamos mirar atras). Devuelve dict o None.
    """
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
def simulate_outcome(direction, entry, future_highs, future_lows):
    if direction == "LONG":
        tp = entry * (1 + TP_PCT)
        sl = entry * (1 - SL_PCT)
    else:
        tp = entry * (1 - TP_PCT)
        sl = entry * (1 + SL_PCT)
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
    amounts = d15.get("amount", d15.get("vol", [0.0] * len(closes)))
    n = len(closes)
    max_gap = GAP_TOP_COIN if base_asset(symbol) in TOP_COINS else GAP_ALTCOIN

    last_signal = {}  # (side) -> idx, para cooldown
    start = max(LEVEL_WINDOW, CHANGE_LB)
    end = n - OUTCOME_CANDLES - 1

    for idx in range(start, end):
        price = closes[idx]
        if price <= 0:
            continue

        # --- filtro de volumen 24h (suma de las ultimas 96 velas) ---------- #
        vol24 = sum(amounts[idx - CHANGE_LB + 1: idx + 1])
        if vol24 < MIN_VOLUME_USD:
            continue

        # --- filtro de movimiento 24h y direccion -------------------------- #
        ref = closes[idx - CHANGE_LB]
        if ref <= 0:
            continue
        ch24 = (price - ref) / ref * 100.0
        if ch24 >= MIN_MOVE_PCT:
            side = "LONG"
        elif ch24 <= -MIN_MOVE_PCT:
            side = "SHORT"
        else:
            continue

        # --- cooldown ------------------------------------------------------ #
        if side in last_signal and (idx - last_signal[side]) < COOLDOWN_CANDLES:
            continue

        # --- ventana de niveles -------------------------------------------- #
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

        # --- resultado ----------------------------------------------------- #
        fh = highs[idx + 1: idx + 1 + OUTCOME_CANDLES]
        fl = lows[idx + 1: idx + 1 + OUTCOME_CANDLES]
        if len(fh) < 4:
            continue
        outcome = simulate_outcome(side, price, fh, fl)
        last_signal[side] = idx

        ts = (datetime.utcfromtimestamp(d15["time"][idx]).strftime("%m-%d %H:%M")
              if "time" in d15 and idx < len(d15["time"]) else "?")
        results.append({
            "symbol": symbol,
            "ts": ts,
            "direction": side,
            "dist_pct": round(dist * 100, 2),
            "gap_pct": round(gap * 100, 2),
            "change_pct": round(ch24, 1),
            "consol_range_pct": consol["range_pct"],
            "outcome": outcome,
        })

    log.info(f"{symbol}: {len(results)} setups")
    return results


# ══════════════════════════════════════════════════════════
#  ANALISIS
# ══════════════════════════════════════════════════════════
def _wr(sub):
    resolved = [r for r in sub if r["outcome"] in ("WIN", "LOSS")]
    if not resolved:
        return (0, 0, 0.0)
    w = sum(1 for r in resolved if r["outcome"] == "WIN")
    return (w, len(resolved), w / len(resolved) * 100)


def analyze(rows):
    total = len(rows)
    wins = sum(1 for r in rows if r["outcome"] == "WIN")
    losses = sum(1 for r in rows if r["outcome"] == "LOSS")
    timeouts = sum(1 for r in rows if r["outcome"] == "TIMEOUT")
    resolved = wins + losses
    wr = wins / resolved * 100 if resolved else 0.0

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

    by_change = {}
    for lo, hi, lbl in [(10, 15, "10-15%"), (15, 25, "15-25%"), (25, 1e9, "25%+")]:
        sub = [r for r in rows if lo <= abs(r["change_pct"]) < hi]
        if sub:
            by_change[lbl] = _wr(sub)

    return {
        "total": total, "wins": wins, "losses": losses, "timeouts": timeouts,
        "resolved": resolved, "wr": wr,
        "by_dir": by_dir, "by_gap": by_gap, "by_dist": by_dist, "by_change": by_change,
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
        conclusion = ""
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

        body = "\n".join(x for x in [
            (f"Aciertos: {wr:.0f}% — {a['wins']} ganadas / {a['losses']} perdidas / "
             f"{a['resolved']} resueltas"),
            f"Timeouts (no tocaron TP ni SL en 8h): {a['timeouts']}  ·  Total setups: {a['total']}",
            conclusion,
            "",
            _fmt_section("Por direccion:", a["by_dir"]),
            "",
            _fmt_section("Por cercania entre niveles (gap):", a["by_gap"]),
            "",
            _fmt_section("Por distancia del precio al nivel:", a["by_dist"]),
            "",
            _fmt_section("Por fuerza del movimiento 24h:", a["by_change"]),
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
             f"move>={MIN_MOVE_PCT}%  vol>=${MIN_VOLUME_USD/1e6:.0f}M")

    all_results = []
    for i, symbol in enumerate(COINS):
        try:
            log.info(f"[{i+1}/{len(COINS)}] {symbol}")
            all_results.extend(backtest_coin(symbol))
        except Exception as e:  # noqa: BLE001
            log.error(f"{symbol}: {e}")

    log.info(f"Total setups: {len(all_results)}")

    if all_results:
        csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "backtest_results.csv")
        fields = ["symbol", "ts", "direction", "dist_pct", "gap_pct",
                  "change_pct", "consol_range_pct", "outcome"]
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(all_results)
        log.info(f"CSV guardado: {csv_path}")

    analysis = analyze(all_results)
    report = build_report(analysis, len(COINS))
    log.info("Enviando reporte...")
    send_telegram(report)
    log.info("=== Backtest completado ===")


if __name__ == "__main__":
    main()
