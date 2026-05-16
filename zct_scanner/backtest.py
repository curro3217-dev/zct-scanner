#!/usr/bin/env python3
"""
ZCT Backtester â Analiza el rendimiento histÃ³rico del scanner.

Simula las seÃ±ales del scanner en las Ãºltimas velas disponibles de MEXC
y calcula quÃ© filtros realmente funcionan (win rate por feature).
EnvÃ­a el anÃ¡lisis completo por Telegram.

Uso: python backtest.py
Requiere: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID (env vars)
"""

import os, time, csv, logging
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
#  CONFIG (igual que el scanner)
# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
PROXIMITY_PCT = 0.005    # 0.5% de proximidad al nivel
MAX_DIST_PCT  = 15.0     # nivel ignorado si estÃ¡ a >15% del precio
CLUSTER_TOP   = 2.0      # distancia mÃ¡xima entre 2 niveles (top coins)
CLUSTER_ALT   = 3.0      # distancia mÃ¡xima entre 2 niveles (altcoins)
SL_PCT        = 0.02     # 2% stop loss
TP_PCT        = SL_PCT * 3.0  # 6% = 3R
SMMA_LEN      = 30
VOL_MA_LEN    = 20
CROSS_LB      = 50
MA_DIR_LB     = 5
MA_DIR_THR    = 0.08
STRONG_PUMP   = 30.0

# Timeframe de simulaciÃ³n y ventana de resultado
SIM_INTERVAL   = '15m'   # vela base del backtest
OUTCOME_CANDLES = 24     # velas hacia adelante para el resultado (24 * 15m = 6h)
WARMUP_CANDLES  = SMMA_LEN + CROSS_LB + MA_DIR_LB + 5  # ~85 velas de calentamiento
CHANGE_LB       = 96     # velas atrÃ¡s para calcular el cambio 24h (96 * 15m = 24h)

TOP_COINS = {'BTC_USDT', 'ETH_USDT', 'BNB_USDT', 'SOL_USDT', 'XRP_USDT'}

INTERVAL_MAP = {
    '15m': 'Min15', '1h': 'Min60', '4h': 'Hour4', '1d': 'Day1',
}

# 30 monedas mÃ¡s lÃ­quidas en MEXC futuros
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


# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
#  API MEXC
# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
def get_klines(symbol: str, interval: str, limit: int = 200) -> dict | None:
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


# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
#  ANÃLISIS ZCT (mismo cÃ³digo que el scanner)
# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
def calc_smma(closes: list, length: int = 30) -> list:
    if len(closes) < length:
        return []
    sma = sum(closes[:length]) / length
    result = [sma]
    for c in closes[length:]:
        result.append((result[-1] * (length - 1) + c) / length)
    return result


def count_crosses(closes: list, ma: list, lb: int = 50) -> int:
    n = min(lb, len(closes) - 1, len(ma) - 1)
    crosses = 0
    for i in range(1, n + 1):
        above_now  = closes[-i]     >= ma[-i]
        above_prev = closes[-(i+1)] >= ma[-(i+1)]
        if above_now != above_prev:
            crosses += 1
    return crosses


def get_ma_direction(ma: list, lb: int = 5) -> str:
    if len(ma) < lb + 1:
        return 'sideways'
    pct = (ma[-1] - ma[-(lb+1)]) / ma[-(lb+1)] * 100
    if pct > MA_DIR_THR:
        return 'up'
    if pct < -MA_DIR_THR:
        return 'down'
    return 'sideways'


def analyze_zct_window(closes: list, amounts: list) -> dict:
    """Analiza ZCT sobre una ventana de velas."""
    ma = calc_smma(closes, SMMA_LEN)
    if not ma:
        return {}
    crosses   = count_crosses(closes, ma, CROSS_LB)
    direction = get_ma_direction(ma, MA_DIR_LB)
    vol_ma    = sum(amounts[-VOL_MA_LEN:]) / VOL_MA_LEN if len(amounts) >= VOL_MA_LEN else None
    vol_ratio = amounts[-1] / vol_ma * 100 if vol_ma else 100.0
    return {'crosses': crosses, 'direction': direction, 'vol_ratio': vol_ratio}


def find_level_cluster(levels: dict, price: float,
                       direction: str, symbol: str,
                       change_pct: float = 0.0) -> dict | None:
    cluster_pct = CLUSTER_TOP if symbol in TOP_COINS else CLUSTER_ALT
    candidates  = []
    for name, lvl in levels.items():
        dist = abs(lvl - price) / price * 100
        if dist > MAX_DIST_PCT:
            continue
        if direction == 'LONG' and lvl > price:
            candidates.append((name, lvl))
        elif direction == 'SHORT' and lvl < price:
            candidates.append((name, lvl))
    if not candidates:
        return None
    candidates.sort(key=lambda x: abs(x[1] - price))
    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            n1, l1 = candidates[i]
            n2, l2 = candidates[j]
            gap = abs(l1 - l2) / min(l1, l2) * 100
            if gap <= cluster_pct:
                return {'lvl1_name': n1, 'lvl1': l1,
                        'lvl2_name': n2, 'lvl2': l2,
                        'gap_pct':   gap, 'single_level': False}
    if abs(change_pct) >= STRONG_PUMP:
        n1, l1 = candidates[0]
        return {'lvl1_name': n1, 'lvl1': l1,
                'lvl2_name': None, 'lvl2': None,
                'gap_pct': 0.0, 'single_level': True}
    return None


# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
#  NIVELES HISTÃRICOS
# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
def compute_levels_at(candle_time: float,
                      d1: dict, d4h: dict,
                      d1h: dict, d15m: dict) -> dict:
    """Calcula los niveles ZCT (PDH/PDL, P4H, P1H, P15m) en el momento T."""
    levels = {}

    # PDH/PDL: dÃ­a anterior
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

    # P4HH/P4HL
    if d4h and 'time' in d4h:
        prev = [(t, h, l) for t, h, l
                in zip(d4h['time'], d4h['high'], d4h['low'])
                if t < candle_time]
        if prev:
            levels['P4HH'] = prev[-1][1]
            levels['P4HL'] = prev[-1][2]

    # P1HH/P1HL
    if d1h and 'time' in d1h:
        prev = [(t, h, l) for t, h, l
                in zip(d1h['time'], d1h['high'], d1h['low'])
                if t < candle_time]
        if prev:
            levels['P1HH'] = prev[-1][1]
            levels['P1HL'] = prev[-1][2]

    # P15mH/P15mL
    if d15m and 'time' in d15m:
        prev = [(t, h, l) for t, h, l
                in zip(d15m['time'], d15m['high'], d15m['low'])
                if t < candle_time]
        if prev:
            levels['P15mH'] = prev[-1][1]
            levels['P15mL'] = prev[-1][2]

    return levels


# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
#  RESULTADO DEL TRADE
# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
def simulate_outcome(direction: str, entry_price: float,
                     future_highs: list, future_lows: list) -> str:
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


# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
#  BACKTEST POR MONEDA
# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
def backtest_coin(symbol: str) -> list:
    results = []

    d15m = get_klines(symbol, '15m', limit=250)
    time.sleep(0.15)
    d1h  = get_klines(symbol, '1h',  limit=200)
    time.sleep(0.15)
    d4h  = get_klines(symbol, '4h',  limit=200)
    time.sleep(0.15)
    d1   = get_klines(symbol, '1d',  limit=200)
    time.sleep(0.2)

    if not d15m or len(d15m.get('close', [])) < WARMUP_CANDLES + OUTCOME_CANDLES + CHANGE_LB:
        log.warning(f'{symbol}: datos insuficientes ({len(d15m["close"]) if d15m else 0} velas 15m)')
        return []

    closes  = d15m['close']
    highs   = d15m['high']
    lows    = d15m['low']
    amounts = d15m.get('amount', d15m.get('vol', [1.0] * len(closes)))
    times   = d15m.get('time', [])
    n       = len(closes)

    start = max(WARMUP_CANDLES, CHANGE_LB)

    for idx in range(start, n - OUTCOME_CANDLES):
        price = closes[idx]
        if price <= 0:
            continue

        # Cambio en las Ãºltimas 24h (96 velas de 15m)
        ref_idx    = max(0, idx - CHANGE_LB)
        ref_price  = closes[ref_idx]
        change_pct = (price - ref_price) / ref_price * 100 if ref_price > 0 else 0.0

        # Determinar direcciÃ³n (testamos ambas si no hay movimiento fuerte)
        directions = []
        if change_pct >= 5.0:    # al menos +5% en 24h para LONG
            directions.append(('LONG', change_pct))
        if change_pct <= -5.0:   # al menos -5% para SHORT
            directions.append(('SHORT', change_pct))
        if not directions:
            continue

        # ZCT anÃ¡lisis sobre la ventana hasta idx
        win_closes  = closes[:idx+1]
        win_amounts = amounts[:idx+1]
        zct = analyze_zct_window(win_closes, win_amounts)
        if not zct:
            continue

        # Niveles histÃ³ricos en este punto
        candle_time = times[idx] if times else 0
        levels = compute_levels_at(candle_time, d1, d4h, d1h, d15m)
        if len(levels) < 2:
            continue

        for direction, cpct in directions:
            # Filtro 1: MA direction compatible
            ma_dir = zct['direction']
            if direction == 'LONG' and ma_dir == 'down':
                continue
            if direction == 'SHORT' and ma_dir == 'up':
                continue

            # Filtro 2: mÃ¡ximo 6 cruces
            crosses = zct['crosses']
            if crosses > 6:
                continue

            # Cluster de 2 niveles
            cluster = find_level_cluster(levels, price, direction, symbol, cpct)
            if not cluster:
                continue

            # Proximidad
            near_lvl = cluster['lvl1']
            if cluster['lvl2'] is not None:
                if abs(cluster['lvl2'] - price) < abs(near_lvl - price):
                    near_lvl = cluster['lvl2']
            dist = abs(near_lvl - price) / near_lvl
            if dist > PROXIMITY_PCT:
                continue

            # Â¡CondiciÃ³n de alerta cumplida! â calcular resultado
            future_highs = highs[idx+1 : idx+1+OUTCOME_CANDLES]
            future_lows  = lows [idx+1 : idx+1+OUTCOME_CANDLES]
            outcome = simulate_outcome(direction, price, future_highs, future_lows)
            if outcome == 'TIMEOUT':
                continue

            results.append({
                'symbol':      symbol,
                'ts':          datetime.utcfromtimestamp(candle_time).strftime('%m-%d %H:%M') if candle_time else '?',
                'direction':   direction,
                'change_pct':  round(cpct, 1),
                'crosses':     crosses,
                'ma_dir':      ma_dir,
                'vol_ratio':   round(zct['vol_ratio'], 1),
                'gap_pct':     round(cluster['gap_pct'], 2),
                'single_lvl':  int(cluster['single_level']),
                'dist_pct':    round(dist * 100, 3),
                'outcome':     outcome,
            })

    log.info(f'{symbol}: {len(results)} setups')
    return results


# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
#  ANÃLISIS DE RESULTADOS
# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
def win_rate(subset: list) -> float:
    if not subset:
        return 0.0
    return sum(1 for r in subset if r['outcome'] == 'WIN') / len(subset) * 100


def analyze(results: list) -> dict:
    if not results:
        return {}

    total = len(results)
    wins  = sum(1 for r in results if r['outcome'] == 'WIN')
    wr    = wins / total * 100

    def breakdown(key, bins):
        out = {}
        for lo, hi, label in bins:
            sub = [r for r in results if lo <= r[key] < hi]
            if sub:
                w = sum(1 for r in sub if r['outcome'] == 'WIN')
                out[label] = (w, len(sub), w / len(sub) * 100)
        return out

    return {
        'total': total, 'wins': wins, 'wr': wr,
        'by_crosses': breakdown('crosses', [
            (0, 2, '0-1 cruces'), (2, 4, '2-3 cruces'),
            (4, 6, '4-5 cruces'), (6, 99, '6+ cruces'),
        ]),
        'by_ma': {d: (
            sum(1 for r in results if r['outcome'] == 'WIN' and r['ma_dir'] == d),
            sum(1 for r in results if r['ma_dir'] == d),
            win_rate([r for r in results if r['ma_dir'] == d]),
        ) for d in ('up', 'down', 'sideways')
            if any(r['ma_dir'] == d for r in results)},
        'by_vol': breakdown('vol_ratio', [
            (0, 85, 'Vol <85%'), (85, 115, 'Vol 85-115%'),
            (115, 150, 'Vol 115-150%'), (150, 9999, 'Vol >150%'),
        ]),
        'by_change': breakdown('change_pct', [
            (-100, -20, 'Dump >20%'), (-20, -5, 'Dump 5-20%'),
            (5, 20, 'Pump 5-20%'), (20, 100, 'Pump >20%'),
        ]),
        'by_dist': breakdown('dist_pct', [
            (0, 0.2, 'Dist <0.2%'), (0.2, 0.35, 'Dist 0.2-0.35%'),
            (0.35, 0.5, 'Dist 0.35-0.5%'),
        ]),
    }


# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
#  REPORTE TELEGRAM
# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
def bar(wr: float) -> str:
    filled = round(wr / 10)
    return 'â' * filled + 'â' * (10 - filled)


def section(title: str, data: dict) -> str:
    if not data:
        return ''
    lines = [f'<b>{title}</b>']
    for label, (w, n, wr) in sorted(data.items(), key=lambda x: -x[1][2]):
        lines.append(f'  {label:<18} {bar(wr)} {wr:.0f}% ({w}/{n})')
    return '\n'.join(lines)


def build_report(a: dict, n_coins: int) -> str:
    if not a:
        return 'â Sin datos suficientes.'

    wr    = a['wr']
    total = a['total']
    wins  = a['wins']
    emoji = 'ð¢' if wr >= 55 else 'ð¡' if wr >= 45 else 'ð´'

    # Recomendaciones automÃ¡ticas
    recs = []

    # Â¿QuÃ© nÃºmero de cruces funciona mejor?
    best_cross = max(a['by_crosses'].items(), key=lambda x: x[1][2], default=None)
    worst_cross = min(a['by_crosses'].items(), key=lambda x: x[1][2], default=None)
    if best_cross and best_cross[1][2] > wr + 5:
        recs.append(f'â¢ Mejor WR con {best_cross[0]} ({best_cross[1][2]:.0f}%) '
                    f'â considera bajar el umbral de cruces')
    if worst_cross and worst_cross[1][2] < wr - 10:
        recs.append(f'â¢ Evitar setups con {worst_cross[0]} ({worst_cross[1][2]:.0f}%)')

    # Â¿QuÃ© distancia al nivel funciona mejor?
    best_dist = max(a['by_dist'].items(), key=lambda x: x[1][2], default=None)
    if best_dist and best_dist[1][2] > wr + 5:
        recs.append(f'â¢ Mejor WR cuando {best_dist[0]} ({best_dist[1][2]:.0f}%) '
                    f'â considera afinar la proximidad')

    # Â¿Volumen importa?
    best_vol = max(a['by_vol'].items(), key=lambda x: x[1][2], default=None)
    if best_vol and best_vol[1][2] > wr + 5:
        recs.append(f'â¢ {best_vol[0]} tiene mejor WR ({best_vol[1][2]:.0f}%)')

    parts = [
        f'ð <b>ZCT Backtest â {n_coins} monedas Â· 15m Â· 6h horizonte</b>',
        f'',
        f'{emoji} Win rate: <b>{wr:.1f}%</b>  ({wins} win / {total-wins} loss / {total} total)',
        f'  SL: 2%   TP: 6% (3R)',
        f'',
        section('ð Por cruces de MA:', a['by_crosses']),
        f'',
        section('ð Por direcciÃ³n MA:', a['by_ma']),
        f'',
        section('ð Por volumen relativo:', a['by_vol']),
        f'',
        section('ð Por fuerza del movimiento:', a['by_change']),
        f'',
        section('ð Por distancia al nivel:', a['by_dist']),
    ]

    if recs:
        parts += ['', '<b>ð¡ Insights automÃ¡ticos:</b>'] + recs

    parts += [
        f'',
        f'â° {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}',
    ]

    return '\n'.join(p for p in parts if p is not None)


def send_telegram(msg: str):
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


# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
#  MAIN
# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
def main():
    log.info('=== ZCT Backtester iniciando ===')
    all_results = []

    for i, symbol in enumerate(COINS):
        try:
            log.info(f'[{i+1}/{len(COINS)}] {symbol}')
            results = backtest_coin(symbol)
            all_results.extend(results)
        except Exception as e:
            log.error(f'{symbol}: {e}')

    log.info(f'Total setups: {len(all_results)}')

    # Guardar CSV
    csv_path = os.path.join(os.path.dirname(__file__), 'backtest_results.csv')
    if all_results:
        with open(csv_path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=all_results[0].keys())
            writer.writeheader()
            writer.writerows(all_results)
        log.info(f'CSV guardado: {csv_path}')

    # Analisis y Telegram
    analysis = analyze(all_results)
    report   = build_report(analysis, len(COINS))
    log.info('Enviando reporte...')
    send_telegram(report)
    log.info('=== Backtest completado ===')


if __name__ == '__main__':
    main()
