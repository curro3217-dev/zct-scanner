"""
ZCT Scanner v8 — Selección Dinámica de Movers
Selecciona monedas automáticamente según:
  - Volumen 24h > $20M (rebajado de $100M para mejor cobertura MEXC)
  - Cambio 1d ≥ +10% → solo LONG
  - Cambio 1d ≤ -10% → solo SHORT
Aplica filtros ZCT: 15m, vol 120-200%, vela alcista, cruces<=1, dist<=0.4%.
Niveles: P4H y P15m (backtest v7: WR 67% y 64%. P1H y PDH descartados).
Cambios v8: vol max 200%, niveles reducidos a P4H+P15m.

Estrategia: Trading From Zero / Koroush AK (ZCT)
Autor: generado con Claude para Curro / Tradetor
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

# ══════════════════════════════════════════════════════════
#  CONFIGURACIÓN
# ══════════════════════════════════════════════════════════
TELEGRAM_TOKEN   = os.environ['TELEGRAM_TOKEN']
TELEGRAM_CHAT_ID = os.environ['TELEGRAM_CHAT_ID']

# ── Selección de monedas ──────────────────────────────────
VOL_24H_MIN   = 20_000_000    # $20M mínimo volumen 24h (MEXC tiene menos liquidez que Binance)
CHANGE_THRESH = 10.0          # ±10% en 1d para ser mover

# ── Niveles ZCT ──────────────────────────────────────────
PROXIMITY_PCT  = 0.004   # alerta cuando precio está a 0.4% de un nivel (backtest v6)
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

# Ruta al log de alertas (se guarda junto a main.py, en la raíz del repo)
ALERTS_LOG = Path(__file__).parent / 'alerts_log.json'


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


def get_daily_change(symbol: str) -> float | None:
    """
    Calcula el cambio real de precio en las últimas 24h usando velas diarias.
    Devuelve el porcentaje (ej: -21.5 para -21.5%) o None si falla.

    Por qué no usar riseFallRate del bulk ticker:
      - El bulk usa cambio de día calendario UTC+8 (medianoche → ahora).
      - TradingView usa 24h rodantes (desde hace 24h hasta ahora).
      - Al principio del día UTC+8 el bulk puede mostrar -2% mientras
        TradingView muestra -21%. Esto causaba 0 movers falsos.
    """
    k = get_klines(symbol, '1d', limit=3)
    if not k or len(k['close']) < 2:
        return None
    prev_close = k['close'][-2]
    curr_close = k['close'][-1]
    if prev_close <= 0:
        return None
    return (curr_close - prev_close) / prev_close * 100


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
    - Vol 24h > $20M (desde bulk ticker)
    - Cambio 1d >= +10% o <= -10% (calculado desde klines diarias, igual que TradingView)
    Ordenados por cambio absoluto descendente.
    """
    tickers = get_all_tickers()
    candidates = []

    # Paso 1: pre-filtrar por volumen usando el bulk ticker (rápido, 1 sola llamada)
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

    # Paso 2: calcular cambio real 24h desde velas diarias y filtrar por threshold
    movers = []
    for c in candidates:
        symbol  = c['symbol']
        change_pct = get_daily_change(symbol)
        if change_pct is None:
            log.debug(f'{symbol}: sin datos diarios')
            continue

        if change_pct >= CHANGE_THRESH:
            direction = 'LONG'
        elif change_pct <= -CHANGE_THRESH:
            direction = 'SHORT'
        else:
            log.debug(f'{symbol}: cambio {change_pct:.1f}% < {CHANGE_THRESH}% → skip')
            continue

        movers.append({
            'symbol':     symbol,
            'direction':  direction,
            'change_pct': change_pct,
            'vol_24h':    c['vol_24h'],
            'price':      c['price'],
        })
        time.sleep(0.15)  # evitar rate limit MEXC entre llamadas individuales

    movers.sort(key=lambda x: abs(x['change_pct']), reverse=True)
    n_long  = sum(1 for m in movers if m['direction'] == 'LONG')
    n_short = sum(1 for m in movers if m['direction'] == 'SHORT')
    log.info(f'Movers: {len(movers)} ({n_long}L / {n_short}S)')

    # Log top 5 para debug
    for m in movers[:5]:
        log.info(f'  {m["symbol"]} {m["direction"]} {m["change_pct"]:+.1f}% vol=${m["vol_24h"]/1e6:.0f}M')

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

    mom_ideal = (crosses <= 3 and direction in ('up', 'down') and vol_ratio > 80)
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
    # Solo 4h y 15m — backtest v7: WR 67% y 64%. P1H (41%) y PDH (25%) descartados.
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
#  LOG DE ALERTAS (para tracking de resultados)
# ══════════════════════════════════════════════════════════
def save_alert_to_log(mover: dict, cluster: dict, zct: dict,
                      near_lvl: float, dist_pct: float):
    """
    Guarda cada alerta en alerts_log.json para análisis posterior de resultados.
    El checker.py leerá este fichero y comprobará si se tocó TP o SL.
    """
    now    = datetime.now(timezone.utc)
    price  = mover['price']
    direction = mover['direction']
    tp_pct = SL_PCT * TP_MULT

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
        'vol_ratio':    round(zct['vol_ratio'], 1),
        'crosses':      zct['crosses'],
        'ma_direction': zct['direction'],
        'lvl1_name':    cluster['lvl1_name'],
        'lvl1':         cluster['lvl1'],
        'lvl2_name':    cluster.get('lvl2_name'),
        'lvl2':         cluster.get('lvl2'),
        'dist_pct':     round(dist_pct, 3),
        'status':       'OPEN',   # OPEN → WIN / LOSS / TIMEOUT
        'resolved_at':  None,
        'resolved_price': None,
    }

    # Cargar log existente (si hay)
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

    # Velas 15m para análisis ZCT (backtest v5: edge en 15m, no 1m)
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

    # Filtro 1: MA nunca contra la dirección del trade
    if direction == 'LONG' and ma_dir == 'down':
        log.info(f'{symbol}: LONG pero MA bajista → skip')
        return
    if direction == 'SHORT' and ma_dir == 'up':
        log.info(f'{symbol}: SHORT pero MA alcista → skip')
        return

    # Filtro 2: máximo 1 cruce (2-3 = 0% WR en backtest, >3 = choppy)
    if crosses > 1:
        log.info(f'{symbol}: demasiado choppy ({crosses} cruces) → skip')
        return

    # Filtro 3: volumen mínimo 80% de la media (permite tendencias sostenidas, no solo spikes)
    # Máx 200% sigue activo (backtest v7: >200% tiene WR 22%)
    if vol_ratio < 80:
        log.info(f'{symbol}: volumen muy bajo ({vol_ratio:.0f}%) → skip')
        return
    if vol_ratio > 200:
        log.info(f'{symbol}: volumen demasiado alto ({vol_ratio:.0f}%) → skip')
        return

    # Filtro 4: vela alcista obligatoria para LONG (close > open en 15m)
    if direction == 'LONG' and closes[-1] <= opens[-1]:
        log.info(f'{symbol}: vela bajista en LONG → skip')
        return

    log.info(f'{symbol}: chg={mover["change_pct"]:.1f}% dir={direction} '
             f'ma={ma_dir} cruces={crosses} vol={vol_ratio:.0f}% ✓ filtros OK')

    # Niveles ZCT
    levels = get_zct_levels(symbol)
    if len(levels) < 2:
        log.info(f'{symbol}: no hay suficientes niveles → skip')
        return

    log.info(f'{symbol}: niveles = {", ".join(f"{k}={v:.6g}" for k,v in levels.items())}')

    # Cluster de 2 niveles (o 1 si pump fuerte ≥30%)
    cluster = find_level_cluster(
        levels, price, direction, symbol,
        change_pct=mover['change_pct']
    )
    if not cluster:
        # Mostrar por qué no hay cluster: niveles en la dirección correcta y sus distancias
        cands = [(k, v, abs(v-price)/price*100) for k, v in levels.items()
                 if (direction == 'LONG' and v > price) or (direction == 'SHORT' and v < price)]
        if cands:
            cands_str = ', '.join(f'{k}={dist:.2f}%' for k,_,dist in cands)
            log.info(f'{symbol}: sin cluster — candidatos en dirección: {cands_str} → skip')
        else:
            log.info(f'{symbol}: sin niveles en dirección {direction} → skip')
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

    # Filtro 5: distancia mínima al nivel (muy pegado = precio ya lo cruzó)
    if dist_pct < 0.2:
        log.info(f'{symbol}: demasiado pegado al nivel ({dist_pct:.3f}%) → skip')
        return

    # Alerta
    _cooldowns[key] = now
    msg = build_alert(mover, cluster, zct, near_lvl, dist_pct)
    send_telegram(msg)
    save_alert_to_log(mover, cluster, zct, near_lvl, dist_pct)
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
