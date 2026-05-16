"""
ZCT Scanner v3 — Selección Dinámica de Movers
Selecciona monedas automáticamente según:
  - Volumen 24h > $100M
  - Cambio 1d ≥ +10% → solo LONG
  - Cambio 1d ≤ -10% → solo SHORT
Aplica filtros ZCT: cluster de 2 niveles (o 1 si pump ≥30%), 30SMMA, volumen.
Niveles: PDH/PDL (1d), P4H, P1H, P15m.

Estrategia: Trading From Zero / Koroush AK (ZCT)
Autor: generado con Claude para Curro / Tradetor
"""

import time, logging, os
from datetime import datetime, timezone
import requests

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)

# ══════════════════════════════════════════════════════════
#  CONFIGURACIÓN
# ══════════════════════════════════════════════════════════
TELEGRAM_TOKEN   = os.environ['TELEGRAM_TOKEN']
TELEGRAM_CHAT_ID = os.environ['TELEGRAM_CHAT_ID']

# ── Selección de monedas ──────────────────────────────────
VOL_24H_MIN   = 100_000_000   # $100M mínimo volumen 24h
CHANGE_THRESH = 10.0          # ±10% en 1d para ser mover

# ── Niveles ZCT ──────────────────────────────────────────
PROXIMITY_PCT  = 0.005   # alerta cuando precio está a 0.5% de un nivel
MAX_DIST_PCT   = 15.0    # nivel ignorado si está a más del 15% del precio
CLUSTER_TOP    = 2.0     # distancia máxima entre 2 niveles (top coins)
CLUSTER_ALT    = 3.0     # distancia máxima entre 2 niveles (altcoins)

# ── Trade parameters ──────────────────────────────────────
SL_PCT    = 0.02    # 2% stop loss máximo
TP_MULT   = 3.0     # 3R mínimo → TP = SL × 3 = 6%
LEVERAGE  = 5       # demo

# ── ZCT / MA ──────────────────────────────────────────────
SMMA_LEN   = 30
VOL_MA_LEN = 20
CROSS_LB   = 50     # velas atrás para contar cruces
MA_DIR_LB  = 5      # velas atrás para detectar dirección MA
MA_DIR_THR = 0.08   # % mínimo de pendiente para considerarla tendencial

# ── Cooldown ──────────────────────────────────────────────
COOLDOWN_MIN = 60   # minutos entre alertas del mismo símbolo+dirección

# ── Top coins (cluster más estricto: 2%) ─────────────────
TOP_COINS = {'BTC_USDT', 'ETH_USDT', 'BNB_USDT', 'SOL_USDT', 'XRP_USDT'}

# ── Pump fuerte: permite nivel único como target ──────────
STRONG_PUMP_PCT = 30.0  # si |cambio| >= 30%, nivel único permitido

# ── Bear market: alerta de salida anticipada ──────────────
EARLY_EXIT_DIST = 5.0   # si nivel > 5% del precio, avisar salida anticipada

# ── Intervalos MEXC ──────────────────────────────────────
INTERVAL_MAP = {
    '1m':  'Min1',
    '15m': 'Min15',
    '1h':  'Min60',
    '4h':  'Hour4',
    '1d':  'Day1',
}

# Estado de cooldowns en memoria
_cooldowns: dict = {}


# ══════════════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════════════
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


# ══════════════════════════════════════════════════════════
#  MEXC API
# ══════════════════════════════════════════════════════════
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


# ══════════════════════════════════════════════════════════
#  SELECCIÓN DE MOVERS
# ══════════════════════════════════════════════════════════
def get_movers() -> list:
    """
    Filtra todos los futuros MEXC y devuelve movers con:
    - Vol 24h > $100M
    - Cambio 1d >= +10% (LONG) o <= -10% (SHORT)
    Ordenados por cambio absoluto descendente.
    """
    tickers = get_all_tickers()
    movers  = []

    for t in tickers:
        symbol = t.get('symbol', '')
        if not symbol.endswith('_USDT'):
            continue
        try:
            vol_24h    = float(t.get('amount24', 0) or 0)
            change_pct = float(t.get('riseFallRate', 0) or 0) * 100
            price      = float(t.get('lastPrice', 0) or 0)
        except (ValueError, TypeError):
            continue

        if vol_24h < VOL_24H_MIN or price <= 0:
            continue

        if change_pct >= CHANGE_THRESH:
            direction = 'LONG'
        elif change_pct <= -CHANGE_THRESH:
            direction = 'SHORT'
        else:
            continue

        movers.append({
            'symbol':     symbol,
            'direction':  direction,
            'change_pct': change_pct,
            'vol_24h':    vol_24h,
            'price':      price,
        })

    movers.sort(key=lambda x: abs(x['change_pct']), reverse=True)
    n_long  = sum(1 for m in movers if m['direction'] == 'LONG')
    n_short = sum(1 for m in movers if m['direction'] == 'SHORT')
    log.info(f'Movers: {len(movers)} ({n_long}L / {n_short}S)')
    return movers


# ══════════════════════════════════════════════════════════
#  ANÁLISIS TÉCNICO ZCT
# ══════════════════════════════════════════════════════════
def calc_smma(closes: list, length: int = 30) -> list:
    """Smoothed Moving Average (Wilder). Idéntica a la de TradingView."""
    if len(closes) < length:
        return []
    sma = sum(closes[:length]) / length
    result = [sma]
    for c in closes[length:]:
        result.append((result[-1] * (length - 1) + c) / length)
    return result


def count_crosses(closes: list, ma: list, lb: int = 50) -> int:
    """Cuenta cruces del precio con la 30SMMA en las últimas lb velas."""
    n = min(lb, len(closes) - 1, len(ma) - 1)
    crosses = 0
    for i in range(1, n + 1):
        above_now  = closes[-i]     >= ma[-i]
        above_prev = closes[-(i+1)] >= ma[-(i+1)]
        if above_now != above_prev:
            crosses += 1
    return crosses


def get_ma_direction(ma: list, lb: int = 5) -> str:
    """Devuelve 'up', 'down' o 'sideways' según la pendiente de la MA."""
    if len(ma) < lb + 1:
        return 'sideways'
    pct = (ma[-1] - ma[-(lb+1)]) / ma[-(lb+1)] * 100
    if pct > MA_DIR_THR:
        return 'up'
    if pct < -MA_DIR_THR:
        return 'down'
    return 'sideways'


def analyze_zct(closes: list, amounts: list) -> dict:
    """
    Aplica los filtros ZCT de medias móviles y volumen.
    Devuelve dict con crosses, ma_direction, vol_ratio, etc.
    """
    ma = calc_smma(closes, SMMA_LEN)
    if not ma:
        return {}

    crosses   = count_crosses(closes, ma, CROSS_LB)
    direction = get_ma_direction(ma, MA_DIR_LB)

    vol_ma    = (sum(amounts[-VOL_MA_LEN:]) / VOL_MA_LEN
                 if len(amounts) >= VOL_MA_LEN else None)
    vol_ratio = amounts[-1] / vol_ma * 100 if vol_ma else 100.0

    mom_ideal = (crosses <= 3 and direction in ('up', 'down') and vol_ratio > 115)
    mr_ideal  = (crosses >= 7 and direction == 'sideways' and 85 <= vol_ratio <= 115)

    return {
        'crosses':   crosses,
        'direction': direction,
        'vol_ratio': vol_ratio,
        'mom_ideal': mom_ideal,
        'mr_ideal':  mr_ideal,
    }


# ══════════════════════════════════════════════════════════
#  NIVELES ZCT
# ══════════════════════════════════════════════════════════
def get_zct_levels(symbol: str) -> dict:
    """
    Obtiene niveles ZCT de la vela anterior en cada timeframe:
      PDH/PDL   → día anterior   (mayor liquidez, tarda más en activarse)
      P4HH/P4HL → 4h anterior
      P1HH/P1HL → 1h anterior
      P15mH/P15mL → 15m anterior  (excelentes para intradia, 70% de trades)
    """
    levels = {}
    for interval, prefix in [('1d', 'PD'), ('4h', 'P4H'), ('1h', 'P1H'), ('15m', 'P15m')]:
        k = get_klines(symbol, interval, limit=3)
        if k and len(k['high']) >= 2:
            levels[f'{prefix}H'] = k['high'][-2]
            levels[f'{prefix}L'] = k['low'][-2]
    return levels


def find_level_cluster(levels: dict, price: float,
                       direction: str, symbol: str,
                       change_pct: float = 0.0):
    """
    Busca un cluster de 2 niveles válido:
      - En la dirección correcta (resistencia para LONG, soporte para SHORT)
      - Dentro del 15% del precio actual
      - Separados máximo cluster_pct (2% top coins / 3% altcoins)

    Excepción nivel único (Anexo 7 del PDF):
      Si |cambio 1d| >= 30% y no hay cluster, permite 1 nivel solo.
      Solo cuando la moneda se mueve con fuerza y sin niveles adicionales.

    Devuelve dict con lvl1/lvl2 y flag 'single_level', o None.
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

    # Intentar cluster de 2 niveles (caso normal)
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

    # Excepción: pump/dump fuerte (≥30%) → nivel único válido
    if abs(change_pct) >= STRONG_PUMP_PCT:
        n1, l1 = candidates[0]
        return {
            'lvl1_name':    n1,   'lvl1': l1,
            'lvl2_name':    None, 'lvl2': None,
            'gap_pct':      0.0,
            'single_level': True,
        }

    return None


# ══════════════════════════════════════════════════════════
#  CONSTRUCCIÓN DE ALERTA
# ══════════════════════════════════════════════════════════
def build_alert(mover: dict, cluster: dict, zct: dict,
                near_lvl: float, dist_pct: float) -> str:
    symbol     = mover['symbol']
    direction  = mover['direction']
    price      = mover['price']
    change_pct = mover['change_pct']
    vol_24h    = mover['vol_24h']

    tp_pct = SL_PCT * TP_MULT
    if direction == 'LONG':
        sl, tp        = price * (1 - SL_PCT), price * (1 + tp_pct)
        d_emoji, d_txt = '🟢', 'LONG'
    else:
        sl, tp        = price * (1 + SL_PCT), price * (1 - tp_pct)
        d_emoji, d_txt = '🔴', 'SHORT'

    fmt    = lambda p: f'{p:,.4f}' if p >= 1 else f'{p:.6f}'
    chg    = f'+{change_pct:.1f}%' if change_pct > 0 else f'{change_pct:.1f}%'
    vol_m  = vol_24h / 1_000_000

    ma_txt  = {'up': '📈 Alcista', 'down': '📉 Bajista',
               'sideways': '↔️ Lateral'}[zct['direction']]
    vol_txt = ('📈 Creciente' if zct['vol_ratio'] > 115
               else '📉 Decreciente' if zct['vol_ratio'] < 85
               else '➡️ Plano')

    # Bloque de niveles: cluster normal o nivel único (pump fuerte)
    if cluster['single_level']:
        levels_txt = (
            f'📍 Nivel único: {cluster["lvl1_name"]} @ {fmt(cluster["lvl1"])}\n'
            f'   ⚡ Pump fuerte ({chg}) — nivel único válido'
        )
    else:
        levels_txt = (
            f'📍 Nivel 1: {cluster["lvl1_name"]} @ {fmt(cluster["lvl1"])}\n'
            f'📍 Nivel 2: {cluster["lvl2_name"]} @ {fmt(cluster["lvl2"])}'
            f'  ({cluster["gap_pct"]:.1f}% entre niveles)'
        )

    # Nota bear market: si nivel > 5% del precio, mejor salida anticipada
    bear_note = ''
    if dist_pct > EARLY_EXIT_DIST:
        bear_note = (
            f'\n⚠️ Nivel a {dist_pct:.1f}% — mercado débil: '
            f'considerar salida anticipada antes del sweep completo'
        )

    tv = (f'https://www.tradingview.com/chart/?symbol='
          f'BINANCE:{symbol.replace("_","")}&interval=240')

    return (
        f'🔔 <b>{symbol}</b> — Mover ZCT\n\n'
        f'📊 Cambio 1d: <b>{chg}</b>  ·  Vol24h: <b>${vol_m:.0f}M</b>\n\n'
        f'{levels_txt}\n\n'
        f'💰 Precio: {fmt(price)}  →  {dist_pct:.2f}% del nivel\n'
        f'{bear_note}\n'
        f'<b>Condiciones ZCT (1m):</b>\n'
        f'{ma_txt}  ·  🔁 Cruces: {zct["crosses"]}'
        f'  ·  📊 Vol: {vol_txt} ({zct["vol_ratio"]:.0f}%)\n'
        f'\n─────────────────\n'
        f'<b>{d_emoji} {d_txt}  ·  x{LEVERAGE} (demo)</b>\n'
        f'📥 Entry: {fmt(price)}\n'
        f'🛑 SL:    {fmt(sl)}  (2% máx)\n'
        f'🎯 TP:    {fmt(tp)}  (6% = 3R mínimo)\n'
        f'\n🔗 <a href="{tv}">Ver en TradingView (4H)</a>\n'
        f'⏰ {datetime.now(timezone.utc).strftime("%H:%M UTC")}'
    )


# ══════════════════════════════════════════════════════════
#  ANÁLISIS POR SÍMBOLO
# ══════════════════════════════════════════════════════════
def analyze(mover: dict):
    symbol    = mover['symbol']
    direction = mover['direction']
    price     = mover['price']

    # Cooldown
    key = f'{symbol}_{direction}'
    now = datetime.now(timezone.utc)
    if key in _cooldowns:
        elapsed = (now - _cooldowns[key]).total_seconds() / 60
        if elapsed < COOLDOWN_MIN:
            return

    # Velas 1m para análisis ZCT
    k1m = get_klines(symbol, '1m', limit=200)
    if not k1m:
        log.warning(f'{symbol}: sin datos 1m')
        return

    closes  = k1m['close']
    amounts = k1m.get('amount', k1m['vol'])

    zct = analyze_zct(closes, amounts)
    if not zct:
        return

    ma_dir    = zct['direction']
    crosses   = zct['crosses']
    vol_ratio = zct['vol_ratio']

    # Filtro 1: MA nunca contra la dirección del trade
    if direction == 'LONG' and ma_dir == 'down':
        log.info(f'{symbol}: LONG pero MA bajista → skip')
        return
    if direction == 'SHORT' and ma_dir == 'up':
        log.info(f'{symbol}: SHORT pero MA alcista → skip')
        return

    # Filtro 2: máximo 6 cruces (7+ = choppy, no apto para momentum)
    if crosses > 6:
        log.info(f'{symbol}: demasiado choppy ({crosses} cruces) → skip')
        return

    log.info(f'{symbol}: chg={mover["change_pct"]:.1f}% dir={direction} '
             f'ma={ma_dir} cruces={crosses} vol={vol_ratio:.0f}%')

    # Niveles ZCT
    levels = get_zct_levels(symbol)
    if len(levels) < 2:
        log.info(f'{symbol}: no hay suficientes niveles')
        return

    # Cluster de 2 niveles (o 1 si pump fuerte ≥30%)
    cluster = find_level_cluster(
        levels, price, direction, symbol,
        change_pct=mover['change_pct']
    )
    if not cluster:
        log.info(f'{symbol}: no se encontró cluster de niveles')
        return

    # Proximidad al nivel más cercano del cluster
    if cluster['single_level'] or cluster['lvl2'] is None:
        near_lvl = cluster['lvl1']
    else:
        near_lvl = (cluster['lvl1']
                    if abs(cluster['lvl1'] - price) < abs(cluster['lvl2'] - price)
                    else cluster['lvl2'])

    dist     = abs(near_lvl - price) / near_lvl
    dist_pct = dist * 100

    if dist > PROXIMITY_PCT:
        log.info(f'{symbol}: no suficientemente cerca ({dist_pct:.2f}%) → skip')
        return

    # Alerta
    _cooldowns[key] = now
    msg = build_alert(mover, cluster, zct, near_lvl, dist_pct)
    send_telegram(msg)
    lvl_type = 'nivel único' if cluster['single_level'] else 'cluster 2 niveles'
    log.info(f'ALERTA → {symbol} {direction} [{lvl_type}]')


# ══════════════════════════════════════════════════════════
#  SCAN PRINCIPAL
# ══════════════════════════════════════════════════════════
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
