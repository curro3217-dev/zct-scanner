#!/usr/bin/env python3
"""
ZCT Backtester - Analiza el rendimiento historico del scanner.
Simula las senales del scanner en las ultimas velas de MEXC
y calcula que filtros funcionan (win rate por feature).
Envia el analisis por Telegram.
Requiere: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
"""

import os, time, csv, logging
from datetime import datetime, timezone
import requests

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

PROXIMITY_PCT = 0.005
MAX_DIST_PCT  = 15.0
CLUSTER_TOP   = 2.0
CLUSTER_ALT   = 3.0
SL_PCT        = 0.02
TP_PCT        = SL_PCT * 3.0
SMMA_LEN      = 30
VOL_MA_LEN    = 20
CROSS_LB      = 50
MA_DIR_LB     = 5
MA_DIR_THR    = 0.08
STRONG_PUMP   = 30.0
OUTCOME_CANDLES = 24
WARMUP_CANDLES  = SMMA_LEN + CROSS_LB + MA_DIR_LB + 5
CHANGE_LB       = 96

TOP_COINS = {'BTC_USDT', 'ETH_USDT', 'BNB_USDT', 'SOL_USDT', 'XRP_USDT'}
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


def get_klines(symbol, interval, limit=200):
    try:
        r = requests.get(f'https://contract.mexc.com/api/v1/contract/kline/{symbol}',
            params={'interval': INTERVAL_MAP[interval], 'limit': limit}, timeout=12)
        d = r.json().get('data', {})
        if not d or 'close' not in d or not d['close']:
            return None
        return {k: [float(x) for x in d[k]] for k in ('open','close','high','low','vol','amount','time') if k in d}
    except Exception as e:
        log.error(f'{symbol} {interval}: {e}')
        return None


def calc_smma(closes, length=30):
    if len(closes) < length: return []
    sma = sum(closes[:length]) / length
    result = [sma]
    for c in closes[length:]:
        result.append((result[-1] * (length - 1) + c) / length)
    return result


def count_crosses(closes, ma, lb=50):
    n = min(lb, len(closes)-1, len(ma)-1)
    crosses = 0
    for i in range(1, n+1):
        if (closes[-i] >= ma[-i]) != (closes[-(i+1)] >= ma[-(i+1)]):
            crosses += 1
    return crosses


def get_ma_direction(ma, lb=5):
    if len(ma) < lb+1: return 'sideways'
    pct = (ma[-1] - ma[-(lb+1)]) / ma[-(lb+1)] * 100
    return 'up' if pct > MA_DIR_THR else 'down' if pct < -MA_DIR_THR else 'sideways'


def analyze_zct_window(closes, amounts):
    ma = calc_smma(closes, SMMA_LEN)
    if not ma: return {}
    vol_ma = sum(amounts[-VOL_MA_LEN:]) / VOL_MA_LEN if len(amounts) >= VOL_MA_LEN else None
    return {
        'crosses': count_crosses(closes, ma, CROSS_LB),
        'direction': get_ma_direction(ma, MA_DIR_LB),
        'vol_ratio': amounts[-1] / vol_ma * 100 if vol_ma else 100.0,
    }


def find_level_cluster(levels, price, direction, symbol, change_pct=0.0):
    cluster_pct = CLUSTER_TOP if symbol in TOP_COINS else CLUSTER_ALT
    candidates = []
    for name, lvl in levels.items():
        if abs(lvl - price) / price * 100 > MAX_DIST_PCT: continue
        if direction == 'LONG' and lvl > price: candidates.append((name, lvl))
        elif direction == 'SHORT' and lvl < price: candidates.append((name, lvl))
    if not candidates: return None
    candidates.sort(key=lambda x: abs(x[1] - price))
    for i in range(len(candidates)):
        for j in range(i+1, len(candidates)):
            n1, l1 = candidates[i]; n2, l2 = candidates[j]
            gap = abs(l1 - l2) / min(l1, l2) * 100
            if gap <= cluster_pct:
                return {'lvl1_name': n1, 'lvl1': l1, 'lvl2_name': n2, 'lvl2': l2, 'gap_pct': gap, 'single_level': False}
    if abs(change_pct) >= STRONG_PUMP:
        n1, l1 = candidates[0]
        return {'lvl1_name': n1, 'lvl1': l1, 'lvl2_name': None, 'lvl2': None, 'gap_pct': 0.0, 'single_level': True}
    return None


def compute_levels_at(candle_time, d1, d4h, d1h, d15m):
    levels = {}
    if d1 and 'time' in d1:
        cdate = datetime.utcfromtimestamp(candle_time).date()
        ph = [d1['high'][i] for i,t in enumerate(d1['time']) if datetime.utcfromtimestamp(t).date() < cdate]
        pl = [d1['low'][i]  for i,t in enumerate(d1['time']) if datetime.utcfromtimestamp(t).date() < cdate]
        if ph: levels['PDH'] = ph[-1]; levels['PDL'] = pl[-1]
    for tag, d in [('P4H', d4h), ('P1H', d1h), ('P15m', d15m)]:
        if d and 'time' in d:
            prev = [(t,h,l) for t,h,l in zip(d['time'],d['high'],d['low']) if t < candle_time]
            if prev: levels[tag+'H'] = prev[-1][1]; levels[tag+'L'] = prev[-1][2]
    return levels


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


def backtest_coin(symbol):
    results = []
    d15m = get_klines(symbol, '15m'); time.sleep(0.15)
    d1h  = get_klines(symbol, '1h');  time.sleep(0.15)
    d4h  = get_klines(symbol, '4h');  time.sleep(0.15)
    d1   = get_klines(symbol, '1d');  time.sleep(0.2)
    if not d15m or len(d15m.get('close',[])) < WARMUP_CANDLES + OUTCOME_CANDLES + CHANGE_LB:
        return []
    closes  = d15m['close']; highs = d15m['high']; lows = d15m['low']
    amounts = d15m.get('amount', d15m.get('vol', [1.0]*len(closes)))
    times   = d15m.get('time', [])
    start   = max(WARMUP_CANDLES, CHANGE_LB)
    for idx in range(start, len(closes) - OUTCOME_CANDLES):
        price = closes[idx]
        if price <= 0: continue
        ref_price  = closes[max(0, idx - CHANGE_LB)]
        change_pct = (price - ref_price) / ref_price * 100 if ref_price > 0 else 0.0
        directions = []
        if change_pct >=  5.0: directions.append(('LONG',  change_pct))
        if change_pct <= -5.0: directions.append(('SHORT', change_pct))
        if not directions: continue
        zct = analyze_zct_window(closes[:idx+1], amounts[:idx+1])
        if not zct: continue
        candle_time = times[idx] if times else 0
        levels = compute_levels_at(candle_time, d1, d4h, d1h, d15m)
        if len(levels) < 2: continue
        for direction, cpct in directions:
            ma_dir = zct['direction']; crosses = zct['crosses']
            if direction == 'LONG'  and ma_dir == 'down': continue
            if direction == 'SHORT' and ma_dir == 'up':   continue
            if crosses > 6: continue
            cluster = find_level_cluster(levels, price, direction, symbol, cpct)
            if not cluster: continue
            near_lvl = cluster['lvl1']
            if cluster['lvl2'] is not None and abs(cluster['lvl2']-price) < abs(near_lvl-price):
                near_lvl = cluster['lvl2']
            dist = abs(near_lvl - price) / near_lvl
            if dist > PROXIMITY_PCT: continue
            outcome = simulate_outcome(direction, price, highs[idx+1:idx+1+OUTCOME_CANDLES], lows[idx+1:idx+1+OUTCOME_CANDLES])
            if outcome == 'TIMEOUT': continue
            results.append({'symbol': symbol,
                'ts': datetime.utcfromtimestamp(candle_time).strftime('%m-%d %H:%M') if candle_time else '?',
                'direction': direction, 'change_pct': round(cpct,1), 'crosses': crosses,
                'ma_dir': ma_dir, 'vol_ratio': round(zct['vol_ratio'],1),
                'gap_pct': round(cluster['gap_pct'],2), 'single_lvl': int(cluster['single_level']),
                'dist_pct': round(dist*100,3), 'outcome': outcome})
    log.info(f'{symbol}: {len(results)} setups')
    return results


def win_rate(subset):
    return sum(1 for r in subset if r['outcome']=='WIN') / len(subset) * 100 if subset else 0.0


def analyze(results):
    if not results: return {}
    total = len(results); wins = sum(1 for r in results if r['outcome']=='WIN'); wr = wins/total*100
    def bd(key, bins):
        out = {}
        for lo,hi,label in bins:
            sub = [r for r in results if lo <= r[key] < hi]
            if sub:
                w = sum(1 for r in sub if r['outcome']=='WIN')
                out[label] = (w, len(sub), w/len(sub)*100)
        return out
    return {
        'total': total, 'wins': wins, 'wr': wr,
        'by_crosses': bd('crosses', [(0,2,'0-1'),(2,4,'2-3'),(4,6,'4-5'),(6,99,'6+')]),
        'by_ma': {d: (sum(1 for r in results if r['outcome']=='WIN' and r['ma_dir']==d), sum(1 for r in results if r['ma_dir']==d), win_rate([r for r in results if r['ma_dir']==d])) for d in ('up','down','sideways') if any(r['ma_dir']==d for r in results)},
        'by_vol': bd('vol_ratio', [(0,85,'<85%'),(85,115,'85-115%'),(115,150,'115-150%'),(150,9999,'>150%')]),
        'by_change': bd('change_pct', [(-100,-20,'Dump>20%'),(-20,-5,'Dump5-20%'),(5,20,'Pump5-20%'),(20,100,'Pump>20%')]),
        'by_dist': bd('dist_pct', [(0,0.2,'<0.2%'),(0.2,0.35,'0.2-0.35%'),(0.35,0.5,'0.35-0.5%')]),
    }


def bbar(wr):
    f = round(wr/10)
    return '█'*f + '░'*(10-f)


def fmt_section(title, data):
    if not data: return ''
    lines = [f'<b>{title}</b>']
    for label,(w,n,wr) in sorted(data.items(), key=lambda x:-x[1][2]):
        lines.append(f'  {label:<12} {bbar(wr)} {wr:.0f}% ({w}/{n})')
    return '
'.join(lines)


def build_report(a, n_coins):
    if not a: return '❌ Sin datos suficientes.'
    wr=a['wr']; total=a['total']; wins=a['wins']
    emoji = 'U0001f7e2' if wr>=55 else 'U0001f7e1' if wr>=45 else 'U0001f534'
    recs=[]
    bc = max(a['by_crosses'].items(), key=lambda x:x[1][2], default=None)
    wc = min(a['by_crosses'].items(), key=lambda x:x[1][2], default=None)
    if bc and bc[1][2]>wr+5: recs.append(f'Cruces {bc[0]} mejor WR ({bc[1][2]:.0f}%)')
    if wc and wc[1][2]<wr-10: recs.append(f'Evitar cruces {wc[0]} ({wc[1][2]:.0f}%)')
    bd = max(a['by_dist'].items(), key=lambda x:x[1][2], default=None)
    if bd and bd[1][2]>wr+5: recs.append(f'Distancia {bd[0]} mejor WR ({bd[1][2]:.0f}%)')
    bv = max(a['by_vol'].items(), key=lambda x:x[1][2], default=None)
    if bv and bv[1][2]>wr+5: recs.append(f'Vol {bv[0]} mejor WR ({bv[1][2]:.0f}%)')
    parts = [
        f'U0001f4ca <b>ZCT Backtest — {n_coins} monedas · 15m · 6h horizonte</b>',
        f'{emoji} Win rate: <b>{wr:.1f}%</b>  ({wins}W / {total-wins}L / {total} total)',
        f'  SL 2%  →  TP 6% (3R)',
        '', fmt_section('U0001f501 Cruces MA:', a['by_crosses']),
        '', fmt_section('U0001f4c8 Direccion MA:', a['by_ma']),
        '', fmt_section('U0001f4ca Volumen:', a['by_vol']),
        '', fmt_section('U0001f680 Fuerza mov:', a['by_change']),
        '', fmt_section('U0001f4cd Distancia nivel:', a['by_dist']),
    ]
    if recs: parts += ['', '<b>U0001f4a1 Insights:</b>'] + [f'  • {r}' for r in recs]
    parts.append(f'
⏰ {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}')
    return '
'.join(p for p in parts if p is not None)


def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(msg); return
    try:
        r = requests.post(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
            data={'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'HTML', 'disable_web_page_preview': 'true'}, timeout=10)
        if not r.ok: log.error(f'Telegram: {r.text}')
    except Exception as e:
        log.error(f'Telegram: {e}'); print(msg)


def main():
    log.info('=== ZCT Backtester iniciando ===')
    all_results = []
    for i, symbol in enumerate(COINS):
        try:
            log.info(f'[{i+1}/{len(COINS)}] {symbol}')
            all_results.extend(backtest_coin(symbol))
        except Exception as e:
            log.error(f'{symbol}: {e}')
    log.info(f'Total setups: {len(all_results)}')
    if all_results:
        csv_path = os.path.join(os.path.dirname(__file__), 'backtest_results.csv')
        with open(csv_path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=all_results[0].keys())
            writer.writeheader(); writer.writerows(all_results)
        log.info(f'CSV: {csv_path}')
    send_telegram(build_report(analyze(all_results), len(COINS)))
    log.info('=== Backtest completado ===')


if __name__ == '__main__':
    main()
