#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ZCT-SCANNER  ->  TFZ-SCANNER  (Trading From Zero)
=================================================
Scanner de setups intradia para perpetuos USDT en MEXC Futuros.

El VOLUMEN se mide global (CoinGecko), NUNCA MEXC. Seleccion intraday:
movimiento >= 10% en 24h (el 7d ya no filtra). Estructura desde MEXC.

Variables de entorno: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID (ya configurados).
Opcional: COINGECKO_API_KEY (Demo, gratis).
"""

import os
import json
import time
import math
import datetime as dt
from urllib import request as urlrequest
from urllib import error as urlerror

# --------------------------------------------------------------------------- #
#  CONFIGURACION (todo sobreescribible por variable de entorno)
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
DIAG             = _envb("DIAG", True)   # imprime el embudo en cada corrida

# ---- Seleccion de monedas ------------------------------------------------- #
# Volumen SIEMPRE global (CoinGecko), NUNCA MEXC. Si CoinGecko falla, 0 candidatos.
MIN_VOLUME_GLOBAL = _envf("MIN_VOLUME_GLOBAL", 100_000_000)  # $100M global 24h
MIN_MOVE_PCT      = _envf("MIN_MOVE_PCT", 10.0)            # movimiento >= 10% en 24h (intraday)
QUOTE             = "_USDT"     # solo perpetuos USDT
COINGECKO_PAGES   = _envi("COINGECKO_PAGES", 1)           # 1 pagina = top 250 por volumen
COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY", "")  # Demo (opcional)

# ---- Parametros de trading (fijos) ---------------------------------------- #
SL_PCT           = 0.02         # 2%
TP_PCT           = 0.06         # 6%  (ratio 1:3)
LEVERAGE         = 10

# ---- Deteccion de niveles de liquidez (TFZ) ------------------------------- #
PIVOT_K          = _envi("PIVOT_K", 2)
LEVEL_TOL_PCT    = _envf("LEVEL_TOL_PCT", 0.006)
MIN_TOUCHES      = _envi("MIN_TOUCHES", 2)
MIN_LEVELS       = _envi("MIN_LEVELS", 2)

GAP_TOP_COIN     = _envf("GAP_TOP_COIN", 0.020)   # top coins: <= 2%
GAP_ALTCOIN      = _envf("GAP_ALTCOIN", 0.030)    # altcoins:  <= 3%
TOP_COINS = {"BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX",
             "LINK", "TRX", "DOT", "MATIC", "LTC", "BCH", "TON"}

MAX_DIST_TO_LEVEL = _envf("MAX_DIST_TO_LEVEL", 0.15)  # 15%

# ---- Consolidacion / base ------------------------------------------------- #
CONSOL_LOOKBACK  = _envi("CONSOL_LOOKBACK", 10)
CONSOL_MAX_RANGE = _envf("CONSOL_MAX_RANGE", 0.030)
CONSOL_TO_LEVEL  = _envf("CONSOL_TO_LEVEL", 0.030)
BREAKOUT_BARS    = _envi("BREAKOUT_BARS", 2)
MAX_EXTENSION    = _envf("MAX_EXTENSION", 0.015)

# ---- Filtro de graficos no-tradeables (Anexo 3) --------------------------- #
WICK_LOOKBACK    = 30
MAX_MEAN_WICK    = _envf("MAX_MEAN_WICK", 0.70)
MAX_GAP_PCT      = _envf("MAX_GAP_PCT", 0.025)
MAX_GAPS_ALLOWED = _envi("MAX_GAPS_ALLOWED", 3)

# ---- Anti-spam / dedup ---------------------------------------------------- #
ALERTS_LOG       = "alerts_log.json"
COOLDOWN_MIN     = _envi("COOLDOWN_MIN", 120)

# ---- Embudo de diagnostico ------------------------------------------------ #
FUNNEL = {
    "evaluados": 0, "datos_ok": 0, "tradeable": 0, "con_niveles": 0,
    "2_niveles_direccion": 0, "dist_ok": 0, "gap_ok": 0, "breakout_alerta": 0,
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


def _get_raw(url, retries=3, timeout=20):
    """GET que devuelve el JSON tal cual (lista o dict), sin formato MEXC."""
    last = None
    for i in range(retries):
        try:
            req = urlrequest.Request(url, headers={"User-Agent": "tfz-scanner/1.0"})
            with urlrequest.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except (urlerror.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            time.sleep(1.5 + i)
    print(f"[WARN] GET raw fallo {url}: {last}")
    return None


def get_tickers():
    """Lista bulk de todos los contratos MEXC (para saber que perps existen)."""
    d = _get(f"{BASE}/ticker")
    if not d or not d.get("success"):
        return []
    return d.get("data", [])


def get_global_market():
    """
    Mapa {SYMBOL: {'vol': vol24_usd, 'ch24': %, 'ch7': %}} desde CoinGecko,
    top por volumen. Para cada symbol nos quedamos con la moneda de MAYOR
    volumen. Devuelve {} si falla -> ese ciclo no saca candidatos.
    """
    key_param = f"&x_cg_demo_api_key={COINGECKO_API_KEY}" if COINGECKO_API_KEY else ""
    out = {}
    for page in range(1, COINGECKO_PAGES + 1):
        url = ("https://api.coingecko.com/api/v3/coins/markets"
               "?vs_currency=usd&order=volume_desc&per_page=250"
               f"&page={page}&price_change_percentage=24h,7d{key_param}")
        data = _get_raw(url)
        if not isinstance(data, list) or not data:
            break
        for c in data:
            sym = (c.get("symbol") or "").upper()
            if not sym or sym in out:          # primero = mayor volumen
                continue
            out[sym] = {
                "vol": c.get("total_volume") or 0.0,
                "ch24": c.get("price_change_percentage_24h"),
                "ch7": c.get("price_change_percentage_7d_in_currency"),
            }
        time.sleep(1.5)
    return out


def get_klines(symbol, interval, limit=200):
    """Velas como dicts {t,o,h,l,c,vol,amount}, de mas antigua a mas reciente."""
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


def select_candidates(tickers, gmarket):
    """
    Filtra perpetuos USDT de MEXC con VOLUMEN GLOBAL (CoinGecko) y movimiento
    24h (intraday). Si gmarket esta vacio devuelve [] (no usamos volumen MEXC).
    """
    if not gmarket:
        return []

    out = []
    for tk in tickers:
        sym = tk.get("symbol", "")
        if not sym.endswith(QUOTE):
            continue

        g = gmarket.get(base_asset(sym).upper())
        if not g:
            continue   # no esta entre las monedas de mayor volumen global

        # --- VOLUMEN: global (CoinGecko), nunca MEXC ----------------------- #
        if (g.get("vol") or 0) < MIN_VOLUME_GLOBAL:
            continue

        # --- CAMBIO 24h (intraday): SOLO el 24h decide la direccion -------- #
        ch24 = g.get("ch24")
        if ch24 is None:
            continue
        ch7 = g.get("ch7")
        ch7 = ch7 if ch7 is not None else 0.0   # solo informativo en la alerta, NO filtra

        if ch24 >= MIN_MOVE_PCT:
            side = "LONG"
        elif ch24 <= -MIN_MOVE_PCT:
            side = "SHORT"
        else:
            continue

        out.append((sym, side, {
            "last": tk.get("lastPrice"),
            "vol_global": g.get("vol") or 0.0,
            "ch24": round(ch24, 2),
            "ch7": round(ch7, 2),
        }))
    return out


# --------------------------------------------------------------------------- #
#  DETECCION DE NIVELES DE LIQUIDEZ
# --------------------------------------------------------------------------- #

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
    Consolidacion (base) pegada al nivel + BREAKOUT. Prueba la ruptura en las
    ultimas BREAKOUT_BARS velas (robustez de timing) y descarta entradas
    tardias (MAX_EXTENSION). Devuelve dict o None.
    """
    if len(candles) < CONSOL_LOOKBACK + 1:
        return None

    last_close = candles[-1]["c"]

    for back in range(0, max(1, BREAKOUT_BARS)):
        ti = len(candles) - 1 - back
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
            continue

        trng = trigger["h"] - trigger["l"]
        tbody = abs(trigger["c"] - trigger["o"])
        if trng > 0 and tbody / trng < 0.4:
            continue

        if side == "LONG":
            if (nearest_level - hi) / nearest_level > CONSOL_TO_LEVEL:
                continue
            if not (trigger["c"] > hi):
                continue
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

    levels = liquidity_levels(k1, side) + liquidity_levels(k5, side)
    if not levels:
        return None
    _bump("con_niveles")

    if side == "LONG":
        target = sorted([(p, n) for (p, n) in levels if p > price], key=lambda x: x[0])
    else:
        target = sorted([(p, n) for (p, n) in levels if p < price],
                        key=lambda x: x[0], reverse=True)
    if len(target) < MIN_LEVELS:
        return None
    _bump("2_niveles_direccion")

    l1, l2 = target[0][0], target[1][0]
    nearest = l1

    dist = abs(nearest - price) / price
    if dist > MAX_DIST_TO_LEVEL:
        return None
    _bump("dist_ok")

    gap = abs(l2 - l1) / l1
    max_gap = GAP_TOP_COIN if base_asset(symbol) in TOP_COINS else GAP_ALTCOIN
    if gap > max_gap:
        return None
    _bump("gap_ok")

    consol = find_consolidation(k1, side, nearest)
    if not consol:
        return None
    _bump("breakout_alerta")

    entry = float(price)
    if side == "LONG":
        sl = round(entry * (1 - SL_PCT), 10)
        tp = round(entry * (1 + TP_PCT), 10)
    else:
        sl = round(entry * (1 + SL_PCT), 10)
        tp = round(entry * (1 - TP_PCT), 10)

    now = dt.datetime.now(dt.timezone.utc)
    tv_sym = base_asset(symbol) + "USDT.P"
    vol_millions = round((info.get("vol_global") or 0) / 1_000_000, 0)
    return {
        "id": f"{symbol}_{int(now.timestamp())}",
        "symbol": symbol,
        "direction": side,
        "entry_price": entry,
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
        "lvl1_name": "TFZ_breakout",
        "change_pct": info.get("ch24"),
        "vol_ratio": vol_millions,
        "tv_link": f"https://www.tradingview.com/chart/?symbol=MEXC%3A{tv_sym}&interval=5",
        "timestamp": now.isoformat(),
        "created_ts": int(now.timestamp()),
        "status": "OPEN",
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
    tp1 = a["levels"][0]
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

    gmarket = get_global_market()
    if not gmarket:
        print("[WARN] CoinGecko no disponible este ciclo; sin candidatos (no usamos volumen MEXC).")
    else:
        print(f"CoinGecko: {len(gmarket)} monedas con volumen global cargadas.")

    candidates = select_candidates(tickers, gmarket)
    print(f"Candidatos tras seleccion: {len(candidates)}")

    log = load_log()
    new_alerts = 0

    for sym, side, info in candidates:
        if recently_alerted(log, sym, side):
            continue
        try:
            alert = evaluate(sym, side, info)
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] {sym} error: {e}")
            alert = None
        if not alert:
            continue
        send_telegram(format_alert(alert))
        log.append(alert)
        new_alerts += 1
        print(f"  ALERTA  {side}  {sym}  @ {alert['entry_price']}")
        time.sleep(0.3)

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
