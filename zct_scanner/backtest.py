#!/usr/bin/env python3
"""
ZCT Backtester v6 — Solo niveles con edge probado (v5 run#12)

  BREAKOUT  : solo LONG en P4HH. MA up, vol >= 200%, espera max 3 velas.
              Requiere vela de entrada alcista (close > open).
              Distancia maxima 0.4% (0.4-0.5% tenia WR 0% en v5).

  MR        : solo LONG en P4HL. crosses >= 7, vol < 100%.
              Eliminado PDL (WR 17% en v5, 2/12 — demasiado ruido en 15m).

  SL = 1%   TP = 2%   (RR 2:1 — rentable con >33% WR)
  Objetivo: 50%+ WR

Uso: python backtest.py
Requiere: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID (env vars)
"""

import os, time, csv, logging, html as html_mod
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# ══════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════
PROXIMITY_PCT   = 0.004       # 0.4% de distancia al nivel (v5: 0.4-0.5% tenia WR 0% en BREAKOUT)
MIN_DIST_PCT    = 0.20        # distancia mínima al nivel (%)
SL_PCT          = 0.01        # Stop Loss 1%
TP_PCT          = SL_PCT * 2  # Take Profit 2% (RR 2:1)
SMMA_LEN        = 30
VOL_MA_LEN      = 20
CROSS_LB        = 50
MA_DIR_LB       = 5
MA_DIR_THR      = 0.08

OUTCOME_CANDLES = 32          # 8h de ventana para que el trade resuelva
WARMUP_CANDLES  = SMMA_LEN + CROSS_LB + MA_DIR_LB + 5
CHANGE_LB       = 96

# BREAKOUT — solo LONG con momentum real
BKOUT_MAX_CROSSES = 1
BKOUT_MIN_VOL     = 200       # vol spike mínimo 200%
BKOUT_MAX_WAIT    = 3         # esperar max 3 velas
BKOUT_LEVELS      = {'P4HH'}  # solo P4HH tiene edge real (v4: 80% WR, 5 setups)

# MR — solo LONG en niveles de soporte
MR_MIN_CROSSES    = 7         # más cruces = más lateral (antes 5)
MR_MAX_VOL        = 100       # vol más plano (antes 115%)
MR_SUPPORT_LEVELS = {'P4HL'}  # solo P4HL (PDL eliminado: WR 17% en v5, 2/12 — demasiado ruido)
MR_MA_DIRS        = {'sideways'}

INTERVAL_MAP = {'15m': 'Min15', '1h': 'Min60', '4h': 'Hour4', '1d': 'Day1'}

COINS = [
    'BTC_USDT', 'ETH_USDT', 'BNB_USDT', 'SOL_USDT', 'XRP_USDT',
    'DOGE_USDT', 'ADA_USDT', 'AVAX_USDT', 'LINK_USDT', 'DOT_USDT',
    'LTC_USDT', 'UNI_USDT', 'ATOM_USDT', 'NEAR_USDT', 'APT_USDT',
    'ARB_USDT', 'OP_USDT', 'INJ_USDT', 'SUI_USDT', 'TRX_USDT',
    'TON_USDT', 'WIF_USDT', 'JUP_USDT', 'SEI_USDT', 'PEPE_USDT',
    'BONK_USDT', 'TIA_USDT', 'PENDLE_USDT', 'FTM_USDT', 'MATIC_USDT',
]

TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')


# ══════════════════════════════════════════════════════════
#  API MEXC
# ══════════════════════════════════════════════════════════
def get_klines(symbol, interval, limit=200):
    try:
        r = requests.get(
            f'https://contract.mexc.com/api/v1/contract/kline/{symbol}',
            params={'interval': INTERVAL_MAP[interval], 'limit': limit},
            timeout=12,
        )
        d = r.json().get('data', {})
        if not d or 'close' not in d or not d['close']:
            return None
        keys = ('open', 'close', 'high', 'low', 'vol', 'amount', 'time')
        return {k: [float(x) for x in d[k]] for k in keys if k in d}
    except Exception as e:
        log.error(f'{symbol} {interval}: {e}')
        return None


# ══════════════════════════════════════════════════════════
#  ANALISIS ZCT
# ══════════════════════════════════════════════════════════
def calc_smma(closes, length=30):
    if len(closes) < length:
        return []
    sma = sum(closes[:length]) / length
    result = [sma]
    for c in closes[length:]:
        result.append((result[-1] * (length - 1) + c) / length)
    return result


def count_crosses(closes, ma, lb=50):
    n = min(lb, len(closes) - 1, len(ma) - 1)
    crosses = 0
    for i in range(1, n + 1):
        above_now  = closes[-i]     >= ma[-i]
        above_prev = closes[-(i+1)] >= ma[-(i+1)]
        if above_now != above_prev:
            crosses += 1
    return crosses


def get_ma_direction(ma, lb=5):
    if len(ma) < lb + 1:
        return 'sideways'
    pct = (ma[-1] - ma[-(lb+1)]) / ma[-(lb+1)] * 100
    if pct > MA_DIR_THR:  return 'up'
    if pct < -MA_DIR_THR: return 'down'
    return 'sideways'


def analyze_zct_window(closes, opens, amounts):
    ma = calc_smma(closes, SMMA_LEN)
    if not ma:
        return {}
    crosses   = count_crosses(closes, ma, CROSS_LB)
    direction = get_ma_direction(ma, MA_DIR_LB)
    vol_ma    = sum(amounts[-VOL_MA_LEN:]) / VOL_MA_LEN if len(amounts) >= VOL_MA_LEN else None
    vol_ratio = amounts[-1] / vol_ma * 100 if vol_ma else 100.0
    bullish_candle = closes[-1] > opens[-1] if opens else True
    return {
        'crosses': crosses,
        'direction': direction,
        'vol_ratio': vol_ratio,
        'bullish_candle': bullish_candle,
    }


# ══════════════════════════════════════════════════════════
#  NIVELES HISTORICOS
# ══════════════════════════════════════════════════════════
def compute_levels_at(candle_time, d1, d4h, d1h, d15m):
    levels = {}
    if d1 and 'time' in d1:
        cdate = datetime.utcfromtimestamp(candle_time).date()
        ph, pl = [], []
        for i, t in enumerate(d1['time']):
            if datetime.utcfromtimestamp(t).date() < cdate:
                ph.append(d1['high'][i])
                pl.append(d1['low'][i])
        if ph:
            levels['PDH'] = ph[-1]
            levels['PDL'] = pl[-1]
    if d4h and 'time' in d4h:
        prev = [(t, h, l) for t, h, l in zip(d4h['time'], d4h['high'], d4h['low']) if t < candle_time]
        if prev:
            levels['P4HH'] = prev[-1][1]
            levels['P4HL'] = prev[-1][2]
    if d1h and 'time' in d1h:
        prev = [(t, h, l) for t, h, l in zip(d1h['time'], d1h['high'], d1h['low']) if t < candle_time]
        if prev:
            levels['P1HH'] = prev[-1][1]
            levels['P1HL'] = prev[-1][2]
    if d15m and 'time' in d15m:
        prev = [(t, h, l) for t, h, l in zip(d15m['time'], d15m['high'], d15m['low']) if t < candle_time]
        if prev:
            levels['P15mH'] = prev[-1][1]
            levels['P15mL'] = prev[-1][2]
    return levels


# ══════════════════════════════════════════════════════════
#  RESULTADO DEL TRADE
# ══════════════════════════════════════════════════════════
def simulate_outcome(direction, entry_price, future_highs, future_lows):
    tp = entry_price * (1 + TP_PCT) if direction == 'LONG' else entry_price * (1 - TP_PCT)
    sl = entry_price * (1 - SL_PCT) if direction == 'LONG' else entry_price * (1 + SL_PCT)
    for high, low in zip(future_highs, future_lows):
        if direction == 'LONG':
            if high >= tp: return 'WIN'
            if low  <= sl: return 'LOSS'
        else:
            if low  <= tp: return 'WIN'
            if high >= sl: return 'LOSS'
    return 'TIMEOUT'


# ══════════════════════════════════════════════════════════
#  BACKTEST POR MONEDA
# ══════════════════════════════════════════════════════════
def backtest_coin(symbol):
    results = []

    d15m = get_klines(symbol, '15m', limit=1500)
    time.sleep(0.15)
    d1h  = get_klines(symbol, '1h',  limit=500)
    time.sleep(0.15)
    d4h  = get_klines(symbol, '4h',  limit=200)
    time.sleep(0.15)
    d1   = get_klines(symbol, '1d',  limit=200)
    time.sleep(0.2)

    min_candles = WARMUP_CANDLES + OUTCOME_CANDLES + BKOUT_MAX_WAIT + 5
    if not d15m or len(d15m.get('close', [])) < min_candles:
        n_got = len(d15m['close']) if d15m else 0
        log.warning(f'{symbol}: datos insuficientes ({n_got} velas 15m)')
        return []

    closes  = d15m['close']
    opens   = d15m.get('open', closes)
    highs   = d15m['high']
    lows    = d15m['low']
    amounts = d15m.get('amount', d15m.get('vol', [1.0] * len(closes)))
    times   = d15m.get('time', [])
    n       = len(closes)

    cd_bkout = {}
    cd_mr    = {}

    end_idx = n - OUTCOME_CANDLES - BKOUT_MAX_WAIT - 2

    for idx in range(WARMUP_CANDLES, end_idx):
        price = closes[idx]
        if price <= 0:
            continue

        zct = analyze_zct_window(closes[:idx+1], opens[:idx+1], amounts[:idx+1])
        if not zct:
            continue

        crosses        = zct['crosses']
        vol_ratio      = zct['vol_ratio']
        ma_dir         = zct['direction']
        bullish_candle = zct['bullish_candle']

        candle_time = times[idx] if idx < len(times) else 0
        levels = compute_levels_at(candle_time, d1, d4h, d1h, d15m)
        if not levels:
            continue

        ref_idx    = max(0, idx - CHANGE_LB)
        change_pct = (price - closes[ref_idx]) / closes[ref_idx] * 100 if closes[ref_idx] > 0 else 0.0

        for lvl_name, lvl_price in levels.items():
            if lvl_price <= 0:
                continue

            dist_pct_val = abs(lvl_price - price) / price * 100
            if not (MIN_DIST_PCT <= dist_pct_val <= PROXIMITY_PCT * 100):
                continue

            ts_str = datetime.utcfromtimestamp(candle_time).strftime('%m-%d %H:%M') if candle_time else '?'

            # ─── BREAKOUT LONG únicamente (solo P4HH) ────────────
            if (lvl_name in BKOUT_LEVELS
                    and crosses <= BKOUT_MAX_CROSSES
                    and vol_ratio >= BKOUT_MIN_VOL
                    and ma_dir == 'up'
                    and lvl_price > price
                    and bullish_candle):

                cd_key = (lvl_name, 'BKOUT', 'LONG')
                if cd_key not in cd_bkout or (idx - cd_bkout[cd_key]) >= 16:
                    entry_idx = None
                    for j in range(1, BKOUT_MAX_WAIT + 1):
                        if idx + j >= n:
                            break
                        if closes[idx + j] > lvl_price:
                            entry_idx = idx + j
                            break

                    if entry_idx is not None:
                        ep = closes[entry_idx]
                        fh = highs[entry_idx+1 : entry_idx+1+OUTCOME_CANDLES]
                        fl = lows [entry_idx+1 : entry_idx+1+OUTCOME_CANDLES]
                        if len(fh) >= 4:
                            out = simulate_outcome('LONG', ep, fh, fl)
                            if out != 'TIMEOUT':
                                cd_bkout[cd_key] = idx
                                results.append({
                                    'strategy':   'BREAKOUT',
                                    'symbol':     symbol,
                                    'ts':         ts_str,
                                    'direction':  'LONG',
                                    'level':      lvl_name,
                                    'crosses':    crosses,
                                    'ma_dir':     ma_dir,
                                    'vol_ratio':  round(vol_ratio, 1),
                                    'dist_pct':   round(dist_pct_val, 3),
                                    'change_pct': round(change_pct, 1),
                                    'outcome':    out,
                                })

            # ─── MR LONG en soportes únicamente ─────────────────
            if (crosses >= MR_MIN_CROSSES
                    and vol_ratio < MR_MAX_VOL
                    and ma_dir in MR_MA_DIRS
                    and lvl_name in MR_SUPPORT_LEVELS
                    and lvl_price < price):

                cd_key = (lvl_name, 'MR', 'LONG')
                if cd_key not in cd_mr or (idx - cd_mr[cd_key]) >= 16:
                    if idx + 1 < n:
                        ep = closes[idx + 1]
                        fh = highs[idx+2 : idx+2+OUTCOME_CANDLES]
                        fl = lows [idx+2 : idx+2+OUTCOME_CANDLES]
                        if len(fh) >= 4:
                            out = simulate_outcome('LONG', ep, fh, fl)
                            if out != 'TIMEOUT':
                                cd_mr[cd_key] = idx
                                results.append({
                                    'strategy':   'MR',
                                    'symbol':     symbol,
                                    'ts':         ts_str,
                                    'direction':  'LONG',
                                    'level':      lvl_name,
                                    'crosses':    crosses,
                                    'ma_dir':     ma_dir,
                                    'vol_ratio':  round(vol_ratio, 1),
                                    'dist_pct':   round(dist_pct_val, 3),
                                    'change_pct': round(change_pct, 1),
                                    'outcome':    out,
                                })

    log.info(f'{symbol}: {len(results)} setups')
    return results


# ══════════════════════════════════════════════════════════
#  ANALISIS
# ══════════════════════════════════════════════════════════
def win_rate(subset):
    if not subset: return 0.0
    return sum(1 for r in subset if r['outcome'] == 'WIN') / len(subset) * 100


def analyze_strategy(rows, label):
    if not rows:
        return {}
    total = len(rows)
    wins  = sum(1 for r in rows if r['outcome'] == 'WIN')
    wr    = wins / total * 100

    lvl_groups = {
        'PDH/PDL':  ('PDH', 'PDL'),
        'P4H H/L':  ('P4HH', 'P4HL'),
        'P1H H/L':  ('P1HH', 'P1HL'),
        'P15m H/L': ('P15mH', 'P15mL'),
    }
    by_level = {}
    for lbl, names in lvl_groups.items():
        sub = [r for r in rows if r.get('level', '') in names]
        if sub:
            w = sum(1 for r in sub if r['outcome'] == 'WIN')
            by_level[lbl] = (w, len(sub), w / len(sub) * 100)

    vol_bins = [(200, 250, '200-250%'), (250, 300, '250-300%'),
                (300, 400, '300-400%'), (400, 9999, '400%+')]
    by_vol = {}
    for lo, hi, lbl in vol_bins:
        sub = [r for r in rows if lo <= r['vol_ratio'] < hi]
        if sub:
            w = sum(1 for r in sub if r['outcome'] == 'WIN')
            by_vol[lbl] = (w, len(sub), w / len(sub) * 100)

    by_dist = {}
    dist_bins = [(0.20, 0.30, '0.2-0.3%'), (0.30, 0.40, '0.3-0.4%'), (0.40, 0.51, '0.4-0.5%')]
    for lo, hi, lbl in dist_bins:
        sub = [r for r in rows if lo <= r['dist_pct'] < hi]
        if sub:
            w = sum(1 for r in sub if r['outcome'] == 'WIN')
            by_dist[lbl] = (w, len(sub), w / len(sub) * 100)

    return {
        'label': label, 'total': total, 'wins': wins, 'wr': wr,
        'by_vol': by_vol,
        'by_dist': by_dist,
        'by_level': by_level,
    }


# ══════════════════════════════════════════════════════════
#  REPORTE TELEGRAM
# ══════════════════════════════════════════════════════════
def bar(wr):
    filled = round(wr / 10)
    return 'X' * filled + '.' * (10 - filled)


def fmt_section(title, data):
    if not data: return ''
    lines = [title]
    for label, (w, n, wr) in sorted(data.items(), key=lambda x: -x[1][2]):
        lines.append(f'  {html_mod.escape(label):<16} {bar(wr)} {wr:.0f}% ({w}/{n})')
    return '\n'.join(lines)


def fmt_strategy(a):
    if not a: return ''
    wr    = a['wr']
    emoji = 'OK' if wr >= 50 else 'MEH' if wr >= 40 else 'BAD'
    lines = [
        f'{emoji} WR {wr:.1f}%  ({a["wins"]}W/{a["total"]-a["wins"]}L / {a["total"]} total)',
        fmt_section('  Por volumen:', a.get('by_vol', {})),
        fmt_section('  Por distancia:', a.get('by_dist', {})),
        fmt_section('  Por nivel:', a.get('by_level', {})),
    ]
    return '\n'.join(l for l in lines if l)


def build_report(bkout, mr, n_coins):
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    bkout_str = fmt_strategy(bkout) if bkout else 'Sin datos'
    mr_str    = fmt_strategy(mr) if mr else 'Sin datos'
    parts = [
        f'ZCT Backtest v6 -- {n_coins} monedas * 15m * ~15 dias',
        f'SL: 1pct  TP: 2pct (2R)  |  Horizonte: {OUTCOME_CANDLES*15//60}h',
        'BREAKOUT: LONG solo P4HH + MA up + vol>=200pct + dist<=0.4pct',
        'MR: LONG solo P4HL + crosses>=7 + vol<100pct',
        '',
        '=== BREAKOUT LONG ===',
        bkout_str,
        '',
        '=== MR LONG (soporte) ===',
        mr_str,
        '',
        ts,
    ]
    return '\n'.join(p for p in parts if p is not None)


def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(msg)
        return
    try:
        r = requests.post(
            f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
            data={'chat_id': TELEGRAM_CHAT_ID, 'text': msg,
                  'disable_web_page_preview': 'true'},
            timeout=10,
        )
        if not r.ok:
            log.error(f'Telegram: {r.text}')
    except Exception as e:
        log.error(f'Telegram: {e}')
        print(msg)


# ══════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════
def main():
    log.info('=== ZCT Backtester v6 iniciando ===')
    log.info(f'SL={SL_PCT*100:.0f}%  TP={TP_PCT*100:.0f}%  RR=2:1')
    log.info(f'BREAKOUT: LONG solo, MA up, vol>={BKOUT_MIN_VOL}%, wait<={BKOUT_MAX_WAIT}v, vela alcista, dist<=0.4%')
    log.info(f'MR: LONG solo P4HL, crosses>={MR_MIN_CROSSES}, vol<{MR_MAX_VOL}%')
    all_results = []

    for i, symbol in enumerate(COINS):
        try:
            log.info(f'[{i+1}/{len(COINS)}] {symbol}')
            results = backtest_coin(symbol)
            all_results.extend(results)
        except Exception as e:
            log.error(f'{symbol}: {e}')

    log.info(f'Total setups: {len(all_results)}')

    csv_path = os.path.join(os.path.dirname(__file__), 'backtest_results.csv')
    fieldnames = ['strategy', 'symbol', 'ts', 'direction', 'level',
                  'crosses', 'ma_dir', 'vol_ratio', 'dist_pct', 'change_pct', 'outcome']
    if all_results:
        with open(csv_path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(all_results)
        log.info(f'CSV guardado: {csv_path}')

    bkout_rows = [r for r in all_results if r['strategy'] == 'BREAKOUT']
    mr_rows    = [r for r in all_results if r['strategy'] == 'MR']

    bkout_analysis = analyze_strategy(bkout_rows, 'BREAKOUT')
    mr_analysis    = analyze_strategy(mr_rows,    'MR')

    report = build_report(bkout_analysis, mr_analysis, len(COINS))
    log.info('Enviando reporte...')
    send_telegram(report)
    log.info('=== Backtest v6 completado ===')


if __name__ == '__main__':
    main()
