#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TFZ-SCANNER v2 — scanner intradia perpetuos USDT. Exchanges: MEXC + Bybit.
Volumen/movimiento 24h: BINANCE (exclusivamente). Klines: exchange nativo del perpetuo.
Las monedas no listadas en Binance spot quedan descartadas.

v2 — Cambios respecto a v1 (automatiza el curso completo Trading From Zero):
  * Volumen y cambio 24h extraidos de Binance en vez de CoinGecko.
  * Formacion 3 (cascada de 3+ niveles encadenados) detectada y priorizada.
  * Formacion 4 (manipulation sweep + reclaim del rango, Rama 9).
  * Stop-loss estructural debajo/encima de la consolidacion (Rama 5),
    en vez del 2% fijo de precio de v1.
  * Take-profit tras full sweep de los 2 niveles objetivo (Rama 6, TP_MODE).
  * Filtro Risk:Reward minimo (RR_MIN, curso = 1:3).
  * Aviso de numeros redondos entre entrada y TP (Rama 6, early exit).
  * Etiqueta Setup 80% (Rama 10).
  * Modo bear market: TP anticipado a +5% de movimiento limpio (Anexo 14).
  * Position sizing al RISK_PCT% del capital (ACCOUNT_SIZE) (Rama 5).
"""
import os, json, time, math, ssl, sys, datetime as dt

try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass
from urllib import request as urlrequest
from urllib import error as urlerror

def _envf(name, default):
    try: return float(os.environ[name])
    except (KeyError, ValueError): return float(default)
def _envi(name, default):
    try: return int(float(os.environ[name]))
    except (KeyError, ValueError): return int(default)
def _envb(name, default=False):
    v = os.environ.get(name)
    if v is None: return default
    return v.strip().lower() in ("1", "true", "yes", "si", "on")

MEXC_BASE  = "https://contract.mexc.com/api/v1/contract"
BYBIT_BASE = "https://api.bybit.com/v5/market"
# Spot: data-api.binance.vision = mirror oficial de datos publicos de Binance
# (api.binance.com devuelve 451 desde los runners de GitHub Actions en EEUU).
# Futures USDT-M: fapi.binance.com (sin mirror; ver nota 451 en README).
BINANCE_SPOT_BASE = os.environ.get("BINANCE_SPOT_BASE", "https://data-api.binance.vision")
BINANCE_FUT_BASE  = os.environ.get("BINANCE_FUT_BASE",  "https://fapi.binance.com")
DIAG       = _envb("DIAG", True)

MIN_VOLUME_GLOBAL = _envf("MIN_VOLUME_GLOBAL", 100_000_000)
MIN_MOVE_PCT      = _envf("MIN_MOVE_PCT", 10.0)
PRICE_TOL  = _envf("PRICE_TOL", 0.10)
VERIFY_LOG = _envb("VERIFY_LOG", True)

LEVERAGE = _envi("LEVERAGE", 10)

# --- Gestion de riesgo (Rama 5 / Rama 6) ----------------------------------- #
RR_MIN       = _envf("RR_MIN", 3.0)         # curso: minimo 1:3
SL_BUFFER    = _envf("SL_BUFFER", 0.002)    # margen bajo la consolidacion
SL_MAX_PCT   = _envf("SL_MAX_PCT", 0.04)    # stop estructural demasiado lejos -> descartar
TP_MODE      = os.environ.get("TP_MODE", "SWEEP").upper()  # SWEEP = tras los 2 niveles | L1 = primer nivel
ACCOUNT_SIZE = _envf("ACCOUNT_SIZE", 0)     # capital en USD (0 = no calcular tamano)
RISK_PCT     = _envf("RISK_PCT", 1.0)       # % de la cuenta arriesgado por trade (curso: max 2%)

# --- Bear market (Anexo 14) ------------------------------------------------- #
BEAR_MODE      = _envb("BEAR_MODE", False)
BEAR_MIN_CLEAN = _envf("BEAR_MIN_CLEAN", 0.05)  # >5% limpio -> TP anticipado

PIVOT_K       = _envi("PIVOT_K",       2)
LEVEL_TOL_PCT = _envf("LEVEL_TOL_PCT", 0.006)
MIN_TOUCHES   = _envi("MIN_TOUCHES",   2)
MIN_LEVELS    = _envi("MIN_LEVELS",    2)
GAP_TOP_COIN  = _envf("GAP_TOP_COIN",  0.020)
GAP_ALTCOIN   = _envf("GAP_ALTCOIN",   0.030)
CASCADE_MIN   = _envi("CASCADE_MIN",   3)    # Formacion 3: niveles encadenados
TOP_COINS = {"BTC","ETH","SOL","BNB","XRP","DOGE","ADA","AVAX",
             "LINK","TRX","DOT","MATIC","LTC","BCH","TON"}
STABLES = {
    "USDT","USDC","DAI","PYUSD","RLUSD","USD1","USDG",
    "USDCV","RUSD","USDS","FDUSD","TUSD","BUSD","GUSD","SUSD",
}

MAX_DIST_TO_LEVEL = _envf("MAX_DIST_TO_LEVEL", 0.15)
MIN_DIST_TO_LEVEL = _envf("MIN_DIST_TO_LEVEL", 0.01)
HTF_PIVOT_K       = _envi("HTF_PIVOT_K",     3)
HTF_MIN_TOUCHES   = _envi("HTF_MIN_TOUCHES", 1)
TF_LABELS         = {1: "ltf", 2: "1h", 3: "4h", 4: "diag"}

DIAG_MIN_TOUCHES = _envi("DIAG_MIN_TOUCHES", 3)
DIAG_PIVOT_K     = _envi("DIAG_PIVOT_K",     3)
DIAG_TOL_PCT     = _envf("DIAG_TOL_PCT",     0.008)

SWEEP_LOOKBACK = _envi("SWEEP_LOOKBACK", 30)
SWEEP_TOL_PCT  = _envf("SWEEP_TOL_PCT",  0.005)

CONSOL_LOOKBACK  = _envi("CONSOL_LOOKBACK",  10)
CONSOL_MAX_RANGE = _envf("CONSOL_MAX_RANGE", 0.030)
CONSOL_TO_LEVEL  = _envf("CONSOL_TO_LEVEL",  0.030)
BREAKOUT_BARS    = _envi("BREAKOUT_BARS",    4)
MAX_EXTENSION    = _envf("MAX_EXTENSION",    0.030)
RECLAIM_WINDOW   = _envi("RECLAIM_WINDOW",   6)   # velas para detectar sweep+reclaim

WICK_LOOKBACK    = 30
MAX_MEAN_WICK    = _envf("MAX_MEAN_WICK",    0.70)
MAX_GAP_PCT      = _envf("MAX_GAP_PCT",      0.025)
MAX_GAPS_ALLOWED = _envi("MAX_GAPS_ALLOWED", 3)

ALERTS_LOG    = "alerts_log.json"
HEARTBEAT_LOG = "heartbeat.json"
COOLDOWN_MIN  = _envi("COOLDOWN_MIN", 120)
FUNNEL = {
    "evaluados":0,"datos_ok":0,"tradeable":0,"con_niveles":0,
    "2_niveles_direccion":0,"dist_ok":0,"gap_ok":0,"entrada_ok":0,"rr_ok":0,
}
def _bump(key): FUNNEL[key] = FUNNEL.get(key,0)+1

# Mapeo de intervalos para Bybit
BYBIT_IV = {"Min5":"5", "Min15":"15", "Hour1":"60", "Hour4":"240"}

# ---- HTTP ----------------------------------------------------------------- #
# INSECURE_SSL=1: solo para pruebas locales detras de proxys/antivirus que
# interceptan TLS. NUNCA activarlo en GitHub Actions.
_SSL_CTX = ssl._create_unverified_context() if _envb("INSECURE_SSL") else None

def _get(url, retries=3, timeout=15):
    last = None
    for i in range(retries):
        try:
            req = urlrequest.Request(url, headers={"User-Agent":"tfz-scanner/2.0"})
            with urlrequest.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
                return json.loads(r.read().decode())
        except (urlerror.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e; time.sleep(1.0+i)
    print(f"[WARN] GET fallo {url}: {last}"); return None

def _get_raw(url, retries=3, timeout=20):
    last = None
    for i in range(retries):
        try:
            req = urlrequest.Request(url, headers={"User-Agent":"tfz-scanner/2.0"})
            with urlrequest.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
                return json.loads(r.read().decode())
        except (urlerror.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e; time.sleep(1.5+i)
    print(f"[WARN] GET raw fallo {url}: {last}"); return None

# ---- Tickers -------------------------------------------------------------- #
def get_mexc_tickers():
    d = _get(f"{MEXC_BASE}/ticker")
    if not d or not d.get("success"): return []
    return d.get("data",[])

def get_bybit_tickers():
    d = _get(f"{BYBIT_BASE}/tickers?category=linear")
    if not d or d.get("retCode") != 0: return []
    return d.get("result",{}).get("list",[])

# ---- Binance (volumen y cambio 24h) ---------------------------------------- #
def _parse_binance_24h(data):
    """Parsea un ticker 24h de Binance (spot o futures): base -> vol/ch24/price."""
    out = {}
    if not isinstance(data, list): return out
    for tk in data:
        sym = tk.get("symbol", "")
        if not sym.endswith("USDT"): continue
        base = sym[:-4]
        if not base or base in out: continue
        try:
            vol   = float(tk.get("quoteVolume") or 0)
            ch24  = float(tk.get("priceChangePercent"))
            price = float(tk.get("lastPrice") or 0)
        except (TypeError, ValueError):
            continue
        out[base] = {"vol": vol, "ch24": ch24, "price": price}
    return out

def get_global_market():
    """
    Mapa base -> {vol, ch24, price} usando EXCLUSIVAMENTE Binance.
    vol = quoteVolume 24h de spot + perpetuos USDT-M (el usuario opera ambos).
    ch24/price: del mercado de perpetuos si existe; si no, de spot.
    """
    spot = _parse_binance_24h(_get_raw(f"{BINANCE_SPOT_BASE}/api/v3/ticker/24hr"))
    fut  = _parse_binance_24h(_get_raw(f"{BINANCE_FUT_BASE}/fapi/v1/ticker/24hr"))
    if not spot and not fut: return {}
    out = {}
    for base in set(spot) | set(fut):
        s = spot.get(base); f = fut.get(base)
        ref = f or s
        out[base] = {
            "vol":   (s["vol"] if s else 0.0) + (f["vol"] if f else 0.0),
            "ch24":  ref["ch24"],
            "price": ref["price"],
            "name":  base,
        }
    return out

# ---- Klines --------------------------------------------------------------- #
def get_mexc_klines(symbol, interval, limit=200):
    d = _get(f"{MEXC_BASE}/kline/{symbol}?interval={interval}&limit={limit}")
    if not d or not d.get("success"): return []
    k = d.get("data",{})
    t = k.get("time",[])
    if not t: return []
    o,h,l,c = k.get("open",[]),k.get("high",[]),k.get("low",[]),k.get("close",[])
    vol,amt  = k.get("vol",[]),k.get("amount",[])
    out = []
    for i in range(len(t)):
        out.append({"t":t[i],"o":o[i],"h":h[i],"l":l[i],"c":c[i],
                    "vol":vol[i] if i<len(vol) else 0.0,
                    "amount":amt[i] if i<len(amt) else 0.0})
    return out

def get_bybit_klines(symbol, interval, limit=200):
    iv = BYBIT_IV.get(interval, "5")
    d = _get(f"{BYBIT_BASE}/kline?category=linear&symbol={symbol}&interval={iv}&limit={limit}")
    if not d or d.get("retCode") != 0: return []
    raw = list(reversed(d.get("result",{}).get("list",[])))
    out = []
    for row in raw:
        try:
            out.append({"t":int(row[0]),"o":float(row[1]),"h":float(row[2]),
                        "l":float(row[3]),"c":float(row[4]),"vol":float(row[5]),
                        "amount":float(row[6])})
        except (IndexError, ValueError): continue
    return out

def get_klines(symbol, interval, limit=200, exchange="MEXC"):
    if exchange == "BYBIT":
        return get_bybit_klines(symbol, interval, limit)
    return get_mexc_klines(symbol, interval, limit)

# ---- Seleccion de monedas ------------------------------------------------- #
def base_asset(symbol, exchange="MEXC"):
    if exchange == "BYBIT":
        return symbol[:-4] if symbol.endswith("USDT") else symbol
    return symbol.split("_")[0]

def trend_side(ch24):
    """Rama 2 del curso: la moneda es candidata si se mueve +-10% en 24h."""
    if ch24 is None: return None
    if ch24 >=  MIN_MOVE_PCT: return "LONG"
    if ch24 <= -MIN_MOVE_PCT: return "SHORT"
    return None

def select_candidates(mexc_tickers, bybit_tickers, gmarket):
    """
    Cruza MEXC + Bybit con CoinGecko. Cada base se evalua solo una vez
    (MEXC tiene prioridad; Bybit añade las que no estan en MEXC).
    Devuelve lista de (symbol, side, info, exchange).
    """
    if not gmarket: return []
    out  = []
    seen = set()  # bases ya procesadas

    # --- MEXC ---
    for tk in mexc_tickers:
        sym = tk.get("symbol","")
        if not sym.endswith("_USDT"): continue
        base = sym.split("_")[0]
        if base in STABLES or base in seen: continue
        g = gmarket.get(base.upper())
        if not g or (g.get("vol") or 0) < MIN_VOLUME_GLOBAL: continue
        side = trend_side(g.get("ch24"))
        if not side: continue
        mexc_price = tk.get("lastPrice"); bn_price = g.get("price")
        if mexc_price and bn_price and abs(bn_price-mexc_price)/mexc_price > PRICE_TOL:
            print(f"[COLISION] {sym}: MEXC={mexc_price:g} BINANCE={bn_price:g} -> descartada")
            continue
        ch24 = g.get("ch24") or 0.0
        if VERIFY_LOG:
            print(f"[FUENTE:MEXC] {sym}: {mexc_price:g} ch24={ch24:+.1f}% vol=${(g.get('vol') or 0)/1e6:.0f}M (Binance) -> {side}")
        seen.add(base)
        out.append((sym, side,
                    {"last":mexc_price,"vol_global":g.get("vol") or 0.0,
                     "ch24":round(ch24,2),"base":base},
                    "MEXC"))

    # --- Bybit (solo monedas nuevas no cubiertas por MEXC) ---
    for tk in bybit_tickers:
        sym = tk.get("symbol","")
        if not sym.endswith("USDT"): continue
        base = sym[:-4]
        if base in STABLES or base in seen: continue
        g = gmarket.get(base.upper())
        if not g or (g.get("vol") or 0) < MIN_VOLUME_GLOBAL: continue
        side = trend_side(g.get("ch24"))
        if not side: continue
        bybit_price = float(tk.get("lastPrice") or 0)
        bn_price    = g.get("price") or 0
        if bybit_price and bn_price and abs(bn_price-bybit_price)/bybit_price > PRICE_TOL:
            print(f"[COLISION] {sym}: BYBIT={bybit_price:g} BINANCE={bn_price:g} -> descartada")
            continue
        ch24 = g.get("ch24") or 0.0
        if VERIFY_LOG:
            print(f"[FUENTE:BYBIT] {sym}: {bybit_price:g} ch24={ch24:+.1f}% vol=${(g.get('vol') or 0)/1e6:.0f}M (Binance) -> {side}")
        seen.add(base)
        out.append((sym, side,
                    {"last":bybit_price,"vol_global":g.get("vol") or 0.0,
                     "ch24":round(ch24,2),"base":base},
                    "BYBIT"))
    return out

# ---- Deteccion de niveles ------------------------------------------------- #
def pivots(candles, k, kind):
    res = []; n = len(candles)
    for i in range(k, n-k):
        if kind=="high":
            v = candles[i]["h"]
            if all(candles[j]["h"]<=v for j in range(i-k,i+k+1) if j!=i): res.append(i)
        else:
            v = candles[i]["l"]
            if all(candles[j]["l"]>=v for j in range(i-k,i+k+1) if j!=i): res.append(i)
    return res

def cluster_levels(prices, tol):
    if not prices: return []
    prices = sorted(prices); clusters = [[prices[0]]]
    for p in prices[1:]:
        if abs(p-clusters[-1][-1])/clusters[-1][-1] <= tol: clusters[-1].append(p)
        else: clusters.append([p])
    return [(sum(c)/len(c),len(c)) for c in clusters]

def liquidity_levels(candles, side, pivot_k=None, min_touches=None):
    pk = PIVOT_K if pivot_k is None else pivot_k
    mt = MIN_TOUCHES if min_touches is None else min_touches
    kind = "high" if side=="LONG" else "low"
    idx = pivots(candles, pk, kind)
    prices = [candles[i][kind[0]] for i in idx]
    levels = cluster_levels(prices, LEVEL_TOL_PCT)
    return [(p,n) for (p,n) in levels if n>=mt]

def trendline_levels(candles, side, pivot_k=None, min_touches=None):
    pk = DIAG_PIVOT_K if pivot_k is None else pivot_k
    mt = DIAG_MIN_TOUCHES if min_touches is None else min_touches
    kind = "high" if side=="LONG" else "low"
    idx = pivots(candles, pk, kind)
    if len(idx)<mt: return []
    pivot_pts   = [(i, candles[i][kind[0]]) for i in idx]
    current_idx = len(candles)-1
    price       = candles[-1]["c"]
    seen = set(); results = []
    n = len(pivot_pts)
    for ai in range(n-1):
        for bi in range(ai+1, n):
            x1,y1 = pivot_pts[ai]; x2,y2 = pivot_pts[bi]
            if x2==x1: continue
            slope = (y2-y1)/(x2-x1)
            touch_count = 2
            for ci in range(n):
                if ci==ai or ci==bi: continue
                xc,yc = pivot_pts[ci]
                projected = y1+slope*(xc-x1)
                if projected>0 and abs(yc-projected)/projected<=DIAG_TOL_PCT:
                    touch_count+=1
            if touch_count<mt: continue
            proj_now = y1+slope*(current_idx-x1)
            if proj_now<=0: continue
            if side=="LONG"  and proj_now<=price: continue
            if side=="SHORT" and proj_now>=price: continue
            bucket = round(proj_now/price*200)
            if bucket in seen: continue
            seen.add(bucket)
            results.append((proj_now, touch_count))
    return results

def check_prior_sweep(candles, level_price, side):
    lookback = min(SWEEP_LOOKBACK, len(candles)-3)
    if lookback<=0: return False
    tol = level_price*SWEEP_TOL_PCT
    for c in candles[-(lookback+3):-3]:
        if side=="LONG":
            if c["h"]>=level_price-tol and c["c"]<level_price: return True
        else:
            if c["l"]<=level_price+tol and c["c"]>level_price: return True
    return False

# ---- Filtros de calidad --------------------------------------------------- #
def is_untradeable(candles):
    recent = candles[-WICK_LOOKBACK:]; wicks=[]
    for c in recent:
        rng=c["h"]-c["l"]
        if rng<=0: continue
        body=abs(c["c"]-c["o"]); wicks.append((rng-body)/rng)
    if wicks and (sum(wicks)/len(wicks))>MAX_MEAN_WICK: return True
    gaps=0
    for i in range(1,len(recent)):
        prev=recent[i-1]["c"]
        if prev and abs(recent[i]["o"]-prev)/prev>MAX_GAP_PCT: gaps+=1
    return gaps>MAX_GAPS_ALLOWED

def find_consolidation(candles, side, nearest_level):
    if len(candles)<CONSOL_LOOKBACK+1: return None
    last_close=candles[-1]["c"]
    for back in range(0, max(1,BREAKOUT_BARS)):
        ti=len(candles)-1-back
        if ti-CONSOL_LOOKBACK<0: continue
        base=candles[ti-CONSOL_LOOKBACK:ti]; trigger=candles[ti]
        highs=[c["h"] for c in base]; lows=[c["l"] for c in base]
        hi,lo=max(highs),min(lows)
        if lo<=0: continue
        rng_pct=(hi-lo)/lo
        if rng_pct>CONSOL_MAX_RANGE: continue
        trng=trigger["h"]-trigger["l"]; tbody=abs(trigger["c"]-trigger["o"])
        if trng>0 and tbody/trng<0.4: continue
        if side=="LONG":
            if (nearest_level-hi)/nearest_level>CONSOL_TO_LEVEL: continue
            if not (trigger["c"]>hi): continue
            if (last_close-hi)/hi>MAX_EXTENSION: continue
        else:
            if (lo-nearest_level)/nearest_level>CONSOL_TO_LEVEL: continue
            if not (trigger["c"]<lo): continue
            if (lo-last_close)/lo>MAX_EXTENSION: continue
        return {"consol_high":hi,"consol_low":lo,
                "range_pct":round(rng_pct*100,2),
                "trigger_close":trigger["c"],"trigger_back":back}
    return None

def find_reclaim(candles, side):
    """
    Formacion 4 (Rama 9 — trampa del Market Maker):
    el precio barre la liquidez del lado contrario (sweep fuera del rango)
    y luego RECLAMA el rango. La vela actual debe cerrar de nuevo dentro.
    Sweep + reclaim = entrada; el stop va al extremo del sweep.
    """
    need = CONSOL_LOOKBACK + RECLAIM_WINDOW + 1
    if len(candles) < need: return None
    base   = candles[-(CONSOL_LOOKBACK+RECLAIM_WINDOW):-RECLAIM_WINDOW]
    recent = candles[-RECLAIM_WINDOW:]
    last   = candles[-1]
    hi = max(c["h"] for c in base); lo = min(c["l"] for c in base)
    if lo<=0: return None
    rng=(hi-lo)/lo
    if rng>CONSOL_MAX_RANGE*1.5: return None
    if side=="LONG":
        sweep_low = min(c["l"] for c in recent)
        swept     = sweep_low < lo*(1-SWEEP_TOL_PCT)
        back_in   = lo < last["c"] <= hi*(1+MAX_EXTENSION)
        if swept and back_in and last["c"] > last["o"]:
            return {"consol_high":hi,"consol_low":lo,"range_pct":round(rng*100,2),
                    "trigger_close":last["c"],"sweep_extreme":sweep_low}
    else:
        sweep_high = max(c["h"] for c in recent)
        swept      = sweep_high > hi*(1+SWEEP_TOL_PCT)
        back_in    = lo*(1-MAX_EXTENSION) <= last["c"] < hi
        if swept and back_in and last["c"] < last["o"]:
            return {"consol_high":hi,"consol_low":lo,"range_pct":round(rng*100,2),
                    "trigger_close":last["c"],"sweep_extreme":sweep_high}
    return None

def round_number_between(entry, tp):
    """
    Rama 6: numeros redondos (1.00, 0.90, 250, 4000...) entre la entrada y el
    TP actuan de muro. Devuelve el primero que bloquea el camino, o None.
    """
    lo, hi = sorted((float(entry), float(tp)))
    if lo <= 0 or hi <= lo: return None
    mag = 10.0 ** math.floor(math.log10(hi))
    for step in (mag, mag/2, mag/10):
        n = math.floor(lo/step) + 1
        cand = n*step
        # debe quedar estrictamente entre ambos (con margen para no marcar el propio TP)
        if lo*1.001 < cand < hi*0.999:
            return round(cand, 10)
    return None

# ---- Evaluacion ----------------------------------------------------------- #
def evaluate(symbol, side, info, exchange="MEXC"):
    _bump("evaluados")
    price = info.get("last")
    if not price or price<=0: return None

    k5  = get_klines(symbol,"Min15",limit=200,exchange=exchange)
    k1  = get_klines(symbol,"Min5", limit=200,exchange=exchange)
    if len(k1)<CONSOL_LOOKBACK+5 or len(k5)<20: return None
    _bump("datos_ok")
    if is_untradeable(k1): return None
    _bump("tradeable")

    k1h = get_klines(symbol,"Hour1",limit=100,exchange=exchange)
    k4h = get_klines(symbol,"Hour4",limit=50, exchange=exchange)

    ltf_lvls  = [(p,n,1,"ltf") for p,n in
                 liquidity_levels(k1,side)+liquidity_levels(k5,side)]
    htf_lvls  = []
    if k1h and len(k1h)>=10:
        htf_lvls += [(p,n,2,"1h") for p,n in
                     liquidity_levels(k1h,side,pivot_k=HTF_PIVOT_K,min_touches=HTF_MIN_TOUCHES)]
    if k4h and len(k4h)>=5:
        htf_lvls += [(p,n,3,"4h") for p,n in
                     liquidity_levels(k4h,side,pivot_k=HTF_PIVOT_K,min_touches=HTF_MIN_TOUCHES)]
    diag_lvls = [(p,n,4,"diag") for p,n in
                 trendline_levels(k1,side)+trendline_levels(k5,side)]

    all_lvls = ltf_lvls+htf_lvls+diag_lvls
    if not all_lvls: return None
    _bump("con_niveles")

    if side=="LONG":
        directional = [(p,n,tf,lbl) for p,n,tf,lbl in all_lvls if p>price]
    else:
        directional = [(p,n,tf,lbl) for p,n,tf,lbl in all_lvls if p<price]
    if len(directional)<MIN_LEVELS: return None
    _bump("2_niveles_direccion")

    # Orden geometrico: t1 = nivel mas cercano al precio, t2 = siguiente.
    geo = sorted(directional, key=lambda x: abs(x[0]-price))
    t1, t1_n, t1_w, t1_lbl = geo[0]
    t2 = geo[1][0]

    dist = abs(t1-price)/price
    if dist>MAX_DIST_TO_LEVEL or dist<MIN_DIST_TO_LEVEL: return None
    _bump("dist_ok")

    base_coin = info.get("base") or base_asset(symbol, exchange)
    max_gap   = GAP_TOP_COIN if base_coin in TOP_COINS else GAP_ALTCOIN
    gap = abs(t2-t1)/t1
    if t1_w<2 and gap>max_gap: return None
    _bump("gap_ok")

    # Formacion 3: contar niveles encadenados en cascada (gaps consecutivos <= max_gap)
    cascade = 1; prev = t1
    for p,_,_,_ in geo[1:]:
        if abs(p-prev)/prev <= max_gap:
            cascade += 1; prev = p
        else:
            break

    # Entrada: breakout de consolidacion (F1/F2/F3) o sweep+reclaim (F4)
    consol  = find_consolidation(k1, side, t1)
    reclaim = None
    if not consol:
        reclaim = find_reclaim(k1, side)
        if not reclaim: return None
    struct = consol or reclaim
    _bump("entrada_ok")

    swept = check_prior_sweep(k1, t1, side)
    if reclaim:                  formation = "F4_reclaim"
    elif cascade >= CASCADE_MIN: formation = "F3_cascade"
    elif swept:                  formation = "F2_sweep"
    else:                        formation = "F1_breakout"

    entry = float(price)

    # --- Stop-loss estructural (Rama 5): bajo la consolidacion / extremo del sweep
    if side=="LONG":
        sl_base = reclaim["sweep_extreme"] if reclaim else struct["consol_low"]
        sl      = sl_base*(1-SL_BUFFER)
        sl_dist = (entry-sl)/entry
    else:
        sl_base = reclaim["sweep_extreme"] if reclaim else struct["consol_high"]
        sl      = sl_base*(1+SL_BUFFER)
        sl_dist = (sl-entry)/entry
    if sl_dist<=0 or sl_dist>SL_MAX_PCT: return None

    # --- Take-profit (Rama 6): tras el full sweep de los 2 niveles objetivo
    if TP_MODE=="L1":
        tp = t1
    else:
        tp = max(t1,t2) if side=="LONG" else min(t1,t2)
    # Anexo 14: en bear market, TP anticipado si hay >5% de camino limpio
    bear_adjusted = False
    if BEAR_MODE and abs(tp-entry)/entry > BEAR_MIN_CLEAN:
        tp = entry*(1+BEAR_MIN_CLEAN) if side=="LONG" else entry*(1-BEAR_MIN_CLEAN)
        bear_adjusted = True

    # --- Filtro Risk:Reward (curso: minimo 1:3)
    rr = abs(tp-entry)/(entry*sl_dist)
    if rr < RR_MIN: return None
    _bump("rr_ok")

    # --- Aviso de numero redondo bloqueando el camino al TP (Rama 6)
    round_warn = round_number_between(entry, tp)

    # --- Setup 80% (Rama 10): moneda activa hoy + 2 niveles horizontales
    #     limpios + consolidacion + breakout
    ch24 = info.get("ch24") or 0.0
    active_today = ch24 >= MIN_MOVE_PCT if side=="LONG" else ch24 <= -MIN_MOVE_PCT
    setup80 = bool(consol) and active_today and t1_lbl!="diag" and t1_n>=2

    # --- Position sizing (Rama 5: riesgo max RISK_PCT% del capital)
    risk_usd = pos_notional = pos_margin = None
    if ACCOUNT_SIZE>0:
        risk_usd     = round(ACCOUNT_SIZE*RISK_PCT/100, 2)
        pos_notional = round(risk_usd/sl_dist, 2)
        pos_margin   = round(pos_notional/LEVERAGE, 2)

    sl    = round(sl, 10)
    tp    = round(tp, 10)

    now  = dt.datetime.now(dt.timezone.utc)
    base = info.get("base") or symbol.split("_")[0]
    # TradingView link segun exchange
    if exchange=="BYBIT":
        tv_sym    = symbol+".P"
        tv_prefix = "BYBIT"
    else:
        tv_sym    = base+"USDT.P"
        tv_prefix = "MEXC"
    tv_link = f"https://www.tradingview.com/chart/?symbol={tv_prefix}%3A{tv_sym}&interval=5"
    vol_millions = round((info.get("vol_global") or 0)/1_000_000, 0)

    return {
        "id":                f"{symbol}_{int(now.timestamp())}",
        "symbol":            symbol,
        "exchange":          exchange,
        "direction":         side,
        "entry_price":       entry,
        "sl":                sl,
        "tp":                tp,
        "rr":                round(rr,2),
        "sl_basis":          "sweep" if reclaim else "consolidacion",
        "tp_mode":           TP_MODE,
        "bear_adjusted":     bear_adjusted,
        "leverage":          LEVERAGE,
        "timeframe":         "5m",
        "setup":             f"TFZ_{formation}",
        "formation":         formation,
        "setup80":           setup80,
        "cascade_levels":    cascade,
        "levels":            [round(t1,10), round(t2,10)],
        "l1_tf":             t1_lbl,
        "l1_touches":        t1_n,
        "level_gap_pct":     round(gap*100,2),
        "dist_to_level_pct": round(dist*100,2),
        "consol_range_pct":  struct["range_pct"],
        "round_number_warn": round_warn,
        "risk_usd":          risk_usd,
        "pos_notional":      pos_notional,
        "pos_margin":        pos_margin,
        "ch24":              info.get("ch24"),
        "change_pct":        info.get("ch24"),
        "vol_ratio":         vol_millions,
        "tv_link":           tv_link,
        "timestamp":         now.isoformat(),
        "created_ts":        int(now.timestamp()),
        "status":            "OPEN",
    }

# ---- Log / dedup / Telegram ----------------------------------------------- #
def load_log():
    if not os.path.exists(ALERTS_LOG): return []
    try:
        with open(ALERTS_LOG,"r",encoding="utf-8") as f: return json.load(f)
    except (json.JSONDecodeError, OSError): return []

def save_log(log):
    with open(ALERTS_LOG,"w",encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)

def record_heartbeat():
    now = dt.datetime.now(dt.timezone.utc)
    try:
        if os.path.exists(HEARTBEAT_LOG):
            with open(HEARTBEAT_LOG,"r",encoding="utf-8") as f: stamps = json.load(f)
        else:
            stamps = []
    except (json.JSONDecodeError, OSError):
        stamps = []
    stamps.append(now.isoformat())
    cutoff = now - dt.timedelta(hours=25)
    stamps = [s for s in stamps if dt.datetime.fromisoformat(s) > cutoff]
    with open(HEARTBEAT_LOG,"w",encoding="utf-8") as f:
        json.dump(stamps, f, ensure_ascii=False, indent=2)

def recently_alerted(log, symbol, side):
    cutoff = time.time()-COOLDOWN_MIN*60
    # Dedup por base asset + side (cubre mismo coin en distintos exchanges)
    base = symbol.split("_")[0] if "_" in symbol else symbol[:-4]
    for a in reversed(log):
        a_base = a.get("symbol","").split("_")[0] if "_" in a.get("symbol","") else a.get("symbol","")[:-4]
        if a_base==base and a.get("direction")==side:
            if (a.get("created_ts") or 0)>=cutoff: return True
    return False

def send_telegram(text):
    token = os.environ.get("TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat  = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[WARN] Faltan credenciales de Telegram; no se envia."); print(text); return
    url     = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id":chat,"text":text,
                          "parse_mode":"HTML","disable_web_page_preview":True}).encode()
    req = urlrequest.Request(url, data=payload, headers={"Content-Type":"application/json"})
    try:
        with urlrequest.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
            body = resp.read().decode("utf-8", "replace")
            print(f"[TG] status={resp.status} resp={body[:200]}")
    except urlerror.URLError as e:
        print(f"[WARN] Telegram fallo: {e}")
        err_body = getattr(e, "read", None)
        if err_body:
            try: print(f"[WARN] Telegram body: {err_body().decode('utf-8','replace')[:300]}")
            except Exception: pass

FORMATION_TAGS = {
    "F1_breakout": "F1 breakout",
    "F2_sweep":    "F2 sweep ⚡",
    "F3_cascade":  "F3 cascada 🌊",
    "F4_reclaim":  "F4 reclaim 🔄",
}

def format_alert(a):
    arrow     = "🟢 LONG" if a["direction"]=="LONG" else "🔴 SHORT"
    levels    = " / ".join(f"{x:g}" for x in a["levels"])
    tf_tag    = f"[{a.get('l1_tf','?')} {a.get('l1_touches','?')}t] " if a.get('l1_tf') else ""
    exch_tag  = f"[{a.get('exchange','?')}] "
    form_tag  = FORMATION_TAGS.get(a.get("formation",""), a.get("formation",""))
    s80_tag   = " 🎯SETUP 80%" if a.get("setup80") else ""
    tp_pct    = round(abs(a['tp']-a['entry_price'])/a['entry_price']*100,2)
    sl_pct    = round(abs(a['sl']-a['entry_price'])/a['entry_price']*100,2)
    lines = [
        f"<b>{arrow} {a['symbol']}</b> {exch_tag}({form_tag}){s80_tag}",
        f"Entrada: <b>{a['entry_price']:g}</b>",
        f"SL: {a['sl']:g} (-{sl_pct}%, bajo {a.get('sl_basis','consolidacion')})  "
        f"TP: {a['tp']:g} (+{tp_pct}%)  <b>R:R 1:{a.get('rr','?')}</b>  x{a['leverage']}",
        f"Niveles objetivo: {tf_tag}{levels} (gap {a['level_gap_pct']}%)",
    ]
    if a.get("cascade_levels",1)>=3:
        lines.append(f"Cascada: {a['cascade_levels']} niveles encadenados")
    lines.append(f"Dist. al nivel: {a['dist_to_level_pct']}%  Base: {a['consol_range_pct']}%")
    lines.append(f"Cambio 24h: {a['ch24']}%  Vol 24h Binance: ${a['vol_ratio']:g}M")
    if a.get("round_number_warn"):
        lines.append(f"⚠️ Numero redondo en el camino: {a['round_number_warn']:g} — considerar TP anticipado")
    if a.get("bear_adjusted"):
        lines.append("🐻 TP ajustado a +5% (modo bear market)")
    if a.get("risk_usd"):
        lines.append(f"💰 Riesgo: ${a['risk_usd']:g} ({RISK_PCT:g}%)  "
                     f"Posicion: ${a['pos_notional']:g}  Margen x{a['leverage']}: ${a['pos_margin']:g}")
    elif sl_pct > 0:
        lines.append(f"Tamano sugerido: nocional = cuenta x {RISK_PCT/sl_pct:.2f} "
                     f"(arriesgas {RISK_PCT:g}% con SL {sl_pct}%)")
    lines += [
        "",
        "— — Plan Omni (copiar) — —",
        f"Entrada: {a['entry_price']:g}",
        f"SL ({sl_pct}%): {a['sl']:g}",
        f"TP nivel: {a['tp']:g} ({tp_pct}%)",
        f"<a href=\"{a['tv_link']}\">📈 Grafico 5m (TradingView)</a>",
    ]
    return "\n".join(lines)

# ---- Main ----------------------------------------------------------------- #
def main():
    print(f"[{dt.datetime.now(dt.timezone.utc).isoformat()}] TFZ-scanner v2 inicio")
    record_heartbeat()

    mexc_tickers  = get_mexc_tickers()
    bybit_tickers = get_bybit_tickers()
    print(f"Tickers: MEXC={len(mexc_tickers)} Bybit={len(bybit_tickers)}")

    gmarket = get_global_market()
    if not gmarket:
        print("[WARN] Binance no disponible; sin candidatos.")
        return
    print(f"Binance: {len(gmarket)} monedas cargadas (spot + perpetuos).")

    candidates = select_candidates(mexc_tickers, bybit_tickers, gmarket)
    print(f"Candidatos tras seleccion: {len(candidates)}")

    log = load_log(); new_alerts = 0
    for sym, side, info, exchange in candidates:
        if recently_alerted(log, sym, side): continue
        try:   alert = evaluate(sym, side, info, exchange)
        except Exception as e:
            print(f"[WARN] {sym} ({exchange}) error: {e}"); alert = None
        if not alert: continue
        send_telegram(format_alert(alert))
        log.append(alert); new_alerts+=1
        print(f" ALERTA {side} {sym} [{exchange}] @ {alert['entry_price']}  "
              f"[{alert['formation']}] RR=1:{alert['rr']}{' SETUP80' if alert['setup80'] else ''}")
        time.sleep(1.2)

    save_log(log)
    if DIAG:
        print("---- Embudo de diagnostico ----")
        print(f" candidatos............... {len(candidates)}")
        print(f" evaluados................ {FUNNEL['evaluados']}")
        print(f" con datos OHLC ok........ {FUNNEL['datos_ok']}")
        print(f" tradeables (no Anexo3)... {FUNNEL['tradeable']}")
        print(f" con >=1 nivel............ {FUNNEL['con_niveles']}")
        print(f" con 2 niveles direccion.. {FUNNEL['2_niveles_direccion']}")
        print(f" nivel a <{MAX_DIST_TO_LEVEL*100:g}% del precio.. {FUNNEL['dist_ok']}")
        print(f" niveles cercanos (gap)... {FUNNEL['gap_ok']}")
        print(f" entrada (brk/reclaim).... {FUNNEL['entrada_ok']}")
        print(f" R:R >= 1:{RR_MIN:g} -> ALERTA... {FUNNEL['rr_ok']}")
        print("-------------------------------")
        print(f"Alertas nuevas: {new_alerts}")

if __name__ == "__main__":
    main()
