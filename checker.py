#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TFZ Checker — Verificacion automatica de resultados del scanner.
Como funciona:
  1. Lee alerts_log.json (creado por main.py con cada alerta).
  2. Para cada alerta OPEN, descarga las velas 15m posteriores al momento
     en que se disparo la alerta.
  3. Recorre vela a vela: si el high supera el TP WIN, si el low
     cae bajo el SL LOSS. El primero en tocarse gana.
  4. Si han pasado mas de 8h sin tocar ninguno: TIMEOUT (trade caducado).
  5. Actualiza alerts_log.json con los resultados.
  6. Envia a Telegram un resumen de todas las alertas (win rate, PF, etc).
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
EVAL_HOURS = 8   # ventana de evaluacion (igual que en el backtest)
INTERVAL_MAP = {
    '1m': 'Min1', '15m': 'Min15', '1h': 'Min60',
    '4h': 'Hour4', '1d': 'Day1',
}
# ══════════════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════════════
def send_telegram(msg: str):
    """Envia mensaje dividiendolo si supera 4000 caracteres."""
    chunks = []
    current = []
    current_len = 0
    for line in msg.splitlines():
        if current_len + len(line) + 1 > 4000 and current:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    for chunk in chunks:
        try:
            r = requests.post(
                f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
                data={
                    'chat_id':    TELEGRAM_CHAT_ID,
                    'text':       chunk,
                    'parse_mode': 'HTML',
                    'disable_web_page_preview': 'true',
                },
                timeout=10,
            )
            if not r.ok:
                log.error(f'Telegram error: {r.text}')
        except Exception as e:
            log.error(f'Telegram exception: {e}')
        time.sleep(0.4)
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
#  EVALUACION DE TRADE
# ══════════════════════════════════════════════════════════
def check_outcome(alert: dict):
    """
    Devuelve 'WIN', 'LOSS', 'TIMEOUT' o None si aun no hay veredicto.
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
    k = get_klines(symbol, '15m', limit=200)
    if not k or not k['time']:
        log.warning(f'{symbol}: sin datos para evaluar')
        return None
    alert_unix = alert_dt.timestamp()
    for i, ts in enumerate(k['time']):
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
        if hit_tp:
            return 'WIN'
        if hit_sl:
            return 'LOSS'
    if hours_elapsed >= EVAL_HOURS:
        return 'TIMEOUT'
    return None
# ══════════════════════════════════════════════════════════
#  ESTADISTICAS
# ══════════════════════════════════════════════════════════
def generate_stats(records: list) -> str:
    total = len(records)
    if total == 0:
        return '📊 TFZ Scanner — sin alertas registradas aun.'
    wins     = sum(1 for r in records if r['status'] == 'WIN')
    losses   = sum(1 for r in records if r['status'] == 'LOSS')
    timeouts = sum(1 for r in records if r['status'] == 'TIMEOUT')
    open_n   = sum(1 for r in records if r['status'] == 'OPEN')
    resolved = wins + losses
    wr = (wins / resolved * 100) if resolved > 0 else 0.0
    # --- Profit Factor y Expectancia -------------------------------------- #
    # Se calculan con los valores reales de TP/SL de cada alerta
    gross_profit = 0.0
    gross_loss = 0.0
    for r in records:
        entry = r.get('entry_price', 0)
        if entry <= 0:
            continue
        if r['status'] == 'WIN':
            gross_profit += abs(r.get('tp', 0) - entry) / entry * 100
        elif r['status'] == 'LOSS':
            gross_loss += abs(r.get('sl', 0) - entry) / entry * 100
    pf = round(gross_profit / gross_loss, 2) if gross_loss > 0 else None
    # Expectancia sobre TODOS los trades (TIMEOUT = 0, OPEN no cuenta)
    closed = [r for r in records if r['status'] != 'OPEN']
    if closed:
        exp = round((gross_profit - gross_loss) / len(closed), 2)
    else:
        exp = None
    # --- Racha maxima de perdidas ----------------------------------------- #
    max_consec_loss = 0
    consec = 0
    for r in sorted(records, key=lambda x: x.get('timestamp', '')):
        if r['status'] == 'LOSS':
            consec += 1
            max_consec_loss = max(max_consec_loss, consec)
        elif r['status'] == 'WIN':
            consec = 0
    # --- Win rate por direccion ------------------------------------------- #
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
    # --- Win rate por nivel ----------------------------------------------- #
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
    # --- Mensaje ---------------------------------------------------------- #
    lines = [
        '📊 <b>TFZ Scanner — Resultados acumulados</b>\n',
        f'Total alertas: {total}',
        (f'✅ Ganadas: {wins}  ❌ Perdidas: {losses}  '
         f'⏱ Timeout: {timeouts}  🟡 Abiertas: {open_n}'),
        f'<b>Win rate: {wr:.0f}%</b> ({wins}/{resolved} resueltas)\n',
    ]
    # PF y Expectancia
    if pf is not None:
        pf_str = f"{pf:.2f}" if pf != float('inf') else "inf"
        lines.append(f'Profit Factor: <b>{pf_str}</b>  (objetivo &gt;1.5)')
    if exp is not None:
        lines.append(f'Expectancia: <b>{exp:+.2f}%</b> por trade cerrado')
    if max_consec_loss > 0:
        lines.append(f'Racha max de perdidas: {max_consec_loss} seguidas')
    lines.append('')
    # Por direccion
    if by_dir:
        lines.append('Por direccion:')
        for d, st in sorted(by_dir.items()):
            dwr = st['wins'] / st['total'] * 100
            lines.append(f'  {d}: {dwr:.0f}% ({st["wins"]}/{st["total"]})')
        lines.append('')
    # Por nivel
    if by_level:
        lines.append('Por nivel:')
        for lvl, st in sorted(by_level.items(), key=lambda x: -x[1]['total']):
            lvl_wr = st['wins'] / st['total'] * 100
            lines.append(f'  {lvl}: {lvl_wr:.0f}% ({st["wins"]}/{st["total"]})')
        lines.append('')
    # Ultimos 5 resultados resueltos
    recent = sorted(
        [r for r in records if r['status'] != 'OPEN'],
        key=lambda x: x['timestamp'],
        reverse=True,
    )[:5]
    if recent:
        lines.append('Ultimos resultados:')
        for r in recent:
            icon = {'WIN': '✅', 'LOSS': '❌', 'TIMEOUT': '⏱'}.get(r['status'], '?')
            ts   = r['timestamp'][:16].replace('T', ' ')
            entry_p = r.get('entry_price', 0) or 0
            tp_p    = r.get('tp', 0) or 0
            sl_p    = r.get('sl', 0) or 0
            dirn    = r.get('direction', 'LONG')
            if r['status'] == 'WIN' and entry_p and tp_p:
                _pnl = (tp_p - entry_p) / entry_p * 100 if dirn == 'LONG' else (entry_p - tp_p) / entry_p * 100
                chg_str = f'+{_pnl:.1f}%'
            elif r['status'] == 'LOSS' and entry_p and sl_p:
                _pnl = (sl_p - entry_p) / entry_p * 100 if dirn == 'LONG' else (entry_p - sl_p) / entry_p * 100
                chg_str = f'{_pnl:.1f}%'
            else:
                chg_str = 'timeout'
            lines.append(
                f'  {icon} {r["symbol"]} {r["direction"]} '
                f'({chg_str}) — {ts}'
            )
    return '\n'.join(lines)
# ══════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════
def main():
    log.info('=== TFZ Checker iniciado ===')
    if not ALERTS_LOG.exists():
        log.info('No hay alerts_log.json — nada que comprobar')
        return
    with open(ALERTS_LOG, encoding='utf-8') as f:
        records = json.load(f)
    if not records:
        log.info('alerts_log.json vacio — nada que comprobar')
        return
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
    log.info('=== TFZ Checker completado ===')
if __name__ == '__main__':
    main()
