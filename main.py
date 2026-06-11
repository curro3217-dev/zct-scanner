#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TFZ-SCANNER — scanner intradia perpetuos USDT. Universo/ejecucion: MEXC. Filtro volumen/movimiento: Binance Spot (data-api.binance.vision).
"""
import os, json, time, math, sys, datetime as dt

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
BINANCE_DATA = "https://data-api.binance.vision/api/v3/ticker/24hr"
DIAG       = _envb("DIAG", True)

MIN_VOLUME_GLOBAL = _envf("MIN_VOLUME_GLOBAL", 100_000_000)
MIN_MOVE_PCT = _envf("MIN_MOVE_PCT", 10.0)
VERIFY_LOG = _envb("VERIFY_LOG", True)

SL_PCT   = 0.015
TP_PCT   = 0.04
LEVERAGE = 10
RISK_PCT = 0.01  # arriesgar el 1% de la cuenta por operacion

PIVOT_K       = _envi("PIVOT_K",       2)
LEVEL_TOL_PCT = _envf("LEVEL_TOL_PCT", 0.006)
MIN_TOUCHES   = _envi("MIN_TOUCHES",   2)
MIN_LEVELS    = _envi("MIN_LEVELS",    2)
GAP_TOP_COIN  = _envf("GAP_TOP_COIN",  0.020)
GAP_ALTCOIN   = _envf("GAP_ALTCOIN",   0.030)
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

WICK_LOOKBACK    = 30
MAX_MEAN_WICK    = _envf("MAX_MEAN_WICK",    0.70)
MAX_GAP_PCT      = _envf("MAX_GAP_PCT",      0.025)
MAX_GAPS_ALLOWED = _envi("MAX_GAPS_ALLOWED", 3)

ALERTS_LOG   = "alerts_log.json"
HEARTBEAT_LOG = "heartbeat.json"
COOLDOWN_MIN = _envi("COOLDOWN_MIN", 120)
FUNNEL = {
    "evaluados":0,"datos_ok":0,"tradeable":0,"con_niveles":0,
    "2_niveles_direccion":0,"dist_ok":0,"gap_ok":0,"breakout_alerta":0,
}
def _bump(key): FUNNEL[key] = FUNNEL.get(key,0)+1


# ---- HTTP ----------------------------------------------------------------- #
def _get(url, retries=3, timeout=15):
    last = None
    for i in range(retries):
        try:
            req = urlrequest.Request(url, headers={"User-Agent":"tfz-scanner/1.0"})
            with urlrequest.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except (urlerror.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e; time.sleep(1.0+i)
    print(f"[WARN] GET fallo {url}: {last}"); return None

def _get_raw(url, retries=3, timeout=20):
    last = None
    for i in range(retries):
        try:
            req = urlrequest.Request(url, headers={"User-Agent":"tfz-scanner/1.0"})
            with urlrequest.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except (urlerror.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e; time.sleep(1.5+i)
    print(f"[WARN] GET raw fallo {url}: {last}"); return None

# ---- Tickers -------------------------------------------------------------- #
def get_mexc_tickers():
    d = _get(f"{MEXC_BASE}/ticker")
    if not d or not d.get("success"): return []
    return d.get("data",[])

# ---- Binance Spot (volumen/movimiento) ------------------------------------ #
def get_binance_tickers():
    """
    Devuelve dict {BASE: {"vol": quoteVolume, "chg": priceChangePercent}} con
    datos spot de Binance (data-api.binance.vision, sin proxy). None si falla.
    """
    d = _get(BINANCE_DATA)
    if not d or not isinstance(d, list): return None
    out = {}
    for t in d:
        sym = t.get("symbol","")
        if not sym.endswith("USDT"): continue
        base = sym[:-4]
        try:
            vol = float(t.get("quoteVolume") or 0)
            chg = float(t.get("priceChangePercent") or 0)
        except (ValueError, TypeError): continue
        out[base] = {"vol": vol, "chg": chg}
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


def get_klines(symbol, interval, limit=200, exchange="MEXC"):
    return get_mexc_klines(symbol, interval, limit)

# ---- Seleccion de monedas ------------------------------------------------- #
def select_candidates(mexc_tickers, binance):
    """
    Cruza el universo MEXC (simbolos/precios) con el volumen y movimiento 24h
    de Binance Spot (data-api.binance.vision). Si Binance no responde, no hay
    candidatos en este scan (se evita operar con datos de volumen obsoletos).
    Devuelve lista de (symbol, side, info, exchange).
    """
    out = []
    if binance is None:
        print("[WARN] Binance Spot (data-api.binance.vision) no disponible: 0 candidatos en este scan")
        return out
    for tk in mexc_tickers:
        sym = tk.get("symbol","")
        if not sym.endswith("_USDT"): continue
        base = sym.split("_")[0]
        if base in STABLES: continue
        b = binance.get(base)
        if not b: continue
        vol = b["vol"]
        ch24 = b["chg"]
        try:
            price = float(tk.get("lastPrice") or 0)
        except (ValueError, TypeError): continue
        if vol < MIN_VOLUME_GLOBAL: continue
        if ch24 >= MIN_MOVE_PCT: side = "LONG"
        elif ch24 <= -MIN_MOVE_PCT: side = "SHORT"
        else: continue
        if VERIFY_LOG:
            print(f"[FUENTE:BINANCE-SPOT] {sym}: {price:g} ch24={ch24:+.1f}% vol=${vol/1e6:.0f}M -> {side}")
        out.append((sym, side,
                     {"last":price,"vol_global":vol,
                      "ch24":round(ch24,2),"ch7":0.0,"base":base},
                     "MEXC"))
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
    if not directional: return None

    geo_nearest = min(directional, key=lambda x: abs(x[0]-price))[0]

    def _lscore(p,n,tf_w,lbl): return abs(p-price)/price/(min(n,5)*tf_w)
    scored = sorted(directional, key=lambda x: _lscore(x[0],x[1],x[2],x[3]))
    if len(scored)<MIN_LEVELS: return None
    _bump("2_niveles_direccion")

    l1,l1_n,l1_tf_w,l1_label = scored[0]
    l2 = scored[1][0]

    dist = abs(l1-price)/price
    if dist>MAX_DIST_TO_LEVEL or dist<MIN_DIST_TO_LEVEL: return None
    _bump("dist_ok")

    gap = abs(l2-l1)/l1
    if l1_tf_w<2:
        base = info.get("base") or symbol.split("_")[0]
        max_gap = GAP_TOP_COIN if base in TOP_COINS else GAP_ALTCOIN
        if gap>max_gap: return None
    _bump("gap_ok")

    consol = find_consolidation(k1, side, geo_nearest)
    if not consol: return None
    _bump("breakout_alerta")

    swept     = check_prior_sweep(k1, l1, side)
    formation = "F2_sweep" if swept else "F1_breakout"

    entry = float(price)
    sl    = round(entry*(1-SL_PCT) if side=="LONG" else entry*(1+SL_PCT), 10)
    tp    = round(entry*(1+TP_PCT) if side=="LONG" else entry*(1-TP_PCT), 10)

    now = dt.datetime.now(dt.timezone.utc)
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
        "leverage":          LEVERAGE,
        "timeframe":         "5m",
        "setup":             f"TFZ_{formation}",
        "formation":         formation,
        "levels":            [round(l1,10), round(l2,10)],
        "l1_tf":             l1_label,
        "l1_touches":        l1_n,
        "level_gap_pct":     round(gap*100,2),
        "dist_to_level_pct": round(dist*100,2),
        "consol_range_pct":  consol["range_pct"],
        "ch24":              info.get("ch24"),
        "ch7":               info.get("ch7"),
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
        with urlrequest.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", "replace")
            print(f"[TG] status={resp.status} resp={body[:200]}")
    except urlerror.URLError as e:
        print(f"[WARN] Telegram fallo: {e}")
        err_body = getattr(e, "read", None)
        if err_body:
            try: print(f"[WARN] Telegram body: {err_body().decode('utf-8','replace')[:300]}")
            except Exception: pass

def format_alert(a):
    arrow     = "🟢 LONG" if a["direction"]=="LONG" else "🔴 SHORT"
    levels    = " / ".join(f"{x:g}" for x in a["levels"])
    tf_tag    = f"[{a.get('l1_tf','?')} {a.get('l1_touches','?')}t] " if a.get('l1_tf') else ""
    sweep_tag = " ⚡sweep" if a.get("formation")=="F2_sweep" else ""
    exch_tag  = f"[{a.get('exchange','?')}] "
    return (
        f"<b>{arrow} {a['symbol']}</b> {exch_tag}(TFZ{sweep_tag})\n"
        f"Entrada: <b>{a['entry_price']:g}</b>\n"
        f"SL: {a['sl']:g} ({SL_PCT*100:.1f}%) TP: {a['tp']:g} ({round(abs(a['tp']-a['entry_price'])/a['entry_price']*100,2)}%) x{a['leverage']}\n"
        f"Tamano sugerido (riesgo): nocional = cuenta x {RISK_PCT/SL_PCT:.2f} (arriesgas {RISK_PCT*100:.0f}% con SL {SL_PCT*100:.1f}%)\n"
        f"Niveles objetivo: {tf_tag}{levels} (gap {a['level_gap_pct']}%)\n"
        f"Dist. al nivel: {a['dist_to_level_pct']}% Base: {a['consol_range_pct']}%\n"
        f"Cambio 24h: {a['ch24']}% 7d: {a['ch7']}%\n"
        f"\n— — Plan Omni (copiar) — —\n"
        f"Entrada: {a['entry_price']:g}\n"
        f"SL (-{SL_PCT*100:.1f}%): {a['sl']:g}\n"
        f"TP nivel: {a['tp']:g} ({round(abs(a['tp']-a['entry_price'])/a['entry_price']*100,2)}%)\n"
        f"<a href=\"{a['tv_link']}\">📈 Grafico 5m (TradingView)</a>"
    )

# ---- Main ----------------------------------------------------------------- #
def main():
    print(f"[{dt.datetime.utcnow().isoformat()}] TFZ-scanner inicio")
    record_heartbeat()

    mexc_tickers = get_mexc_tickers()
    binance = get_binance_tickers()
    print(f"Tickers: MEXC={len(mexc_tickers)} | Binance-spot={'OK' if binance is not None else 'FAIL'}")

    candidates = select_candidates(mexc_tickers, binance)
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
        print(f" ALERTA {side} {sym} [{exchange}] @ {alert['entry_price']}  [{alert['formation']}]")
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
        print(f" breakout -> ALERTA....... {FUNNEL['breakout_alerta']}")
        print("-------------------------------")
        print(f"Alertas nuevas: {new_alerts}")

if __name__ == "__main__":
    main()
