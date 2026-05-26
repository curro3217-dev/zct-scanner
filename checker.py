"""
ZCT Checker — Verificación automática de resultados del scanner.

Cómo funciona:
  1. Lee alerts_log.json (creado por main.py con cada alerta).
  2. Para cada alerta OPEN, descarga las velas 15m posteriores al momento
     en que se disparó la alerta.
  3. Recorre vela a vela: si el high supera el TP → WIN, si el low
     cae bajo el SL → LOSS. El primero en tocarse gana.
  4. Si han pasado más de 8h sin tocar ninguno: TIMEOUT (trade caducado).
  5. Actualiza alerts_log.json con los resultados.
  6. Envía a Telegram un resumen de todas las alertas (win rate, por nivel, etc).

Autor: generado con Claude para Curro / Tradetor
"""

import json, os, time, logging
from datetime import datetime, timezone
from pathlib import Path
import requests

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
)

TELEGRAM_TOKEN   = os.environ['TELEGRAM_TOKEN']
TELEGRAM_CHAT_ID = os.environ['TELEGRAM_CHAT_ID']

ALERTS_LOG = Path(__file__).parent / 'alerts_log.json'
EVAL_HOURS = 8   # ventana de evaluación (igual que en el backtest)

INTERVAL_MAP = {
    '1m': 'Min1', '15m': 'Min15', '1h': 'Min60',
    '4h': 'Hour4', '1d': 'Day1',
}


# ══════════════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════════════
def send_telegram(msg: str):
    try:
        r = requests.post(
            f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
            data={
                'chat_id':    TELEGRAM_CHAT_ID,
                'text':       msg,
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
def get_klines(symbol: str, interval: str, limit: int = 200):
    """Descarga velas OHLCV de MEXC futuros, con timestamps."""
    try:
        r = requests.get(
            f'https://contract.mexc.com/api/v1/contract/kline/{symbol}',
            params={'interval': INTERVAL_MAP[interval], 'limit': limit},
            timeout=10,
        )
        d = r.json().get('data', {})
        if not d or 'close' not in d or not d['close']:
            return None

        # Timestamps: MEXC devuelve segundos UNIX.
        # Si el primer valor es > 1e12 asumimos milisegundos → dividir entre 1000.
        raw_times = [int(x) for x in d.get('time', [])]
        if raw_times and raw_times[0] > 1_700_000_000_000:
            times = [t // 1000 for t in raw_times]
        else:
            times = raw_times

        return {
            'time':  times,
            'open':  [float(x) for x in d['open']],
            'close': [float(x) for x in d['close']],
            'high':  [float(x) for x in d['high']],
            'low':   [float(x) for x in d['low']],
        }
    except Exception as e:
        log.error(f'{symbol} klines error: {e}')
        return None


# ══════════════════════════════════════════════════════════
#  EVALUACIÓN DE TRADE
# ══════════════════════════════════════════════════════════
def check_outcome(alert: dict):
    """
    Devuelve 'WIN', 'LOSS', 'TIMEOUT' o None si aún no hay veredicto.

    Lógica vela a vela (igual que el backtest):
      LONG:  high >= TP → WIN  |  low <= SL → LOSS
      SHORT: low  <= TP → WIN  |  high >= SL → LOSS
    El primero que se toca en cada vela gana (usamos orden: TP primero si
    la vela la toca en la misma barra que el SL, igual que en el backtest).
    """
    symbol    = alert['symbol']
    direction = alert['direction']
    entry     = alert['entry_price']
    sl        = alert['sl']
    tp        = alert['tp']

    alert_dt  = datetime.fromisoformat(alert['timestamp'])
    if alert_dt.tzinfo is None:
        alert_dt = alert_dt.replace(tzinfo=timezone.utc)

    now           = datetime.now(timezone.utc)
    hours_elapsed = (now - alert_dt).total_seconds() / 3600

    # Con limit=200 velas de 15m cubrimos ~50 horas → más que suficiente
    k = get_klines(symbol, '15m', limit=200)
    if not k or not k['time']:
        log.warning(f'{symbol}: sin datos para evaluar')
        return None

    alert_unix = alert_dt.timestamp()

    for i, ts in enumerate(k['time']):
        # Solo velas DESPUÉS de que se disparó la alerta
        if ts < alert_unix:
            continue

        high = k['high'][i]
        low  = k['low'][i]

        if direction == 'LONG':
            hit_tp = high >= tp
            hit_sl = low  <= sl
        else:  # SHORT
            hit_tp = low  <= tp
            hit_sl = high >= sl

        # Si la misma vela toca ambos: asumimos TP primero (favorable;
        # en backtest se usa la misma asunción conservadora).
        if hit_tp:
            return 'WIN'
        if hit_sl:
            return 'LOSS'

    # Si han pasado >= 8h sin tocar nada: caducado
    if hours_elapsed >= EVAL_HOURS:
        return 'TIMEOUT'

    return None  # Sigue abierto dentro de la ventana de 8h


# ══════════════════════════════════════════════════════════
#  ESTADÍSTICAS
# ══════════════════════════════════════════════════════════
def generate_stats(records: list) -> str:
    total = len(records)
    if total == 0:
        return '📊 ZCT Scanner — sin alertas registradas aún.'

    wins     = sum(1 for r in records if r['status'] == 'WIN')
    losses   = sum(1 for r in records if r['status'] == 'LOSS')
    timeouts = sum(1 for r in records if r['status'] == 'TIMEOUT')
    open_n   = sum(1 for r in records if r['status'] == 'OPEN')
    resolved = wins + losses  # TIMEOUT no cuenta para win rate

    wr = (wins / resolved * 100) if resolved > 0 else 0.0

    # Win rate por nivel (solo WIN/LOSS, no TIMEOUT)
    by_level: dict = {}
    for r in records:
        if r['status'] not in ('WIN', 'LOSS'):
            continue
        lvl = r.get('lvl1_name', 'Desconocido')
        if lvl not in by_level:
            by_level[lvl] = {'wins': 0, 'total': 0}
        by_level[lvl]['total'] += 1
        if r['status'] == 'WIN':
            by_level[lvl]['wins'] += 1

    # Win rate por dirección
    by_dir: dict = {}
    for r in records:
        if r['status'] not in ('WIN', 'LOSS'):
            continue
        d = r['direction']
        if d not in by_dir:
            by_dir[d] = {'wins': 0, 'total': 0}
        by_dir[d]['total'] += 1
        if r['status'] == 'WIN':
            by_dir[d]['wins'] += 1

    lines = [
        '📊 <b>ZCT Scanner — Resultados acumulados</b>\n',
        f'Total alertas: {total}',
        (f'✅ Ganadas: {wins}  ❌ Perdidas: {losses}  '
         f'⏱ Timeout: {timeouts}  🟡 Abiertas: {open_n}'),
        f'<b>Win rate: {wr:.0f}%</b> ({wins}/{resolved} resueltas)\n',
    ]

    if by_dir:
        lines.append('Por dirección:')
        for d, st in sorted(by_dir.items()):
            dwr = st['wins'] / st['total'] * 100
            lines.append(f'  {d}: {dwr:.0f}% ({st["wins"]}/{st["total"]})')
        lines.append('')

    if by_level:
        lines.append('Por nivel:')
        for lvl, st in sorted(by_level.items(), key=lambda x: -x[1]['total']):
            lvl_wr = st['wins'] / st['total'] * 100
            lines.append(f'  {lvl}: {lvl_wr:.0f}% ({st["wins"]}/{st["total"]})')
        lines.append('')

    # Últimos 5 resultados resueltos
    recent = sorted(
        [r for r in records if r['status'] != 'OPEN'],
        key=lambda x: x['timestamp'],
        reverse=True,
    )[:5]
    if recent:
        lines.append('Últimos resultados:')
        for r in recent:
            icon = {'WIN': '✅', 'LOSS': '❌', 'TIMEOUT': '⏱'}.get(r['status'], '?')
            ts   = r['timestamp'][:16].replace('T', ' ')
            chg  = f'+{r["change_pct"]:.1f}%' if r['change_pct'] > 0 else f'{r["change_pct"]:.1f}%'
            lines.append(
                f'  {icon} {r["symbol"]} {r["direction"]} '
                f'({chg}, vol {r["vol_ratio"]:.0f}%) — {ts}'
            )

    return '\n'.join(lines)


# ══════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════
def main():
    log.info('=== ZCT Checker iniciado ===')

    if not ALERTS_LOG.exists():
        log.info('No hay alerts_log.json todavía — nada que comprobar')
        return

    with open(ALERTS_LOG, encoding='utf-8') as f:
        records = json.load(f)

    open_alerts = [r for r in records if r['status'] == 'OPEN']
    log.info(f'Alertas abiertas: {len(open_alerts)} / Total: {len(records)}')

    updated = 0
    for alert in open_alerts:
        try:
            outcome = check_outcome(alert)
            if outcome:
                alert['status']       = outcome
                alert['resolved_at']  = datetime.now(timezone.utc).isoformat()
                log.info(f'{alert["symbol"]} {alert["direction"]} → {outcome}')
                updated += 1
            else:
                log.info(f'{alert["symbol"]} {alert["direction"]} → sigue OPEN')
        except Exception as e:
            log.error(f'Error evaluando {alert.get("id")}: {e}')
        time.sleep(0.4)

    if updated > 0:
        with open(ALERTS_LOG, 'w', encoding='utf-8') as f:
            json.dump(records, f, indent=2, ensure_ascii=False)
        log.info(f'{updated} alertas actualizadas en el log')

    stats_msg = generate_stats(records)
    send_telegram(stats_msg)
    log.info('=== ZCT Checker completado ===')


if __name__ == '__main__':
    main()
