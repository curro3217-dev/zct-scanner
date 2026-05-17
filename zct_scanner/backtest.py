#!/usr/bin/env python3
"""
ZCT Backtester v3 — Testea dos estrategias en paralelo:

  BREAKOUT  : nivel como punto de ruptura. Entrada DESPUES de que el precio
              cierre al otro lado del nivel (simula confirmacion real).
              Filtros: crosses <= 1, vol spike >150%, MA compatible.

  MR        : nivel como punto de rechazo (mean reversion). Entrada en la
              aproximacion, direccion INVERTIDA (resistencia -> SHORT,
              soporte -> LONG).
              Filtros: crosses >= 5, vol plano <115%, MA lateral.

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
PROXIMITY_PCT   = 0.005
MIN_DIST_PCT    = 0.20
SL_PCT          = 0.02
TP_PCT          = SL_PCT * 3.0
SMMA_LEN        = 30
VOL_MA_LEN      = 20
CROSS_LB        = 50
MA_DIR_LB       = 5
MA_DIR_THR      = 0.08

OUTCOME_CANDLES = 32
WARMUP_CANDLES  = SMMA_LEN + CROSS_LB + MA_DIR_LB + 5
CHANGE_LB       = 96

BKOUT_MAX_CROSSES = 1
BKOUT_MIN_VOL     = 150
BKOUT_MAX_WAIT    = 8
MR_MIN_CROSSES    = 5
MR_MAX_VOL        = 115
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


def analyze_zct_window(closes, amounts):
    ma = calc_smma(closes, SMMA_LEN)
    if not ma:
        return {}
    crosses   = count_crosses(closes, ma, CROSS_LB)
    direction = get_ma_direction(ma, MA_DIR_LB)
    vol_ma    = sum(amounts[-VOL_MA_LEN:]) / VOL_MA_LEN if len(amounts) >= VOL_MA_LEN else None
    vol_ratio = amounts[-1] / vol_ma * 100 if vol_ma else 100.0
    return {'crosses': crosses, 'direction': direction, 'vol_ratio': vol_ratio}


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

        zct = analyze_zct_window(closes[:idx+1], amounts[:idx+1])
        if not zct:
            continue

        crosses   = zct['crosses']
        vol_ratio = zct['vol_ratio']
        ma_dir    = zct['direction']

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

            # ─── BREAKOUT ───────────────────────────────────────
            if crosses <= BKOUT_MAX_CROSSES and vol_ratio >= BKOUT_MIN_VOL:
                if lvl_price > price:
                    bk_dir = 'LONG'
                    ma_ok  = (ma_dir != 'down')
                else:
                    bk_dir = 'SHORT'
                    ma_ok  = (ma_dir != 'up')

                if ma_ok:
                    cd_key = (lvl_name, 'BKOUT', bk_dir)
                    if cd_key not in cd_bkout or (idx - cd_bkout[cd_key]) >= 16:
                        entry_idx = None
                        for j in range(1, BKOUT_MAX_WAIT + 1):
                            if idx + j >= n:
                                break
                            if bk_dir == 'LONG' and closes[idx + j] > lvl_price:
                                entry_idx = idx + j
                                break
                            if bk_dir == 'SHORT' and closes[idx + j] < lvl_price:
                                entry_idx = idx + j
                                break

                        if entry_idx is not None:
                            ep = closes[entry_idx]
                            fh = highs[entry_idx+1 : entry_idx+1+OUTCOME_CANDLES]
                            fl = lows [entry_idx+1 : entry_idx+1+OUTCOME_CANDLES]
                            if len(fh) >= 4:
                                out = simulate_outcome(bk_dir, ep, fh, fl)
                                if out != 'TIMEOUT':
                                    cd_bkout[cd_key] = idx
                                    results.append({
                                        'strategy':   'BREAKOUT',
                                        'symbol':     symbol,
                                        'ts':         ts_str,
                                        'direction':  bk_dir,
                                        'level':      lvl_name,
                                        'crosses':    crosses,
                                        'ma_dir':     ma_dir,
                                        'vol_ratio':  round(vol_ratio, 1),
                                        'dist_pct':   round(dist_pct_val, 3),
                                        'change_pct': round(change_pct, 1),
                                        'outcome':    out,
                                    })

            # ─── MEAN REVERSION ─────────────────────────────────
            if crosses >= MR_MIN_CROSSES and vol_ratio < MR_MAX_VOL and ma_dir in MR_MA_DIRS:
                mr_dir = 'SHORT' if lvl_price > price else 'LONG'

                cd_key = (lvl_name, 'MR', mr_dir)
                if cd_key not in cd_mr or (idx - cd_mr[cd_key]) >= 16:
                    if idx + 1 < n:
                        ep = closes[idx + 1]
                        fh = highs[idx+2 : idx+2+OUTCOME_CANDLES]
                        fl = lows [idx+2 : idx+2+OUTCOME_CANDLES]
                        if len(fh) >= 4:
                            out = simulate_outcome(mr_dir, ep, fh, fl)
                            if out != 'TIMEOUT':
                                cd_mr[cd_key] = idx
                                results.append({
                                    'strategy':   'MR',
                                    'symbol':     symbol,
                                    'ts':         ts_str,
                                    'direction':  mr_dir,
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

    def brk(key, bins):
        out = {}
        for lo, hi, lbl in bins:
            sub = [r for r in rows if lo <= r[key] < hi]
            if sub:
                w = sum(1 for r in sub if r['outcome'] == 'WIN')
                out[lbl] = (w, len(sub), w / len(sub) * 100)
        return out

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

    by_dir = {}
    for d in ('LONG', 'SHORT'):
        sub = [r for r in rows if r['direction'] == d]
        if sub:
            w = sum(1 for r in sub if r['outcome'] == 'WIN')
            by_dir[d] = (w, len(sub), w / len(sub) * 100)

    return {
        'label': label, 'total': total, 'wins': wins, 'wr': wr,
        'by_ma': {d: (
            sum(1 for r in rows if r['outcome'] == 'WIN' and r['ma_dir'] == d),
            sum(1 for r in rows if r['ma_dir'] == d),
            win_rate([r for r in rows if r['ma_dir'] == d]),
        ) for d in ('up', 'down', 'sideways') if any(r['ma_dir'] == d for r in rows)},
        'by_dist': brk('dist_pct', [
            (0.20, 0.30, '0.2-0.3%'), (0.30, 0.40, '0.3-0.4%'), (0.40, 0.51, '0.4-0.5%'),
        ]),
        'by_level': by_level,
        'by_dir':   by_dir,
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
    emoji = 'OK' if wr >= 55 else 'MEH' if wr >= 45 else 'BAD'
    lines = [
        f'{emoji} WR {wr:.1f}%  ({a["wins"]}W/{a["total"]-a["wins"]}L / {a["total"]} total)',
        fmt_section('  Por direccion:', a['by_dir']),
        fmt_section('  Por MA:', a['by_ma']),
        fmt_section('  Por distancia:', a['by_dist']),
        fmt_section('  Por nivel:', a['by_level']),
    ]
    return '\n'.join(l for l in lines if l)


def build_report(bkout, mr, n_coins):
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    bkout_str = fmt_strategy(bkout) if bkout else 'Sin datos'
    mr_str    = fmt_strategy(mr) if mr else 'Sin datos'
    parts = [
        f'ZCT Backtest v3 -- {n_coins} monedas * 15m * ~15 dias',
        f'SL: 2%  TP: 6% (3R)  |  Horizonte: {OUTCOME_CANDLES*15//60}h',
        '',
        '=== BREAKOUT (entrada tras cruzar nivel) ===',
        bkout_str,
        '',
        '=== MEAN REVERSION (rechazo en nivel) ===',
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
                  'parse_mode': 'HTML', 'disable_web_page_preview': 'true'},
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
    log.info('=== ZCT Backtester v3 iniciando ===')
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
    log.info('=== Backtest v3 completado ===')


if __name__ == '__main__':
    main()
