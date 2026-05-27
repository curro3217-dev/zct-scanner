#!/usr/bin/env python3
"""
ZCT Backtester v8 — Test de CHANGE_THRESH 7% vs 10%

  BREAKOUT  : LONG en PDH, P4HH, P1HH, P15mH. MA up, vol >= 120%, espera max 3 velas.
              Requiere vela de entrada alcista (close > open). Distancia max 0.4%.
              v8: añadido filtro CHANGE_THRESH para comparar 7% vs 10%.

  MR descartado: WR 19.2% con RR 2:1 — necesita >33% para breakeven. No tiene edge.

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
PROXIMITY_PCT   = 0.004       # 0.4% de distancia al nivel
MIN_DIST_PCT    = 0.20        # distancia minima al nivel (%)
SL_PCT          = 0.01        # Stop Loss 1%
TP_PCT          = SL_PCT * 2  # Take Profit 2% (RR 2:1)
SMMA_LEN        = 30
VOL_MA_LEN      = 20
CROSS_LB        = 50
MA_DIR_LB       = 5
MA_DIR_THR      = 0.08

OUTCOME_CANDLES = 32          # 8h de ventana para que el trade resuelva
WARMUP_CANDLES  = SMMA_LEN + CROSS_LB + MA_DIR_LB + 5
CHANGE_LB       = 96          # 96 velas × 15m = 24h de lookback para el cambio

# Filtro de mover: solo senales cuando la moneda lleva X% de cambio en 24h
# Actualmente en el scanner es 10%. Testeamos 7% para ver si hay mas senales sin perder WR.
CHANGE_THRESH   = 7.0         # % minimo de cambio 24h para considerar la senal

# BREAKOUT — solo LONG con momentum real
BKOUT_MAX_CROSSES = 1
BKOUT_MIN_VOL     = 120       # vol minimo 120% (bajado de 200% — scanner llega tarde al spike)
BKOUT_MAX_WAIT    = 3         # esperar max 3 velas
BKOUT_LEVELS      = {'PDH', 'P4HH', 'P1HH', 'P15mH'}  # todos los highs (mas muestra)

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

        # Filtro mover: solo operar cuando la moneda lleva >= CHANGE_THRESH% en 24h
        if abs(change_pct) < CHANGE_THRESH:
            continue

        for lvl_name, lvl_price in levels.items():
            if lvl_price <= 0:
                continue

            dist_pct_val = abs(lvl_price - price) / price * 100
            if not (MIN_DIST_PCT <= dist_pct_val <= PROXIMITY_PCT * 100):
                continue

            ts_str = datetime.utcfromtimestamp(candle_time).strftime('%m-%d %H:%M') if candle_time else '?'

            # BREAKOUT LONG
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
        'PDH':      ('PDH',),
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

    vol_bins = [(120, 150, '120-150%'), (150, 200, '150-200%'),
                (200, 300, '200-300%'), (300, 9999, '300%+')]
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

    # Desglose por rango de cambio 24h
    change_bins = [(7, 10, '7-10%'), (10, 15, '10-15%'), (15, 25, '15-25%'), (25, 999, '25%+')]
    by_change = {}
    for lo, hi, lbl in change_bins:
        sub = [r for r in rows if lo <= abs(r['change_pct']) < hi]
        if sub:
            w = sum(1 for r in sub if r['outcome'] == 'WIN')
            by_change[lbl] = (w, len(sub), w / len(sub) * 100)

    return {
        'label': label, 'total': total, 'wins': wins, 'wr': wr,
        'by_vol': by_vol,
        'by_dist': by_dist,
        'by_level': by_level,
        'by_change': by_change,
    }


# ══════════════════════════════════════════════════════════
#  REPORTE TELEGRAM
# ══════════════════════════════════════════════════════════
def fmt_vol_section(by_vol):
    if not by_vol:
        return ''
    lines = ['Por volumen en el momento de la senal:']
    for lbl, (w, n, wr) in sorted(by_vol.items(), key=lambda x: -x[1][2]):
        lines.append(f'  {lbl}: {wr:.0f}% de aciertos ({w} ganadas / {n} total)')
    return '\n'.join(lines)


def fmt_level_section(by_level):
    if not by_level:
        return ''
    level_names = {
        'PDH':      'Maximo del dia anterior',
        'P4H H/L':  'Maximo de la ultima vela de 4h',
        'P1H H/L':  'Maximo de la ultima vela de 1h',
        'P15m H/L': 'Maximo de la ultima vela de 15m',
    }
    lines = ['Por nivel de precio usado:']
    for lbl, (w, n, wr) in sorted(by_level.items(), key=lambda x: -x[1][2]):
        nombre = level_names.get(lbl, lbl)
        lines.append(f'  {nombre}: {wr:.0f}% de aciertos ({w} ganadas / {n} total)')
    return '\n'.join(lines)


def build_report(bkout, n_coins):
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

    if bkout:
        total = bkout['total']
        wins  = bkout['wins']
        wr    = bkout['wr']

        if total < 20:
            conclusion = 'AVISO: Solo ' + str(total) + ' operaciones — datos insuficientes para concluir nada'
        elif wr >= 50:
            conclusion = 'RENTABLE: ' + str(round(wr)) + '% de aciertos con ' + str(total) + ' operaciones'
        elif wr >= 33:
            conclusion = 'EN EL LIMITE: ' + str(round(wr)) + '% de aciertos con ' + str(total) + ' operaciones'
        else:
            conclusion = 'NO RENTABLE: solo ' + str(round(wr)) + '% de aciertos con ' + str(total) + ' operaciones'

        bkout_lines = [
            'Aciertos: ' + str(round(wr)) + '% — ' + str(wins) + ' ganadas / ' + str(total - wins) + ' perdidas / ' + str(total) + ' operaciones',
            conclusion,
            '',
            fmt_vol_section(bkout.get('by_vol', {})),
            '',
            fmt_level_section(bkout.get('by_level', {})),
        ]
        bkout_str = '\n'.join(l for l in bkout_lines if l is not None)
    else:
        bkout_str = 'Sin datos'

    # Desglose por cambio 24h
    by_change = bkout.get('by_change', {}) if bkout else {}
    change_lines = []
    if by_change:
        change_lines = ['', 'Por fuerza del movimiento 24h en el momento de la senal:']
        for lbl, (w, n, wr) in sorted(by_change.items(), key=lambda x: x[0]):
            change_lines.append(f'  {lbl}: {wr:.0f}% de aciertos ({w} ganadas / {n} total)')

    parts = [
        '📊 ZCT Backtest v8 — Filtro cambio 24h >= ' + str(CHANGE_THRESH) + '%',
        'Analizadas ' + str(n_coins) + ' monedas durante los ultimos 15 dias',
        'Stop Loss 1% · Take Profit 2%',
        'Para ganar dinero necesitas acertar mas del 33% de las veces',
        '',
        'ESTRATEGIA BREAKOUT (operar el impulso)',
        bkout_str,
        '\n'.join(change_lines),
        '',
        'ESTRATEGIA CONTRATENDENCIA',
        'Aciertos: 19% — 73 operaciones — ABANDONADA',
        'No llega al minimo del 33% para ser rentable',
        '',
        ts,
    ]
    return '\n'.join(parts)


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
    log.info('=== ZCT Backtester v8 iniciando ===')
    log.info(f'SL={SL_PCT*100:.0f}%  TP={TP_PCT*100:.0f}%  RR=2:1  CHANGE_THRESH={CHANGE_THRESH}%')
    log.info(f'BREAKOUT: LONG, niveles={BKOUT_LEVELS}, MA up, vol>={BKOUT_MIN_VOL}%, wait<={BKOUT_MAX_WAIT}v, dist<=0.4%')
    log.info('MR: descartado (WR 19% < 33% breakeven con RR 2:1)')
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
    bkout_analysis = analyze_strategy(bkout_rows, 'BREAKOUT')

    report = build_report(bkout_analysis, len(COINS))
    log.info('Enviando reporte...')
    send_telegram(report)
    log.info('=== Backtest v7 completado ===')


if __name__ == '__main__':
    main()
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
    bkout_analysis = analyze_strategy(bkout_rows, 'BREAKOUT')

    report = build_report(bkout_analysis, len(COINS))
    log.info('Enviando reporte...')
    send_telegram(report)
    log.info('=== Backtest v7 completado ===')


if __name__ == '__main__':
    main()
