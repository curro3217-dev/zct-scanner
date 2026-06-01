#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ZCT-SCANNER  ->  TFZ-SCANNER  (Trading From Zero)
=================================================
Scanner de setups intradia para perpetuos USDT en MEXC Futuros.

Reemplaza la metodologia ZCT (30SMMA, conteo de cruces, vol ratio) por la
logica de la estrategia "Trading From Zero":

    - El precio se mueve hacia la LIQUIDEZ (stops detras de niveles).
    - Buscamos 2+ NIVELES de liquidez claros y CERCANOS en la direccion
      de la tendencia (resistencias para LONG, soportes para SHORT).
    - El precio construye una CONSOLIDACION pegada al nivel mas cercano.
    - La ENTRADA es el BREAKOUT con cierre fuera del rango.
    - Descartamos graficos no-tradeables (mechas enormes, gaps, sin estructura).

Parametros de trading (fijos):
    SL 2% | TP 6% | Ratio 1:3 | Apalancamiento x10

Las entradas se buscan en grafico de 5m; los niveles se detectan en 5m + 15m.
El link de TradingView de la alerta apunta al grafico de 5m.

Variables de entorno: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID (ya configurados).
Opcionales para calibrar: DIAG, MIN_VOLUME_USD, MIN_MOVE_PCT, PIVOT_K,
LEVEL_TOL_PCT, MIN_TOUCHES, MIN_LEVELS, GAP_TOP_COIN, GAP_ALTCOIN,
MAX_DIST_TO_LEVEL, CONSOL_LOOKBACK, CONSOL_MAX_RANGE, CONSOL_TO_LEVEL,
BREAKOUT_BARS, MAX_EXTENSION, MAX_MEAN_WICK, MAX_GAP_PCT, MAX_GAPS_ALLOWED,
COOLDOWN_MIN.
"""

import os
import json
import time
import math
import datetime as dt
from urllib import request as urlrequest
from urllib import error as urlerror

# --------------------------------------------------------------------------- #
#  CONFIGURACION
#  Todos los umbrales se pueden sobreescribir con variables de entorno
#  (secrets o env vars del workflow) sin tocar el codigo. Ej: para aflojar,
#  pon LEVEL_TOL_PCT=0.008 o MAX_DIST_TO_LEVEL=0.20 en el workflow.
# --------------------------------------------------------------------------- #

def _envf(name, default):
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return float(default)


def _envi(name, default):
    try:
        return int(float(os.environ[name]))
    except (KeyError, ValueError):
        return int(default)


def _envb(name, default=False):
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "si", "on")


BASE = "https://contract.mexc.com/api/v1/contract"

# ---- Diagnostico ---------------------------------------------------------- #
# DIAG=1 imprime el embudo (cuantos candidatos mueren en cada filtro).
DIAG             = _envb("DIAG", True)   # por defecto activo mientras calibramos

# ---- Seleccion de monedas (se mantiene de lo anterior) -------------------- #
MIN_VOLUME_USD   = _envf("MIN_VOLUME_USD", 20_000_000)  # vol MEXC 24h ($20M~$100M global)
MIN_MOVE_PCT     = _envf("MIN_MOVE_PCT", 10.0)          # movimiento >= 10% en 24h O 7d
QUOTE            = "_USDT"      # solo perpetuos USDT

# ---- Parametros de trading (fijos) ---------------------------------------- #
SL_PCT           = 0.02         # 2%
TP_PCT           = 0.06         # 6%  (ratio 1:3)
LEVERAGE         = 10

# ---- Deteccion de niveles de liquidez (TFZ) ------------------------------- #
PIVOT_K          = _envi("PIVOT_K", 2)            # ventana a cada lado del swing/pivot
LEVEL_TOL_PCT    = _envf("LEVEL_TOL_PCT", 0.006)  # toques dentro de 0.6% = mismo nivel
MIN_TOUCHES      = _envi("MIN_TOUCHES", 2)        # un nivel valido necesita >= N toques
MIN_LEVELS       = _envi("MIN_LEVELS", 2)         # se exigen al menos N niveles (Anexo 5)

# Distancia maxima ENTRE los dos niveles objetivo (Anexo 10)
GAP_TOP_COIN     = _envf("GAP_TOP_COIN", 0.020)   # top coins: <= 2%
GAP_ALTCOIN      = _envf("GAP_ALTCOIN", 0.030)    # altcoins:  <= 3%
TOP_COINS = {"BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX",
             "LINK", "TRX", "DOT", "MATIC", "LTC", "BCH", "TON"}

# Distancia maxima del PRECIO al nivel mas cercano (Anexo 11)
MAX_DIST_TO_LEVEL = _envf("MAX_DIST_TO_LEVEL", 0.15)  # 15% en movimiento limpio

# ---- Consolidacion / base ------------------------------------------------- #
CONSOL_LOOKBACK  = _envi("CONSOL_LOOKBACK", 10)       # nº de velas 5m que forman la base
CONSOL_MAX_RANGE = _envf("CONSOL_MAX_RANGE", 0.030)   # rango de la consolidacion <= 3%
CONSOL_TO_LEVEL  = _envf("CONSOL_TO_LEVEL", 0.030)    # techo de la base a <= 3% del nivel
BREAKOUT_BARS    = _envi("BREAKOUT_BARS", 2)          # busca la ruptura en las ultimas N velas
MAX_EXTENSION    = _envf("MAX_EXTENSION", 0.015)      # no alertar si el precio ya se fue >1.5% del breakout

# ---- Filtro de graficos no-tradeables (Anexo 3) --------------------------- #
WICK_LOOKBACK    = 30
MAX_MEAN_WICK    = _envf("MAX_MEAN_WICK", 0.70)   # mecha media > 70% del rango -> sin estructura
MAX_GAP_PCT      = _envf("MAX_GAP_PCT", 0.025)    # gaps > 2.5% frecuentes -> baja liquidez
MAX_GAPS_ALLOWED = _envi("MAX_GAPS_ALLOWED", 3)

# ---- Anti-spam / dedup ---------------------------------------------------- #
ALERTS_LOG       = "alerts_log.json"
COOLDOWN_MIN     = _envi("COOLDOWN_MIN", 120)     # no repetir mismo symbol+side en N min

# ---- Embudo de diagnostico (se rellena en cada corrida) ------------------- #
FUNNEL = {
    "evaluados": 0,
    "datos_ok": 0,
    "tradeable": 0,
    "con_niveles": 0,
    "2_niveles_direccion": 0,
    "dist_ok": 0,
    "gap_ok": 0,
    "breakout_alerta": 0,
}


def _bump(key):
    FUNNEL[key] = FUNNEL.get(key, 0) + 1

# --------------------------------------------------------------------------- #
#  HTTP
# --------------------------------------------------------------------------- #

def _get(url, retries=3, timeout=15):
    last = None
    for i in range(retries):
        try:
            req = urlrequest.Request(url, headers={"User-Agent": "tfz-scanner/1.0"})
            with urlrequest.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except (urlerror.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            time.sleep(1.0 + i)
    print(f"[WARN] GET fallo {url}: {last}")
    return None


def get_tickers():
    """Lista bulk de todos los contratos."""
    d = _get(f"{BASE}/ticker")
    if not d or not d.get("success"):
        return []
    return d.get("data", [])


def get_klines(symbol, interval, limit=200):
    """
    Devuelve lista de velas como dicts {t,o,h,l,c,vol,amount} ordenadas
    de mas antigua a mas reciente. La API entrega arrays columnares.
    """
    d = _get(f"{BASE}/kline/{symbol}?interval={interval}&limit={limit}")
    if not d or not d.get("success"):
        return []
    k = d.get("data", {})
    t = k.get("time", [])
    if not t:
        return []
    o, h, l, c = k.get("open", []), k.get("high", []), k.get("low", []), k.get("close", [])
    vol, amt = k.get("vol", []), k.get("amount", [])
    out = []
    for i in range(len(t)):
        out.append({
            "t": t[i], "o": o[i], "h": h[i], "l": l[i], "c": c[i],
            "vol": vol[i] if i < len(vol) else 0.0,
            "amount": amt[i] if i < len(amt) else 0.0,
        })
    return out


# --------------------------------------------------------------------------- #
#  SELECCION DE MONEDAS
# --------------------------------------------------------------------------- #

def base_asset(symbol):
    return symbol.split("_")[0]


def change_24h_from_daily(symbol):
    """
    Cambio % de dia segun convencion previa: desde klines diarias
    (close[-1] - close[-2]) / close[-2] * 100.
    Evita el sesgo UTC+8 del campo riseFallRate del bulk.
    """
    d = get_klines(symbol, "Day1", limit=3)
    if len(d) < 2:
        return None
    prev, last = d[-2]["c"], d[-1]["c"]
    if not prev:
        return None
    return (last - prev) / prev * 100.0


def select_candidates(tickers):
    """
    Aplica los filtros de seleccion y devuelve [(symbol, side, info), ...].
    side = 'LONG' si sube, 'SHORT' si baja.
    Usa r7 del propio ticker para el cambio de 7d y solo llama a klines
    diarias para confirmar el cambio de 24h de los que ya pasan volumen.
    """
    out = []
    for tk in tickers:
        sym = tk.get("symbol", "")
        if not sym.endswith(QUOTE):
            continue
        if (tk.get("amount24") or 0) < MIN_VOLUME_USD:
            continue

        # 7d desde el propio ticker (riseFallRates.r7 viene en fraccion)
        rr = tk.get("riseFallRates") or {}
        ch7 = (rr.get("r7") or 0.0) * 100.0

        # 24h calculado desde klines diarias (mas fiable que riseFallRate)
        ch24 = change_24h_from_daily(sym)
        if ch24 is None:
            ch24 = (rr.get("r") or 0.0) * 100.0  # fallback

        up   = (ch24 >= MIN_MOVE_PCT) or (ch7 >= MIN_MOVE_PCT)
        down = (ch24 <= -MIN_MOVE_PCT) or (ch7 <= -MIN_MOVE_PCT)
        if not (up or down):
            continue
        if up and down:
            # señal contradictoria 24h vs 7d -> seguimos el de 24h (mas reciente)
            side = "LONG" if ch24 >= 0 else "SHORT"
        else:
            side = "LONG" if up else "SHORT"

        out.append((sym, side, {
            "last": tk.get("lastPrice"),
            "amount24": tk.get("amount24"),
            "ch24": round(ch24, 2),
            "ch7": round(ch7, 2),
        }))
    return out


# --------------------------------------------------------------------------- #
#  DETECCION DE NIVELES DE LIQUIDEZ
# --------------------------------------------------------------------------- #

def pivots(candles, k, kind):
    """
    Indices de swing highs ('high') o swing lows ('low'):
    el extremo de la vela i es el mayor/menor en la ventana [i-k, i+k].
    """
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
    """
    Agrupa precios cercanos (dentro de tol relativo) en niveles.
    Devuelve [(precio_medio, n_toques), ...] ordenado por precio.
    """
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
    """
    Niveles de liquidez validos (>= MIN_TOUCHES toques) en la direccion
    del trade. Para LONG = resistencias (swing highs); SHORT = soportes.
    """
    kind = "high" if side == "LONG" else "low"
    idx = pivots(candles, PIVOT_K, kind)
    prices = [candles[i][kind[0]] for i in idx]  # 'h' o 'l'
    levels = cluster_levels(prices, LEVEL_TOL_PCT)
    return [(p, n) for (p, n) in levels if n >= MIN_TOUCHES]


# --------------------------------------------------------------------------- #
#  FILTROS DE CALIDAD
# --------------------------------------------------------------------------- #

def is_untradeable(candles):
    """Anexo 3: mechas enormes sin estructura o demasiados gaps."""
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


def find_consolidation(candles, side, nearest_level):
    """
    Busca una consolidacion (base) pegada al nivel y un BREAKOUT.

    Robustez de timing: prueba la ruptura en las ultimas BREAKOUT_BARS velas
    (no solo la ultima), por si el cron se ejecuta una vela tarde. Para cada
    posicion de trigger candidata, la base son las CONSOL_LOOKBACK velas
    inmediatamente anteriores. Incluye un guardia anti-entrada-tardia
    (MAX_EXTENSION): si el precio actual ya se fue demasiado lejos del
    breakout, no alerta.

    Devuelve dict con la info de la base si hay breakout valido, o None.
    Se prueba de la vela mas reciente hacia atras (preferimos la mas fresca).
    """
    if len(candles) < CONSOL_LOOKBACK + 1:
        return None

    last_close = candles[-1]["c"]

    for back in range(0, max(1, BREAKOUT_BARS)):
        ti = len(candles) - 1 - back          # indice del trigger candidato
        if ti - CONSOL_LOOKBACK < 0:
            continue
        base = candles[ti - CONSOL_LOOKBACK:ti]
        trigger = candles[ti]

        highs = [c["h"] for c in base]
        lows  = [c["l"] for c in base]
        hi, lo = max(highs), min(lows)
        if lo <= 0:
            continue
        rng_pct = (hi - lo) / lo
        if rng_pct > CONSOL_MAX_RANGE:
            continue  # demasiado ancha: no es consolidacion

        # el trigger no debe ser una mecha gigante (cuerpo real)
        trng = trigger["h"] - trigger["l"]
        tbody = abs(trigger["c"] - trigger["o"])
        if trng > 0 and tbody / trng < 0.4:
            continue

        if side == "LONG":
            # la base debe estar justo por debajo del nivel
            if (nearest_level - hi) / nearest_level > CONSOL_TO_LEVEL:
                continue
            # breakout: cierre por encima del techo del rango
            if not (trigger["c"] > hi):
                continue
            # anti-entrada-tardia: el precio actual no debe haberse ido lejos
            if (last_close - hi) / hi > MAX_EXTENSION:
                continue
        else:  # SHORT
            if (lo - nearest_level) / nearest_level > CONSOL_TO_LEVEL:
                continue
            if not (trigger["c"] < lo):
                continue
            if (lo - last_close) / lo > MAX_EXTENSION:
                continue

        return {"consol_high": hi, "consol_low": lo,
                "range_pct": round(rng_pct * 100, 2),
                "trigger_close": trigger["c"],
                "trigger_back": back}

    return None


# --------------------------------------------------------------------------- #
#  EVALUACION DE UN SETUP (TFZ)
# --------------------------------------------------------------------------- #

def evaluate(symbol, side, info):
    """
    Devuelve un dict de alerta si el simbolo cumple el setup TFZ completo,
    o None. Combina niveles de 5m + 15m, exige 2 niveles cercanos, valida
    distancia al precio, consolidacion y breakout en 5m.
    """
    _bump("evaluados")
    price = info.get("last")
    if not price:
        return None

    k5 = get_klines(symbol, "Min15", limit=200)   # 15m para niveles fuertes
    k1 = get_klines(symbol, "Min5", limit=200)    # 5m para base + trigger
    if len(k1) < CONSOL_LOOKBACK + 5 or len(k5) < 20:
        return None
    _bump("datos_ok")

    if is_untradeable(k1):
        return None
    _bump("tradeable")

    # Niveles combinados 5m + 15m
    levels = liquidity_levels(k1, side) + liquidity_levels(k5, side)
    if not levels:
        return None
    _bump("con_niveles")

    # Niveles en la direccion correcta respecto al precio
    if side == "LONG":
        target = sorted([(p, n) for (p, n) in levels if p > price], key=lambda x: x[0])
    else:
        target = sorted([(p, n) for (p, n) in levels if p < price],
                        key=lambda x: x[0], reverse=True)
    if len(target) < MIN_LEVELS:
        return None
    _bump("2_niveles_direccion")

    l1, l2 = target[0][0], target[1][0]   # dos niveles mas cercanos
    nearest = l1

    # Distancia al nivel mas cercano (Anexo 11)
    dist = abs(nearest - price) / price
    if dist > MAX_DIST_TO_LEVEL:
        return None
    _bump("dist_ok")

    # Distancia entre los dos niveles (Anexo 10)
    gap = abs(l2 - l1) / l1
    max_gap = GAP_TOP_COIN if base_asset(symbol) in TOP_COINS else GAP_ALTCOIN
    if gap > max_gap:
        return None
    _bump("gap_ok")

    # Consolidacion + breakout en 5m
    consol = find_consolidation(k1, side, nearest)
    if not consol:
        return None
    _bump("breakout_alerta")

    # --- Construccion de la alerta ----------------------------------------- #
    entry = float(price)
    if side == "LONG":
        sl = round(entry * (1 - SL_PCT), 10)
        tp = round(entry * (1 + TP_PCT), 10)
    else:
        sl = round(entry * (1 + SL_PCT), 10)
        tp = round(entry * (1 - TP_PCT), 10)

    now = dt.datetime.now(dt.timezone.utc)
    tv_sym = base_asset(symbol) + "USDT.P"
    vol_millions = round((info.get("amount24") or 0) / 1_000_000, 0)
    # Nombres de campo EXACTOS que espera checker.py:
    #   direction, entry_price, timestamp, status='OPEN',
    #   lvl1_name, change_pct, vol_ratio
    return {
        "id": f"{symbol}_{int(now.timestamp())}",
        "symbol": symbol,
        "direction": side,                 # checker.py -> alert['direction']
        "entry_price": entry,              # checker.py -> alert['entry_price']
        "sl": sl,
        "tp": tp,
        "leverage": LEVERAGE,
        "timeframe": "5m",
        "setup": "TFZ_breakout",
        "levels": [round(l1, 10), round(l2, 10)],
        "level_gap_pct": round(gap * 100, 2),
        "dist_to_level_pct": round(dist * 100, 2),
        "consol_range_pct": consol["range_pct"],
        "ch24": info.get("ch24"),
        "ch7": info.get("ch7"),
        # --- campos heredados que checker.py/generate_stats aun usa ----------
        "lvl1_name": "TFZ_breakout",       # agrupacion "por nivel" en el resumen
        "change_pct": info.get("ch24"),    # % de cambio mostrado en el resumen
        "vol_ratio": vol_millions,         # reutilizado: volumen MEXC en millones
        # --------------------------------------------------------------------
        "tv_link": f"https://www.tradingview.com/chart/?symbol=MEXC%3A{tv_sym}&interval=5",
        "timestamp": now.isoformat(),      # checker.py -> alert['timestamp']
        "created_ts": int(now.timestamp()),  # solo para dedup interno del scanner
        "status": "OPEN",                  # checker.py filtra status == 'OPEN'
    }


# --------------------------------------------------------------------------- #
#  LOG / DEDUP / TELEGRAM
# --------------------------------------------------------------------------- #

def load_log():
    if not os.path.exists(ALERTS_LOG):
        return []
    try:
        with open(ALERTS_LOG, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def save_log(log):
    with open(ALERTS_LOG, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


def recently_alerted(log, symbol, side):
    cutoff = time.time() - COOLDOWN_MIN * 60
    for a in reversed(log):
        if a.get("symbol") == symbol and a.get("direction") == side:
            if (a.get("created_ts") or 0) >= cutoff:
                return True
    return False


def send_telegram(text):
    # Mismo nombre de secret que checker.py: TELEGRAM_TOKEN / TELEGRAM_CHAT_ID
    token = os.environ.get("TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat  = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[WARN] Faltan credenciales de Telegram; no se envia.")
        print(text)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": True,
    }).encode()
    req = urlrequest.Request(url, data=payload,
                             headers={"Content-Type": "application/json"})
    try:
        urlrequest.urlopen(req, timeout=15)
    except urlerror.URLError as e:
        print(f"[WARN] Telegram fallo: {e}")


def format_alert(a):
    arrow = "🟢 LONG" if a["direction"] == "LONG" else "🔴 SHORT"
    levels = " / ".join(f"{x:g}" for x in a["levels"])
    tp1 = a["levels"][0]                       # primer nivel = cierre de la 1a mitad
    return (
        f"<b>{arrow}  {a['symbol']}</b>  (TFZ breakout)\n"
        f"Entrada: <b>{a['entry_price']:g}</b>\n"
        f"SL: {a['sl']:g}  (2%)   TP: {a['tp']:g}  (6%)   x{a['leverage']}\n"
        f"Niveles objetivo: {levels}  (gap {a['level_gap_pct']}%)\n"
        f"Dist. al nivel: {a['dist_to_level_pct']}%   Base: {a['consol_range_pct']}%\n"
        f"Cambio 24h: {a['ch24']}%   7d: {a['ch7']}%\n"
        f"\n— — Plan Omni (copiar) — —\n"
        f"Entrada: {a['entry_price']:g}\n"
        f"SL (-2%): {a['sl']:g}\n"
        f"TP1 (50%) nivel: {tp1:g}\n"
        f"TP2 (50%) +6%: {a['tp']:g}\n"
        f"<a href=\"{a['tv_link']}\">📈 Grafico 5m (TradingView)</a>"
    )


# --------------------------------------------------------------------------- #
#  MAIN
# --------------------------------------------------------------------------- #

def main():
    print(f"[{dt.datetime.utcnow().isoformat()}] TFZ-scanner inicio")
    tickers = get_tickers()
    if not tickers:
        print("[ERROR] No se pudieron obtener tickers.")
        return

    candidates = select_candidates(tickers)
    print(f"Candidatos tras seleccion: {len(candidates)}")

    log = load_log()
    new_alerts = 0

    for sym, side, info in candidates:
        if recently_alerted(log, sym, side):
            continue
        try:
            alert = evaluate(sym, side, info)
        except Exception as e:  # noqa: BLE001  (robustez en produccion)
            print(f"[WARN] {sym} error: {e}")
            alert = None
        if not alert:
            continue
        send_telegram(format_alert(alert))
        log.append(alert)
        new_alerts += 1
        print(f"  ALERTA  {side}  {sym}  @ {alert['entry_price']}")
        time.sleep(0.3)  # cortesia con la API

    save_log(log)

    if DIAG:
        print("---- Embudo de diagnostico ----")
        print(f"  candidatos............... {len(candidates)}")
        print(f"  evaluados................ {FUNNEL['evaluados']}")
        print(f"  con datos OHLC ok........ {FUNNEL['datos_ok']}")
        print(f"  tradeables (no Anexo3)... {FUNNEL['tradeable']}")
        print(f"  con >=1 nivel............ {FUNNEL['con_niveles']}")
        print(f"  con 2 niveles direccion.. {FUNNEL['2_niveles_direccion']}")
        print(f"  nivel a <{MAX_DIST_TO_LEVEL*100:g}% del precio. {FUNNEL['dist_ok']}")
        print(f"  niveles cercanos (gap)... {FUNNEL['gap_ok']}")
        print(f"  breakout -> ALERTA....... {FUNNEL['breakout_alerta']}")
        print("-------------------------------")

    print(f"Alertas nuevas: {new_alerts}")


if __name__ == "__main__":
    main()
