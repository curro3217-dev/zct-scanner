#!/usr/bin/env python3
"""
ZCT Backtester — Analiza el rendimiento histórico del scanner.

Simula las señales del scanner en las últimas velas disponibles de MEXC
y calcula qué filtros realmente funcionan (win rate por feature).
Envía el análisis completo por Telegram.

Uso: python backtest.py
Requiere: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID (env vars)
"""

import os, time, csv, logging, html as html_mod
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# ══════════════════════════════════════════════════════════
#  CONFIG (igual que el scanner)
# ══════════════════════════════════════════════════════════
PROXIMITY_PCT = 0.005    # 0.5% de proximidad al nivel
MAX_DIST_PCT  = 15.0     # nivel ignorado si está a >15% del precio
CLUSTER_TOP   = 2.0      # distancia máxima entre 2 niveles (top coins)
CLUSTER_ALT   = 3.0      # distancia máxima entre 2 niveles (altcoins)
SL_PCT        = 0.02     # 2% stop loss
TP_PCT        = SL_PCT * 3.0  # 6% = 3R
SMMA_LEN      = 30
VOL_MA_LEN    = 20
CROSS_LB      = 50
MA_DIR_LB     = 5
MA_DIR_THR    = 0.08
STRONG_PUMP   = 30.0

# Timeframe de simulación y ventana de resultado
SIM_INTERVAL   = '15m'   # vela base del backtest
OUTCOME_CANDLES = 24     # velas hacia adelante para el resultado (24 * 15m = 6h)
WARMUP_CANDLES  = SMMA_LEN + CROSS_LB + MA_DIR_LB + 5  # ~85 velas de calentamiento
CHANGE_LB       = 96     # velas atrás para calcular el cambio 24h (96 * 15m = 24h)

TOP_COINS = {'BTC_USDT', 'ETH_USDT', 'BNB_USDT', 'SOL_USDT', 'XRP_USDT'}

INTERVAL_MAP = {
    '15m': 'Min15', '1h': 'Min60', '4h': 'Hour4', '1d': 'Day1',
}

# 30 monedas más líquidas en MEXC futuros
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


# ══════════════════════════════════════════════════════════
#  ANÁLISIS ZCT (mismo código que el scanner)
# ══════════════════════════════════════════════════════════
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


# ══════════════════════════════════════════════════════════
#  NIVELES HISTÓRICOS
# ══════════════════════════════════════════════════════════
def compute_levels_at(candle_time: float,
                      d1: dict, d4h: dict,
                      d1h: dict, d15m: dict) -> dict:
    """Calcula los niveles ZCT (PDH/PDL, P4H, P1H, P15m) en el momento T."""
    levels = {}

    # PDH/PDL: dia anterior
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


# ══════════════════════════════════════════════════════════
#  RESULTADO DEL TRADE
# ══════════════════════════════════════════════════════════
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


# ══════════════════════════════════════════════════════════
#  BACKTEST POR MONEDA
# ══════════════════════════════════════════════════════════
def backtest_coin(symbol: str) -> list:
    """
    Logica v2: el setup lo determina la PROXIMIDAD al nivel, no el movimiento pasado.

    Para cada vela idx:
      1. Comprueba si el precio esta a [0.2%, 0.5%] de cualquier nivel S/R.
      2. Direccion = posicion del nivel (encima -> LONG, debajo -> SHORT).
      3. Filtros ZCT: crosses <= 1, vol_ratio > 150%, ma_dir compatible.
      4. Entrada simulada en la SIGUIENTE vela (como haria el trader al recibir la alerta).
      5. Cooldown de 4h por nivel para evitar setups duplicados.
    """
    results = []

    d15m = get_klines(symbol, '15m', limit=500)
    time.sleep(0.15)
    d1h  = get_klines(symbol, '1h',  limit=200)
    time.sleep(0.15)
    d4h  = get_klines(symbol, '4h',  limit=200)
    time.sleep(0.15)
    d1   = get_klines(symbol, '1d',  limit=200)
    time.sleep(0.2)

    if not d15m or len(d15m.get('close', [])) < WARMUP_CANDLES + OUTCOME_CANDLES + 2:
        log.warning(f'{symbol}: datos insuficientes ({len(d15m["close"]) if d15m else 0} velas 15m)')
        return []

    closes  = d15m['close']
    highs   = d15m['high']
    lows    = d15m['low']
    amounts = d15m.get('amount', d15m.get('vol', [1.0] * len(closes)))
    times   = d15m.get('time', [])
    n       = len(closes)

    # Cooldown: (lvl_name, direction) -> ultimo idx disparado
    cooldown: dict = {}

    for idx in range(WARMUP_CANDLES, n - OUTCOME_CANDLES - 1):
        price = closes[idx]
        if price <= 0:
            continue

        # ZCT analisis sobre la ventana hasta idx (inclusive)
        zct = analyze_zct_window(closes[:idx+1], amounts[:idx+1])
        if not zct:
            continue

        crosses   = zct['crosses']
        vol_ratio = zct['vol_ratio']
        ma_dir    = zct['direction']

        # Filtros ZCT obligatorios
        if crosses > 1 or vol_ratio < 150:
            continue

        # Niveles en este instante
        candle_time = times[idx] if idx < len(times) else 0
        levels = compute_levels_at(candle_time, d1, d4h, d1h, d15m)
        if not levels:
            continue

        for lvl_name, lvl_price in levels.items():
            if lvl_price <= 0:
                continue

            dist_pct_val = abs(lvl_price - price) / price * 100

            # Proximidad: el precio debe estar a [0.2%, 0.5%] del nivel
            if not (0.2 <= dist_pct_val <= PROXIMITY_PCT * 100):
                continue

            # Direccion segun posicion del nivel
            if lvl_price > price:
                direction = 'LONG'   # nivel encima -> breakout alcista
                if ma_dir == 'down':
                    continue         # MA en contra -> skip
            else:
                direction = 'SHORT'  # nivel debajo -> breakout bajista
                if ma_dir == 'up':
                    continue         # MA en contra -> skip

            # Cooldown 4h (16 velas de 15m) por nivel
            cd_key = (lvl_name, direction)
            if cd_key in cooldown and idx - cooldown[cd_key] < 16:
                continue
            cooldown[cd_key] = idx

            # Entrada: SIGUIENTE vela (simula recibir la alerta y entrar)
            entry_price = closes[idx + 1]

            # Resultado: desde idx+2 (primer bar completo tras la entrada)
            future_highs = highs[idx+2 : idx+2+OUTCOME_CANDLES]
            future_lows  = lows [idx+2 : idx+2+OUTCOME_CANDLES]
            if len(future_highs) < 4:
                continue
            outcome = simulate_outcome(direction, entry_price, future_highs, future_lows)
            if outcome == 'TIMEOUT':
                continue

            # change_pct 24h (solo informativo)
            ref_idx    = max(0, idx - CHANGE_LB)
            change_pct = (price - closes[ref_idx]) / closes[ref_idx] * 100 if closes[ref_idx] > 0 else 0.0

            results.append({
                'symbol':     symbol,
                'ts':         datetime.utcfromtimestamp(candle_time).strftime('%m-%d %H:%M') if candle_time else '?',
                'direction':  direction,
                'level':      lvl_name,
                'change_pct': round(change_pct, 1),
                'crosses':    crosses,
                'ma_dir':     ma_dir,
                'vol_ratio':  round(vol_ratio, 1),
                'gap_pct':    0.0,
                'single_lvl': 1,
                'dist_pct':   round(dist_pct_val, 3),
                'outcome':    outcome,
            })

    log.info(f'{symbol}: {len(results)} setups')
    return results


# ══════════════════════════════════════════════════════════
#  ANÁLISIS DE RESULTADOS
# ══════════════════════════════════════════════════════════
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

    # Breakdown por tipo de nivel (PDH/PDL, P4H, P1H, P15m)
    lvl_groups = {
        'PDH/PDL':   ('PDH', 'PDL'),
        'P4H H/L':   ('P4HH', 'P4HL'),
        'P1H H/L':   ('P1HH', 'P1HL'),
        'P15m H/L':  ('P15mH', 'P15mL'),
    }
    by_level = {}
    for label, prefixes in lvl_groups.items():
        sub = [r for r in results if r.get('level', '') in prefixes]
        if sub:
            w = sum(1 for r in sub if r['outcome'] == 'WIN')
            by_level[label] = (w, len(sub), w / len(sub) * 100)

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
        'by_level': by_level,
    }


# ══════════════════════════════════════════════════════════
#  REPORTE TELEGRAM
# ══════════════════════════════════════════════════════════
def bar(wr: float) -> str:
    filled = round(wr / 10)
    return '█' * filled + '░' * (10 - filled)


def section(title: str, data: dict) -> str:
    if not data:
        return ''
    lines = [f'<b>{title}</b>']
    for label, (w, n, wr) in sorted(data.items(), key=lambda x: -x[1][2]):
        safe = html_mod.escape(label)
        lines.append(f'  {safe:<18} {bar(wr)} {wr:.0f}% ({w}/{n})')
    return '\n'.join(lines)


def build_report(a: dict, n_coins: int) -> str:
    if not a:
        return 'Sin datos suficientes.'

    wr    = a['wr']
    total = a['total']
    wins  = a['wins']
    emoji = 'OK' if wr >= 55 else 'MEH' if wr >= 45 else 'BAD'

    # Recomendaciones automaticas
    recs = []

    best_cross = max(a['by_crosses'].items(), key=lambda x: x[1][2], default=None)
    worst_cross = min(a['by_crosses'].items(), key=lambda x: x[1][2], default=None)
    if best_cross and best_cross[1][2] > wr + 5:
        recs.append(f'Mejor WR con {best_cross[0]} ({best_cross[1][2]:.0f}%)')
    if worst_cross and worst_cross[1][2] < wr - 10:
        recs.append(f'Evitar setups con {worst_cross[0]} ({worst_cross[1][2]:.0f}%)')

    best_dist = max(a['by_dist'].items(), key=lambda x: x[1][2], default=None)
    if best_dist and best_dist[1][2] > wr + 5:
        recs.append(f'Mejor WR cuando {best_dist[0]} ({best_dist[1][2]:.0f}%)')

    best_vol = max(a['by_vol'].items(), key=lambda x: x[1][2], default=None)
    if best_vol and best_vol[1][2] > wr + 5:
        recs.append(f'{best_vol[0]} tiene mejor WR ({best_vol[1][2]:.0f}%)')

    parts = [
        f'ZCT Backtest v2 -- {n_coins} monedas * 15m * 6h horizonte',
        f'Entrada: vela siguiente al setup (timing real del scanner)',
        f'',
        f'{emoji} Win rate: {wr:.1f}%  ({wins} win / {total-wins} loss / {total} total)',
        f'  SL: 2%   TP: 6% (3R)',
        f'',
        section('Por cruces de MA:', a['by_crosses']),
        f'',
        section('Por direccion MA:', a['by_ma']),
        f'',
        section('Por volumen relativo:', a['by_vol']),
        f'',
        section('Por fuerza del movimiento:', a['by_change']),
        f'',
        section('Por distancia al nivel:', a['by_dist']),
        f'',
        section('Por tipo de nivel:', a.get('by_level', {})),
    ]

    if recs:
        parts += ['', 'Insights automaticos:'] + recs

    parts += [
        f'',
        f'{datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}',
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


# ══════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════
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
        fieldnames = ['symbol', 'ts', 'direction', 'level', 'change_pct',
                      'crosses', 'ma_dir', 'vol_ratio', 'gap_pct',
                      'single_lvl', 'dist_pct', 'outcome']
        with open(csv_path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
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
