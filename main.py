"""
ZCT Scanner — Zero Complexity Trading
Monitoriza niveles clave en futuros perpetuos (Bybit) y alerta por Telegram
cuando el precio se aproxima a un nivel con condiciones favorables.

Autor: generado con Claude para Curro / Tradetor
"""

import time
import logging
import requests
from datetime import datetime, timezone
import os

# ────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ['TELEGRAM_TOKEN']
TELEGRAM_CHAT_ID = os.environ['TELEGRAM_CHAT_ID']

SYMBOLS = [
    'BTC_USDT', 'ETH_USDT', 'SOL_USDT', 'HYPE_USDT',
    'DOGE_USDT', 'BNB_USDT', 'XRP_USDT', 'LINK_USDT',  # TON no disponible en MEXC, reemplazado por XRP
    'TAO_USDT', 'AVAX_USDT',
    # MEXC usa formato SYMBOL_USDT — los que no existen se filtran automáticamente
    # Si quieres añadir más: 'XRP_USDT', 'ADA_USDT', etc.
]

SMMA_LENGTH      = 30       # Longitud de la Smoothed MA (ZCT)
VOLUSD_MA_LEN    = 60       # Periodos para la MA del VolUSD
VOLUSD_MIN       = 100_000  # VolUSD mínimo ($100K) — filtro de liquidez ZCT
PROXIMITY_PCT    = 0.005    # Distancia al nivel para activar alerta (0.5%)
ALERT_COOLDOWN_S = 3600     # Cooldown entre alertas del mismo símbolo+nivel (1h)
SCAN_INTERVAL_S  = 60       # Segundos entre escaneos completos (solo en modo continuo)
SCAN_ONCE        = os.environ.get('SCAN_ONCE', '0') == '1'  # True en GitHub Actions

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# MEXC API (sin restricciones geográficas)
# ─────────────────────────────────────────────
MEXC_BASE = 'https://contract.mexc.com'

# Mapa de intervalos: formato interno → formato MEXC
INTERVAL_MAP = {
    '1m': 'Min1',
    '1h': 'Min60',  # MEXC usa Min60 para velas de 1h (no Hour1)
    '4h': 'Hour4',
    '1d': 'Day1',
}

def get_klines(symbol, interval, limit=200):
    """Devuelve lista de velas como dicts {open, high, low, close, volume}."""
    mexc_interval = INTERVAL_MAP.get(interval, interval)
    try:
        r = requests.get(
            f'{MEXC_BASE}/api/v1/contract/kline/{symbol}',
            params={'interval': mexc_interval, 'limit': limit},
            timeout=10
        )
        r.raise_for_status()
        data = r.json()
        # MEXC devuelve listas paralelas de arrays
        candles = []
        d = data['data']
        for i in range(len(d['time'])):
            candles.append({
                'open':   float(d['open'][i]),
                'high':   float(d['high'][i]),
                'low':    float(d['low'][i]),
                'close':  float(d['close'][i]),
                'volume': float(d['vol'][i]),
            })
        return candles
    except Exception as e:
        log.error(f'{symbol} {interval}: {e}')
        return []

def validate_symbols(symbols):
    """Filtra los símbolos que realmente existen en MEXC Futuros."""
    try:
        r = requests.get(
            f'{MEXC_BASE}/api/v1/contract/detail',
            timeout=15
        )
        valid = {s['symbol'] for s in r.json()['data']}
        ok   = [s for s in symbols if s in valid]
        skip = [s for s in symbols if s not in valid]
        if skip:
            log.warning(f'No disponibles en MEXC Futuros (se ignoran): {skip}')
        return ok
    except Exception as e:
        log.error(f'Error validando símbolos: {e}')
        return symbols

# ─────────────────────────────────────────────
# INDICADORES ZCT
# ─────────────────────────────────────────────
def calc_smma(closes, length=30):
    """
    Smoothed Moving Average (Wilder's smoothing).
    Idéntica a la SMMA de TradingView con length=30.
    """
    result = [None] * len(closes)
    if len(closes) < length:
        return result
    result[length - 1] = sum(closes[:length]) / length
    for i in range(length, len(closes)):
        result[i] = (result[i - 1] * (length - 1) + closes[i]) / length
    return result

def calc_volusd_ma(candles, length=60):
    """MA del VolUSD (volumen × precio de cierre)."""
    vols = [c['close'] * c['volume'] for c in candles]
    if len(vols) < length:
        return None
    return sum(vols[-length:]) / length

def count_crossovers(closes, smma_vals, lookback=50):
    """
    Cuenta cuántas veces el precio cruzó la SMMA en las últimas `lookback` velas.
    Regla ZCT: cada cruce cuenta, incluyendo mechas.
    Simplificación: usamos el precio de cierre (no mechas) — suficiente para la clasificación.
    """
    pairs = [(c, s) for c, s in zip(closes, smma_vals) if s is not None]
    if len(pairs) < 2:
        return 0
    recent = pairs[-lookback:]
    crosses = 0
    for i in range(1, len(recent)):
        prev_above = recent[i-1][0] > recent[i-1][1]
        curr_above = recent[i][0]   > recent[i][1]
        if prev_above != curr_above:
            crosses += 1
    return crosses

def get_ma_direction(smma_vals, lookback=10, threshold=0.001):
    """
    Dirección de la SMMA: 'trending_up', 'trending_down' o 'sideways'.
    Compara el valor actual con el de hace `lookback` velas.
    """
    valid = [s for s in smma_vals if s is not None]
    if len(valid) < lookback + 1:
        return 'unknown'
    change = (valid[-1] - valid[-(lookback + 1)]) / valid[-(lookback + 1)]
    if change >  threshold: return 'trending_up'
    if change < -threshold: return 'trending_down'
    return 'sideways'

def classify(crosses, direction):
    """
    Clasifica las condiciones según las tablas ZCT.
    Devuelve (momentum_class, mr_class): 'IDEAL', 'AVERAGE' o 'POOR'.
    """
    trending = direction in ('trending_up', 'trending_down')
    sideways = direction == 'sideways'

    # Momentum
    if   crosses <= 3 and trending:  momentum = 'IDEAL'
    elif crosses <= 6 and trending:  momentum = 'AVERAGE'
    else:                            momentum = 'POOR'

    # Mean Reversion (espejo exacto del momentum)
    if   crosses >= 7 and sideways:                            mr = 'IDEAL'
    elif (crosses >= 7 and trending) or \
         (4 <= crosses <= 6 and sideways):                     mr = 'AVERAGE'
    else:                                                       mr = 'POOR'

    return momentum, mr

# ─────────────────────────────────────────────
# NIVELES ZCT (PDH/PDL, P4H-H/L, P1H-H/L)
# ─────────────────────────────────────────────
def get_levels(symbol):
    """
    Obtiene niveles clave de timeframes superiores:
    - PDH / PDL  → máximo y mínimo del día anterior
    - P4H-H / L  → máximo y mínimo de la vela 4H anterior
    - P1H-H / L  → máximo y mínimo de la vela 1H anterior
    """
    levels = {}
    specs = [('1d', 'PD'), ('4h', 'P4H'), ('1h', 'P1H')]
    for interval, prefix in specs:
        candles = get_klines(symbol, interval, limit=3)
        if len(candles) >= 2:
            prev = candles[-2]
            levels[f'{prefix}H'] = prev['high']
            levels[f'{prefix}L'] = prev['low']
    return levels

def find_nearby_levels(price, levels, threshold=PROXIMITY_PCT):
    """Devuelve los niveles dentro del threshold, ordenados por proximidad."""
    result = []
    for name, lvl in levels.items():
        dist = abs(price - lvl) / lvl
        if dist <= threshold:
            result.append((name, lvl, dist))
    return sorted(result, key=lambda x: x[2])

# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────
def send_telegram(msg):
    try:
        requests.post(
            f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
            json={'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'HTML'},
            timeout=10
        ).raise_for_status()
    except Exception as e:
        log.error(f'Telegram error: {e}')

def build_alert(symbol, price, lvl_name, lvl_price, dist_pct,
                mom, mr, crosses, direction, vma, vcurrent):

    dir_emoji = {'trending_up': '📈', 'trending_down': '📉', 'sideways': '↔️'}.get(direction, '❓')
    dir_es    = {'trending_up': 'Alcista', 'trending_down': 'Bajista', 'sideways': 'Lateral'}.get(direction, '?')

    vol_ratio = (vcurrent / vma * 100) if vma else 0
    if   vol_ratio > 115: vol_txt = '📈 Creciente'
    elif vol_ratio < 85:  vol_txt = '📉 Decreciente'
    else:                 vol_txt = '➡️ Plano'

    # Mejor setup disponible
    if   mom == 'IDEAL':    setup = '🚀 MOMENTUM — IDEAL';   best = 'mom'
    elif mr  == 'IDEAL':    setup = '🔄 MEAN REVERSION — IDEAL';  best = 'mr'
    elif mom == 'AVERAGE':  setup = '🚀 MOMENTUM — AVERAGE'; best = 'mom'
    elif mr  == 'AVERAGE':  setup = '🔄 MEAN REVERSION — AVERAGE'; best = 'mr'
    else:                   setup = '⚠️ Condiciones POOR';   best = 'none'

    # Dirección: niveles H = resistencia, L = soporte
    is_high = lvl_name.endswith('H')
    if best == 'mr':
        trade_dir = 'SHORT' if is_high else 'LONG'
    elif best == 'mom':
        trade_dir = 'LONG' if is_high else 'SHORT'
    else:
        trade_dir = None

    # Entry / SL / TP  (SL = 1.2% fijo, TP = 1R)
    SL_PCT   = 0.012
    LEVERAGE = 5
    entry = price
    if trade_dir == 'SHORT':
        sl = entry * (1 + SL_PCT)
        tp = entry * (1 - SL_PCT)
    elif trade_dir == 'LONG':
        sl = entry * (1 - SL_PCT)
        tp = entry * (1 + SL_PCT)
    else:
        sl = tp = None

    fmt = lambda p: f'{p:,.4f}' if p >= 1 else f'{p:.6f}'

    if trade_dir and sl:
        dir_txt   = '🔴 SHORT' if trade_dir == 'SHORT' else '🟢 LONG'
        trade_blk = (
            f'\n─────────────────\n'
            f'<b>{dir_txt}  ·  x{LEVERAGE} (demo)</b>\n'
            f'📥 Entry: {fmt(entry)}\n'
            f'🛑 SL:    {fmt(sl)}\n'
            f'🎯 TP:    {fmt(tp)}\n'
        )
    else:
        trade_blk = ''

    return (
        f'🔔 <b>{symbol}</b> — Nivel ZCT próximo\n\n'
        f'📍 <b>Nivel:</b> {lvl_name} @ {fmt(lvl_price)}\n'
        f'💰 <b>Precio:</b> {fmt(price)} ({dist_pct * 100:.2f}% del nivel)\n\n'
        f'{setup}\n'
        f'{dir_emoji} <b>30SMMA:</b> {dir_es}\n'
        f'🔁 <b>Cruces (50 velas 1m):</b> {crosses}\n'
        f'📊 <b>Volumen:</b> {vol_txt} ({vol_ratio:.0f}% de su MA)'
        f'{trade_blk}\n'
        f'⏰ {datetime.now(timezone.utc).strftime("%H:%M UTC")}'
    )
# ─────────────────────────────────────────────
# COOLDOWN (evita spam de alertas)
# ─────────────────────────────────────────────
_cooldowns: dict = {}

def can_alert(symbol, level):
    key = (symbol, level)
    return (time.time() - _cooldowns.get(key, 0)) > ALERT_COOLDOWN_S

def mark_alert(symbol, level):
    _cooldowns[(symbol, level)] = time.time()

# ─────────────────────────────────────────────
# ESCANEO POR SÍMBOLO
# ─────────────────────────────────────────────
def scan(symbol):
    # 1. Velas de 1m (necesitamos al menos 150 para SMMA + VolUSD MA)
    candles = get_klines(symbol, '1m', limit=150)
    if len(candles) < SMMA_LENGTH + VOLUSD_MA_LEN:
        log.warning(f'{symbol}: datos insuficientes')
        return

    closes = [c['close'] for c in candles]
    price  = closes[-1]

    # 2. Indicadores
    smma_vals = calc_smma(closes, SMMA_LENGTH)
    if smma_vals[-1] is None:
        return

    vma      = calc_volusd_ma(candles, VOLUSD_MA_LEN)
    vcurrent = candles[-1]['close'] * candles[-1]['volume']

    # Filtro de liquidez ZCT: VolUSD MA mínimo $100K
    if not vma or vma < VOLUSD_MIN:
        log.info(f'{symbol}: VolUSD MA bajo ({vma:.0f}), skip')
        return

    crosses   = count_crossovers(closes, smma_vals)
    direction = get_ma_direction(smma_vals)
    mom, mr   = classify(crosses, direction)

    log.info(f'{symbol}: precio={price:.4f} cruces={crosses} dir={direction} mom={mom} mr={mr}')

    # Si ambas condiciones son POOR, no hay nada que alertar
    if mom == 'POOR' and mr == 'POOR':
        return

    # 3. Niveles ZCT y proximidad
    levels = get_levels(symbol)
    nearby = find_nearby_levels(price, levels)

    for lvl_name, lvl_price, dist in nearby:
        if can_alert(symbol, lvl_name):
            msg = build_alert(
                symbol, price, lvl_name, lvl_price, dist,
                mom, mr, crosses, direction, vma, vcurrent
            )
            send_telegram(msg)
            mark_alert(symbol, lvl_name)
            log.info(f'✅ Alerta enviada: {symbol} @ {lvl_name}')

# ─────────────────────────────────────────────
# LOOP PRINCIPAL
# ─────────────────────────────────────────────
def main():
    log.info('ZCT Scanner arrancando...')
    active = validate_symbols(SYMBOLS)
    log.info(f'Símbolos activos: {active}')

    # Mensaje de inicio solo en modo continuo (no en GitHub Actions)
    if not SCAN_ONCE:
        send_telegram(
            '🟢 <b>ZCT Scanner iniciado</b>\n'
            f'📊 Monitorizando: {", ".join(active)}\n'
            f'⏱ Intervalo: {SCAN_INTERVAL_S}s\n'
            f'📍 Proximidad a nivel: {PROXIMITY_PCT * 100:.1f}%\n'
            f'🔕 Cooldown entre alertas: {ALERT_COOLDOWN_S // 60} min'
        )

    while True:
        log.info(f'--- Escaneo ({len(active)} símbolos) ---')
        for symbol in active:
            try:
                scan(symbol)
            except Exception as e:
                log.error(f'Error en {symbol}: {e}')
            time.sleep(2)  # pausa entre símbolos para respetar rate limits

        if SCAN_ONCE:
            log.info('Escaneo único completado (modo GitHub Actions).')
            break

        log.info(f'Escaneo completo. Próximo en {SCAN_INTERVAL_S}s.')
        time.sleep(SCAN_INTERVAL_S)

if __name__ == '__main__':
    main()
