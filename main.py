"""
ZCT Scanner v9 - Seleccion Dinamica de Movers
Selecciona monedas automaticamente segun:
  - Volumen 24h > $100M
  - Cambio >= +10% o <= -10% en 24h O en 7 dias
Aplica filtros ZCT: 15m, vol 80-200%, MA direction, cruces<=1, dist<=0.4%.
Niveles: P4H y P15m (backtest v7: WR 67% y 64%).
"""

import time, logging, os, json
from datetime import datetime, timezone
from pathlib import Path
import requests

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)

# =====================================================
#  CONFIGURACION
# =====================================================
TELEGRAM_TOKEN   = os.environ['TELEGRAM_TOKEN']
TELEGRAM_CHAT_ID = os.environ['TELEGRAM_CHAT_ID']

# Seleccion de monedas
VOL_24H_MIN   = 20_000_000   # $20M minimo volumen 24h (MEXC, no volumen global agregado)
CHANGE_THRESH = 10.0         # +-10% para ser mover (24h o 7d)

# Niveles ZCT
PROXIMITY_PCT  = 0.004   # alerta cuando precio esta a 0.4% de un nivel
MAX_DIST_PCT   = 15.0    # nivel ignorado si esta a mas del 15% del precio
CLUSTER_TOP    = 2.0     # distancia maxima entre 2 niveles (top coins)
CLUSTER_ALT    = 3.0     # distancia maxima entre 2 niveles (altcoins)

# Trade parameters
SL_PCT    = 0.02    # 2% stop loss
TP_MULT   = 3.0     # 3R -> TP = SL x 3 = 6%
LEVERAGE  = 10      # x10

# ZCT / MA
SMMA_LEN   = 30
VOL_MA_LEN = 20
CROSS_LB   = 50     # velas atras para contar cruces
MA_DIR_LB  = 5      # velas atras para detectar direccion MA
MA_DIR_THR = 0.08   # % minimo de pendiente para considerarla tendencial

# Cooldown
COOLDOWN_MIN = 60   # minutos entre alertas del mismo simbolo+direccion

# Top coins (cluster mas estricto: 2%)
TOP_COINS = {'BTC_USDT', 'ETH_USDT', 'BNB_USDT', 'SOL_USDT', 'XRP_USDT'}

# Pump fuerte: permite nivel unico como target
STRONG_PUMP_PCT = 30.0  # si |cambio| >= 30%, nivel unico permitido

# Bear market: alerta de salida anticipada
EARLY_EXIT_DIST = 5.0   # si nivel > 5% del precio, avisar salida anticipada

# Intervalos MEXC
INTERVAL_MAP = {
    '1m':  'Min1',
    '15m': 'Min15',
    '1h':  'Min60',
    '4h':  'Hour4',
    '1d':  'Day1',
}


ALERTS_LOG = Path(__file__).parent / 'alerts_log.json'


# =====================================================
#  TELEGRAM
# =====================================================
def send_telegram(msg: str):
    try:
        r = requests.post(
            f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
            data={
                'chat_id': TELEGRAM_CHAT_ID,
                'text': msg,
                'parse_mode': 'HTML',
                'disable_web_page_preview': 'true',
            },
            timeout=10,
        )
        if not r.ok:
            log.error(f'Telegram error: {r.text}')
    except Exception as e:
        log.error(f'Telegram exception: {e}')


# =====================================================
#  MEXC API
# =====================================================
def get_all_tickers() -> list:
    """Devuelve todos los tickers de futuros MEXC con stats 24h."""
    try:
        r = requests.get(
            'https://contract.mexc.com/api/v1/contract/ticker',
            timeout=15,
        )
        return r.json().get('data', [])
    except Exception as e:
        log.error(f'get_all_tickers: {e}')
        return []


def get_klines(symbol: str, interval: str, limit: int = 200):
    """Devuelve velas OHLCV de MEXC futuros."""
    try:
        r = requests.get(
            f'https://contract.mexc.com/api/v1/contract/kline/{symbol}',
            params={'interval': INTERVAL_MAP[interval], 'limit': limit},
            timeout=10,
        )
        d = r.json().get('data', {})
        if not d or 'close' not in d or not d['close']:
            return None
        return {k: [float(x) for x in d[k]]
                for k in ('open', 'close', 'high', 'low', 'vol', 'amount')
                if k in d}
    except Exception as e:
        log.error(f'{symbol} {interval}: {e}')
        return None


def get_changes(symbol: str):
    """
    Calcula cambio de precio en las ultimas 24h y ultimos 7 dias desde klines diarias.
    Devuelve (change_1d, change_7d) en porcentaje, o (None, None) si falla.

    Por que no usar riseFallRate del bulk ticker:
      - El bulk usa cambio de dia calendario UTC+8, no 24h rodantes.
      - Esto causaba 0 movers cuando una coin caia 21% pero el dia UTC+8
        acababa de empezar y el bulk mostraba solo -2%.
    """
    k = get_klines(symbol, '1d', limit=10)
    if not k or len(k['close']) < 2:
        return None, None

    closes = k['close']
    curr   = closes[-1]

    change_1d = (curr - closes[-2]) / closes[-2] * 100 if closes[-2] > 0 else None
    change_7d = (curr - closes[-8]) / closes[-8] * 100 if len(closes) >= 8 and closes[-8] > 0 else None

    return change_1d, change_7d


# =====================================================
#  SELECCION DE MOVERS
# =====================================================
def get_movers() -> list:
    """
    Filtra todos los futuros MEXC y devuelve movers con:
    - Vol 24h >= $100M (desde bulk ticker)
    - Cambio >= +/-10% en 24h O en 7 dias (calculado desde klines diarias)
    Ordenados por cambio absoluto descendente.
    """
    tickers = get_all_tickers()
    candidates = []

    # Paso 1: pre-filtrar por volumen (1 sola llamada bulk)
    for t in tickers:
        symbol = t.get('symbol', '')
        if not symbol.endswith('_USDT'):
            continue
        try:
            vol_24h = float(t.get('amount24', 0) or 0)
            price   = float(t.get('lastPrice', 0) or 0)
        except (ValueError, TypeError):
            continue
        if vol_24h >= VOL_24H_MIN and price > 0:
            candidates.append({'symbol': symbol, 'vol_24h': vol_24h, 'price': price})

    log.info(f'Candidatos por volumen (>= ${VOL_24H_MIN/1e6:.0f}M): {len(candidates)}')

    # Paso 2: filtrar por cambio 24h o 7d
    movers = []
    for c in candidates:
        symbol = c['symbol']
        change_1d, change_7d = get_changes(symbol)
        if change_1d is None:
            log.debug(f'{symbol}: sin datos diarios')
            continue

        chg_1d_str = f'{change_1d:+.1f}%'
        chg_7d_str = f'{change_7d:+.1f}%' if change_7d is not None else 'N/A'

        qualifies_long  = (change_1d >= CHANGE_THRESH or
                           (change_7d is not None and change_7d >= CHANGE_THRESH))
        qualifies_short = (change_1d <= -CHANGE_THRESH or
                           (change_7d is not None and change_7d <= -CHANGE_THRESH))

        if qualifies_long:
            direction  = 'LONG'
            change_pct = change_1d
        elif qualifies_short:
            direction  = 'SHORT'
            change_pct = change_1d
        else:
            log.debug(f'{symbol}: 1d={chg_1d_str} 7d={chg_7d_str} -> skip')
            continue

        movers.append({
            'symbol':     symbol,
            'direction':  direction,
            'change_pct': change_pct,
            'change_7d':  change_7d,
            'vol_24h':    c['vol_24h'],
            'price':      c['price'],
        })
        time.sleep(0.15)

    movers.sort(key=lambda x: abs(x['change_pct']), reverse=True)
    n_long  = sum(1 for m in movers if m['direction'] == 'LONG')
    n_short = sum(1 for m in movers if m['direction'] == 'SHORT')
    log.info(f'Movers: {len(movers)} ({n_long}L / {n_short}S)')

    for m in movers[:5]:
        chg7 = f' (7d {m["change_7d"]:+.1f}%)' if m.get('change_7d') else ''
        log.info(f'  {m["symbol"]} {m["direction"]} 1d={m["change_pct"]:+.1f}%{chg7} vol=${m["vol_24h"]/1e6:.0f}M')

    return movers


# =====================================================
#  ANALISIS TECNICO ZCT
# =====================================================
def calc_smma(closes: list, length: int = 30) -> list:
    """Smoothed Moving Average (Wilder). Identica a la de TradingView."""
    if len(closes) < length:
        return []
    sma = sum(closes[:length]) / length
    result = [sma]
    for c in closes[length:]:
        result.append((result[-1] * (length - 1) + c) / length)
    return result


def count_crosses(closes: list, ma: list, lb: int = 50) -> int:
    """Cuenta cruces del precio con la 30SMMA en las ultimas lb velas."""
    n = min(lb, len(closes) - 1, len(ma) - 1)
    crosses = 0
    for i in range(1, n + 1):
        above_now  = closes[-i]     >= ma[-i]
        above_prev = closes[-(i+1)] >= ma[-(i+1)]
        if above_now != above_prev:
            crosses += 1
    return crosses


def get_ma_direction(ma: list, lb: int = 5) -> str:
    """Devuelve 'up', 'down' o 'sideways' segun la pendiente de la MA."""
    if len(ma) < lb + 1:
        return 'sideways'
    pct = (ma[-1] - ma[-(lb+1)]) / ma[-(lb+1)] * 100
    if pct > MA_DIR_THR:
        return 'up'
    if pct < -MA_DIR_THR:
        return 'down'
    return 'sideways'


def analyze_zct(closes: list, amounts: list) -> dict:
    """Aplica los filtros ZCT de medias moviles y volumen."""
    ma = calc_smma(closes, SMMA_LEN)
    if not ma:
        return {}

    crosses   = count_crosses(closes, ma, CROSS_LB)
    direction = get_ma_direction(ma, MA_DIR_LB)

    vol_ma    = (sum(amounts[-VOL_MA_LEN:]) / VOL_MA_LEN
                 if len(amounts) >= VOL_MA_LEN else None)
    vol_ratio = amounts[-1] / vol_ma * 100 if vol_ma else 100.0

    mom_ideal = (crosses <= 3 and direction in ('up', 'down') and vol_ratio > 80)
    mr_ideal  = (crosses >= 7 and direction == 'sideways' and 85 <= vol_ratio <= 115)

    return {
        'crosses':   crosses,
        'direction': direction,
        'vol_ratio': vol_ratio,
        'mom_ideal': mom_ideal,
        'mr_ideal':  mr_ideal,
    }


# =====================================================
#  NIVELES ZCT
# =====================================================
def get_zct_levels(symbol: str) -> dict:
    """Obtiene niveles ZCT de la vela anterior en cada timeframe."""
    levels = {}
    for interval, prefix in [('4h', 'P4H'), ('15m', 'P15m')]:
        k = get_klines(symbol, interval, limit=3)
        if k and len(k['high']) >= 2:
            levels[f'{prefix}H'] = k['high'][-2]
            levels[f'{prefix}L'] = k['low'][-2]
    return levels


def find_level_cluster(levels: dict, price: float,
                       direction: str, symbol: str,
                       change_pct: float = 0.0):
    """
    Busca un cluster de 2 niveles valido en la direccion correcta,
    dentro del 15% del precio y separados maximo cluster_pct.
    Si |cambio| >= 30% permite nivel unico.
    """
    cluster_pct = CLUSTER_TOP if symbol in TOP_COINS else CLUSTER_ALT

    candidates = []
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
                return {
                    'lvl1_name':    n1, 'lvl1': l1,
                    'lvl2_name':    n2, 'lvl2': l2,
                    'gap_pct':      gap,
                    'single_level': False,
                }

    if abs(change_pct) >= STRONG_PUMP_PCT:
        n1, l1 = candidates[0]
        return {
            'lvl1_name':    n1,   'lvl1': l1,
            'lvl2_name':    None, 'lvl2': None,
            'gap_pct':      0.0,
            'single_level': True,
        }

    return None


# =====================================================
#  LOG DE ALERTAS
# =====================================================
def save_alert_to_log(mover: dict, cluster: dict, zct: dict,
                      near_lvl: float, dist_pct: float):
    """Guarda cada alerta en alerts_log.json para tracking de resultados."""
    now       = datetime.now(timezone.utc)
    price     = mover['price']
    direction = mover['direction']
    tp_pct    = SL_PCT * TP_MULT

    if direction == 'LONG':
        sl = price * (1 - SL_PCT)
        tp = price * (1 + tp_pct)
    else:
        sl = price * (1 + SL_PCT)
        tp = price * (1 - tp_pct)

    record = {
        'id':           f'{mover["symbol"]}_{direction}_{now.strftime("%Y%m%d_%H%M%S")}',
        'symbol':       mover['symbol'],
        'direction':    direction,
        'timestamp':    now.isoformat(),
        'entry_price':  price,
        'sl':           round(sl, 8),
        'tp':           round(tp, 8),
        'change_pct':   round(mover['change_pct'], 2),
        'change_7d':    round(mover['change_7d'], 2) if mover.get('change_7d') else None,
        'vol_ratio':    round(zct['vol_ratio'], 1),
        'crosses':      zct['crosses'],
        'ma_direction': zct['direction'],
        'lvl1_name':    cluster['lvl1_name'],
        'lvl1':         cluster['lvl1'],
        'lvl2_name':    cluster.get('lvl2_name'),
        'lvl2':         cluster.get('lvl2'),
        'dist_pct':     round(dist_pct, 3),
        'status':       'OPEN',
        'resolved_at':  None,
        'resolved_price': None,
    }

    if ALERTS_LOG.exists():
        try:
            with open(ALERTS_LOG, encoding='utf-8') as f:
                log_data = json.load(f)
        except Exception:
            log_data = []
    else:
        log_data = []

    log_data.append(record)

    with open(ALERTS_LOG, 'w', encoding='utf-8') as f:
        json.dump(log_data, f, indent=2, ensure_ascii=False)

    log.info(f'Alerta guardada en log: {record["id"]}')


# =====================================================
#  CONSTRUCCION DE ALERTA
# =====================================================
def build_alert(mover: dict, cluster: dict, zct: dict,
                near_lvl: float, dist_pct: float) -> str:
    symbol     = mover['symbol']
    direction  = mover['direction']
    price      = mover['price']
    change_pct = mover['change_pct']
    change_7d  = mover.get('change_7d')
    vol_24h    = mover['vol_24h']

    tp_pct = SL_PCT * TP_MULT
    if direction == 'LONG':
        sl, tp        = price * (1 - SL_PCT), price * (1 + tp_pct)
        d_emoji, d_txt = 'LONG'[0], 'LONG'
        d_emoji = 'LONG'
    else:
        sl, tp        = price * (1 + SL_PCT), price * (1 - tp_pct)
        d_emoji = 'SHORT'
        d_txt   = 'SHORT'

    fmt   = lambda p: f'{p:,.4f}' if p >= 1 else f'{p:.6f}'
    chg   = f'+{change_pct:.1f}%' if change_pct > 0 else f'{change_pct:.1f}%'
    vol_m = vol_24h / 1_000_000

    # Cambio mostrado: 24h siempre; 7d si es relevante (>= umbral y 24h no llega)
    chg_line = f'Cambio 1d: <b>{chg}</b>'
    if change_7d is not None and abs(change_7d) >= CHANGE_THRESH:
        chg7 = f'+{change_7d:.1f}%' if change_7d > 0 else f'{change_7d:.1f}%'
        chg_line += f'  ·  7d: <b>{chg7}</b>'

    ma_txt  = {'up': 'Alcista', 'down': 'Bajista',
               'sideways': 'Lateral'}[zct['direction']]
    vol_txt = ('Creciente' if zct['vol_ratio'] > 115
               else 'Decreciente' if zct['vol_ratio'] < 85
               else 'Plano')

    if cluster['single_level']:
        levels_txt = (
            f'Nivel unico: {cluster["lvl1_name"]} @ {fmt(cluster["lvl1"])}\n'
            f'   Pump fuerte ({chg}) - nivel unico valido'
        )
    else:
        levels_txt = (
            f'Nivel 1: {cluster["lvl1_name"]} @ {fmt(cluster["lvl1"])}\n'
            f'Nivel 2: {cluster["lvl2_name"]} @ {fmt(cluster["lvl2"])}'
            f'  ({cluster["gap_pct"]:.1f}% entre niveles)'
        )

    bear_note = ''
    if dist_pct > EARLY_EXIT_DIST:
        bear_note = (
            f'\nNivel a {dist_pct:.1f}% - mercado debil: '
            f'considerar salida anticipada'
        )

    tv = (f'https://www.tradingview.com/chart/?symbol='
          f'BINANCE:{symbol.replace("_","")}&interval=5')

    dir_icon = 'LONG' if direction == 'LONG' else 'SHORT'

    return (
        f'ZCT: <b>{symbol}</b> - Mover ZCT\n\n'
        f'{chg_line}  .  Vol24h: <b>${vol_m:.0f}M</b>\n\n'
        f'{levels_txt}\n\n'
        f'Precio: {fmt(price)}  ->  {dist_pct:.2f}% del nivel'
        f'{bear_note}\n'
        f'<b>Condiciones ZCT (15m):</b>\n'
        f'{ma_txt}  .  Cruces: {zct["crosses"]}'
        f'  .  Vol: {vol_txt} ({zct["vol_ratio"]:.0f}%)\n'
        f'\n'
        f'<b>{dir_icon}  .  x{LEVERAGE} (demo)</b>\n'
        f'Entry: {fmt(price)}\n'
        f'SL:    {fmt(sl)}  (2% max)\n'
        f'TP:    {fmt(tp)}  (6% = 3R)\n'
        f'\n<a href="{tv}">Ver en TradingView (5m)</a>\n'
        f'{datetime.now(timezone.utc).strftime("%H:%M UTC")}'
    )


# =====================================================
#  ANALISIS POR SIMBOLO
# =====================================================
def analyze(mover: dict):
    symbol    = mover['symbol']
    direction = mover['direction']
    price     = mover['price']

    key = f'{symbol}_{direction}'
    now = datetime.now(timezone.utc)

    # Cooldown persistente: lee alerts_log.json para saber la ultima alerta
    # (el dict _cooldowns en memoria no sobrevive entre runs de GitHub Actions)
    if ALERTS_LOG.exists():
        try:
            with open(ALERTS_LOG, encoding='utf-8') as f:
                log_data = json.load(f)
            for rec in reversed(log_data):
                if rec.get('symbol') == symbol and rec.get('direction') == direction:
                    last_ts = datetime.fromisoformat(rec['timestamp'])
                    if last_ts.tzinfo is None:
                        last_ts = last_ts.replace(tzinfo=timezone.utc)
                    elapsed = (now - last_ts).total_seconds() / 60
                    if elapsed < COOLDOWN_MIN:
                        log.info(f'{symbol}: cooldown activo ({elapsed:.0f}min < {COOLDOWN_MIN}min) -> skip')
                        return
                    break
        except Exception as e:
            log.warning(f'Cooldown check error: {e}')

    k15m = get_klines(symbol, '15m', limit=200)
    if not k15m:
        log.warning(f'{symbol}: sin datos 15m')
        return

    closes  = k15m['close']
    opens   = k15m.get('open', closes)
    amounts = k15m.get('amount', k15m['vol'])

    zct = analyze_zct(closes, amounts)
    if not zct:
        return

    ma_dir    = zct['direction']
    crosses   = zct['crosses']
    vol_ratio = zct['vol_ratio']

    # Filtro 1: MA nunca contra la direccion del trade
    if direction == 'LONG' and ma_dir == 'down':
        log.info(f'{symbol}: LONG pero MA bajista -> skip')
        return
    if direction == 'SHORT' and ma_dir == 'up':
        log.info(f'{symbol}: SHORT pero MA alcista -> skip')
        return

    # Filtro 2: maximo 1 cruce
    if crosses > 1:
        log.info(f'{symbol}: demasiado choppy ({crosses} cruces) -> skip')
        return

    # Filtro 3: volumen 80-200%
    if vol_ratio < 80:
        log.info(f'{symbol}: volumen muy bajo ({vol_ratio:.0f}%) -> skip')
        return
    if vol_ratio > 200:
        log.info(f'{symbol}: volumen demasiado alto ({vol_ratio:.0f}%) -> skip')
        return

    # Filtro 4: vela alcista obligatoria para LONG
    if direction == 'LONG' and closes[-1] <= opens[-1]:
        log.info(f'{symbol}: vela bajista en LONG -> skip')
        return

    log.info(f'{symbol}: chg={mover["change_pct"]:.1f}% dir={direction} '
             f'ma={ma_dir} cruces={crosses} vol={vol_ratio:.0f}% OK filtros')

    # Niveles ZCT
    levels = get_zct_levels(symbol)
    if len(levels) < 2:
        log.info(f'{symbol}: no hay suficientes niveles -> skip')
        return

    log.info(f'{symbol}: niveles = {", ".join(f"{k}={v:.6g}" for k,v in levels.items())}')

    cluster = find_level_cluster(
        levels, price, direction, symbol,
        change_pct=mover['change_pct']
    )
    if not cluster:
        cands = [(k, v, abs(v-price)/price*100) for k, v in levels.items()
                 if (direction == 'LONG' and v > price) or (direction == 'SHORT' and v < price)]
        if cands:
            cands_str = ', '.join(f'{k}={dist:.2f}%' for k,_,dist in cands)
            log.info(f'{symbol}: sin cluster - candidatos: {cands_str} -> skip')
        else:
            log.info(f'{symbol}: sin niveles en direccion {direction} -> skip')
        return

    if cluster['single_level'] or cluster['lvl2'] is None:
        near_lvl = cluster['lvl1']
    else:
        near_lvl = (cluster['lvl1']
                    if abs(cluster['lvl1'] - price) < abs(cluster['lvl2'] - price)
                    else cluster['lvl2'])

    dist     = abs(near_lvl - price) / near_lvl
    dist_pct = dist * 100

    if dist > PROXIMITY_PCT:
        log.info(f'{symbol}: no suficientemente cerca ({dist_pct:.2f}%) -> skip')
        return

    if dist_pct < 0.2:
        log.info(f'{symbol}: demasiado pegado al nivel ({dist_pct:.3f}%) -> skip')
        return

    msg = build_alert(mover, cluster, zct, near_lvl, dist_pct)
    send_telegram(msg)
    save_alert_to_log(mover, cluster, zct, near_lvl, dist_pct)
    lvl_type = 'nivel unico' if cluster['single_level'] else 'cluster 2 niveles'
    log.info(f'ALERTA -> {symbol} {direction} [{lvl_type}]')


# =====================================================
#  SCAN PRINCIPAL
# =====================================================
def scan():
    log.info('=== Scan iniciado ===')
    movers = get_movers()
    if not movers:
        log.info('Sin movers en este ciclo')
        return
    for mover in movers:
        try:
            analyze(mover)
            time.sleep(0.3)
        except Exception as e:
            log.error(f'{mover["symbol"]}: {e}')
    log.info('=== Scan completado ===')


if __name__ == '__main__':
    scan_once = os.environ.get('SCAN_ONCE', '0') == '1'
    if scan_once:
        scan()
    else:
        while True:
            scan()
            time.sleep(300)
