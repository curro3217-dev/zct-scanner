"""
ZCT Scanner v5 — Selección Dinámica de Movers
Selecciona monedas automáticamente según:
  - Volumen 24h > $100M
  - Cambio 1d ≥ +10% → solo LONG
  - Cambio 1d ≤ -10% → solo SHORT
Aplica filtros ZCT (backtest v6): 15m, vol>=200%, vela alcista, cruces<=1, dist<=0.4%.
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
STRONG
